"""
10m LST 空洞填补（结果后处理）核心算法

背景：10m 地表温度产品在数据预处理阶段按联合掩膜（Landsat QA 无云 +
Sentinel-2 SCL ∈ {4,5,6} + 热红外有效值）剔除了云/云影等像元，因此最终
导出的 `rf_10m_lst_final_{date}.tif` 在云区存在 nodata 空洞。

本模块对这些空洞做**空间重建**（gap filling），只做估计、不改变无云区数值：

    1. 若整幅图空洞占比很小（< 2%），直接在原始分辨率用 IDW 插值填一次；
    2. 否则采用「多尺度金字塔 + IDW」：
       - 逐级 2× 平均聚合下采样，直到某层空洞占比 < 2% 或达到最大层数，
         最粗层代表「大尺度温度趋势」；
       - 最粗层对残余小洞做 IDW（k 近邻反距离加权）填充；
       - 逐层双线性上采样回填：有效像元写真值，空洞像元继承上层趋势值。
       这样成片云洞中心由大尺度趋势决定，不会被拉平成洞口邻域均值。

输出：填洞后的 LST GeoTIFF + 空洞掩膜 GeoTIFF（1=估计像元，0=原始有效），
并返回填充统计。填洞不依赖 DEM，也不参与 TCR/闭合精度评价。
"""

import logging
import os
from typing import Dict, Optional, Tuple

import numpy as np
import rasterio

logger = logging.getLogger(__name__)

# 空洞占比低于该值视为"小洞"，直接 IDW 一次填完（省掉金字塔）
_SMALL_HOLE_RATIO = 0.02
# 金字塔最大下采样层数（2^8 = 256 倍）
_MAX_LEVEL = 8
# IDW 近邻数
_IDW_K = 16
# IDW 距离幂次
_IDW_POWER = 2.0


def _load_band(tif_path: str) -> Tuple[np.ndarray, dict]:
    """读取 tif 第一波段为 float32，nodata/非有限 → NaN；返回 (数组, profile)。

    LST 数值范围 ~250–330 K，float32 有 7 位有效数字、精度完全够用，
    比 float64 省一半内存（内存优化：全图读入 + 金字塔处理下峰值减半）。
    """
    import rasterio

    with rasterio.open(tif_path) as src:
        arr = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        nodata = src.nodata
        if nodata is not None:
            arr[arr == nodata] = np.nan
        arr[~np.isfinite(arr)] = np.nan
    return arr, profile


def _downsample_mean(arr: np.ndarray) -> np.ndarray:
    """2× 平均聚合：只对有效值求均值；整块无效 → NaN。"""
    h, w = arr.shape
    h2, w2 = h // 2, w // 2
    if h2 < 1 or w2 < 1:
        raise ValueError("影像尺寸过小，无法继续下采样")
    block = arr[: h2 * 2, : w2 * 2].reshape(h2, 2, w2, 2)
    valid = np.isfinite(block)
    s = np.where(valid, block, 0.0).sum(axis=(1, 3))
    n = valid.sum(axis=(1, 3))
    out = np.full((h2, w2), np.nan, dtype=np.float32)
    ok = n > 0
    out[ok] = (s[ok] / n[ok]).astype(np.float32)
    return out


def _idw_fill(arr: np.ndarray, k: int = _IDW_K, power: float = _IDW_POWER) -> np.ndarray:
    """对 arr 中的 NaN 像元做 k 近邻反距离加权插值，返回填好的数组（原地语义，返回新数组）。"""
    from scipy.spatial import cKDTree

    mask = np.isfinite(arr)
    if mask.all():
        return arr
    out = arr.copy()
    valid_coords = np.argwhere(mask)
    hole_coords = np.argwhere(~mask)
    if len(valid_coords) == 0:
        raise ValueError("没有任何有效像元可参考，无法填洞")
    # 有效像元的值（用分开的坐标数组做花式索引，避免 arr[坐标对] 的错误取行）
    valid_vals = arr[valid_coords[:, 0], valid_coords[:, 1]].astype(np.float64)
    tree = cKDTree(valid_coords)
    kk = min(k, len(valid_coords))
    dists, idxs = tree.query(hole_coords, k=kk, workers=-1)
    dists = np.asarray(dists, dtype=np.float64)
    idxs = np.asarray(idxs)
    if kk == 1:
        dists = dists[:, None]
        idxs = idxs[:, None]
    weights = 1.0 / (np.maximum(dists, 1e-6) ** power)
    # 逐近邻累加（避免构造 (N, k) 大中间数组）
    num = np.zeros(len(hole_coords), dtype=np.float64)
    den = np.zeros(len(hole_coords), dtype=np.float64)
    for j in range(kk):
        wj = weights[:, j]
        vj = valid_vals[idxs[:, j]]
        num += wj * vj
        den += wj
    out[hole_coords[:, 0], hole_coords[:, 1]] = (num / np.maximum(den, 1e-12)).astype(arr.dtype)
    return out


def _stats(arr: np.ndarray) -> Dict[str, float]:
    v = arr[np.isfinite(arr)]
    if len(v) == 0:
        return {}
    return {
        "min": float(v.min()),
        "max": float(v.max()),
        "mean": float(v.mean()),
        "std": float(v.std()),
    }


def gapfill_lst(
    input_tif: str,
    output_tif: str,
    output_mask_tif: str = "",
    max_level: int = _MAX_LEVEL,
    progress_callback=None,
) -> Dict:
    """对带空洞的 10m LST GeoTIFF 做空间填洞。

    Args:
        input_tif:      原始 LST GeoTIFF（空洞 = nodata/NaN）
        output_tif:     填洞后输出 GeoTIFF（保留地理参考，全像元有值）
        output_mask_tif:空洞掩膜 GeoTIFF（uint8：1=估计像元，0=原始有效）；可留空不写
        max_level:      金字塔最大下采样层数
        progress_callback: 进度回调 (percent 0~1, message)

    Returns:
        dict: 统计信息（total_pixels / valid_pixels / filled_pixels / filled_ratio
              / before / after / max_level / used_pyramid）
    """
    if not os.path.isfile(input_tif):
        raise FileNotFoundError(f"输入 LST 影像不存在: {input_tif}")

    def _progress(pct: float, msg: str = ""):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    _progress(0.0, "读取原始 LST 影像")
    arr, profile = _load_band(input_tif)
    height, width = arr.shape
    total = int(height * width)
    valid = int(np.isfinite(arr).sum())
    filled = total - valid
    # 占比口径统一：优先用研究区多边形内像元数（写入 tif 的 REGION_PIXELS 元数据，
    # 由 export_geotiff 写入）；缺失时回退 bbox 全网格
    region_pixels = 0
    try:
        with rasterio.open(input_tif) as _src:
            _tag = _src.tags().get("REGION_PIXELS")
        region_pixels = int(_tag) if _tag else 0
    except Exception:
        region_pixels = 0
    denom = region_pixels if region_pixels > 0 else total
    filled_ratio = filled / denom if denom else 0.0
    before = _stats(arr)

    # 全图无洞：原样输出（拷贝）
    if filled == 0:
        os.makedirs(os.path.dirname(output_tif) or ".", exist_ok=True)
        _write_tif(arr, output_tif, profile)
        if output_mask_tif:
            _write_tif(np.zeros((height, width), dtype=np.uint8), output_mask_tif,
                       _mask_profile(profile, height, width))
        _progress(1.0, "原始影像无空洞，无需填补")
        return {
            "total_pixels": total, "valid_pixels": valid, "filled_pixels": 0,
            "filled_ratio": 0.0, "region_pixels": region_pixels,
            "before": before, "after": _stats(arr),
            "max_level": 0, "used_pyramid": False,
            "output_tif": output_tif, "output_mask_tif": output_mask_tif,
        }

    _progress(0.1, "构建多尺度金字塔")
    # 小洞捷径：空洞占比 < 2% 时直接在原始分辨率 IDW 一次填完
    used_pyramid = True
    if filled_ratio < _SMALL_HOLE_RATIO:
        _progress(0.5, "空洞占比小，直接 IDW 填充")
        filled_arr = _idw_fill(arr)
        used_pyramid = False
        effective_level = 0
    else:
        # 逐级下采样，直到空洞占比 < 2% 或达到最大层数
        levels = [arr]
        masks = [~np.isfinite(arr)]
        level = 0
        while level < max_level:
            nxt = _downsample_mean(levels[-1])
            nxt_mask = ~np.isfinite(nxt)
            levels.append(nxt)
            masks.append(nxt_mask)
            level += 1
            if nxt_mask.sum() / max(nxt.size, 1) < _SMALL_HOLE_RATIO:
                break

        effective_level = level
        _progress(0.2 + 0.1 * level / max_level, f"最粗层 {level}：空洞占比已收敛")

        # 最粗层 IDW 填充残余小洞
        coarse = _idw_fill(levels[-1])
        prev = coarse

        # 逐层双线性上采样 + 回填真实值
        for lv in range(level - 1, -1, -1):
            from PIL import Image

            target = levels[lv]
            h_t, w_t = target.shape
            # PIL 双线性上采样到当前层尺寸（兼容不同 scipy 版本对 output_shape 的支持差异）
            trend = np.asarray(
                Image.fromarray(np.asarray(prev, dtype=np.float32)).resize(
                    (w_t, h_t), Image.BILINEAR),
                dtype=np.float32,
            )
            filled_layer = np.where(np.isfinite(target), target, trend)
            prev = filled_layer
            _progress(
                0.3 + 0.6 * (level - lv) / max(level, 1),
                f"回填第 {lv} 层（{prev.shape[0]}×{prev.shape[1]}）",
            )
        filled_arr = prev

    # 兜底：仍可能存在非有限值（如粗层全 NaN 或 zoom 边界），用整体均值填补
    leftover = ~np.isfinite(filled_arr)
    if leftover.any():
        fill_value = float(np.nanmean(filled_arr)) if np.isfinite(filled_arr).any() else 0.0
        filled_arr[leftover] = fill_value

    after = _stats(filled_arr)
    mask = (np.isfinite(arr) ^ True).astype(np.uint8)

    _progress(0.9, "写入填洞结果与空洞掩膜")
    os.makedirs(os.path.dirname(output_tif) or ".", exist_ok=True)
    _write_tif(filled_arr, output_tif, profile)
    if output_mask_tif:
        os.makedirs(os.path.dirname(output_mask_tif) or ".", exist_ok=True)
        _write_tif(mask, output_mask_tif, _mask_profile(profile, height, width))

    _progress(1.0, "填洞完成")
    return {
        "total_pixels": total, "valid_pixels": valid, "filled_pixels": int(filled),
        "filled_ratio": round(filled_ratio, 6), "region_pixels": region_pixels,
        "before": before, "after": after,
        "max_level": effective_level, "used_pyramid": used_pyramid,
        "output_tif": output_tif, "output_mask_tif": output_mask_tif,
    }


def _mask_profile(profile: dict, height: int, width: int) -> dict:
    """空洞掩膜 tif 的 profile（uint8，单波段）。"""
    p = dict(profile)
    p.update(dtype="uint8", count=1, nodata=None, compress="lzw",
             tiled=True, blockxsize=256, blockysize=256,
             height=int(height), width=int(width))
    return p


def _write_tif(arr: np.ndarray, path: str, profile: dict, nodata: float = np.nan) -> None:
    """按 profile 写单波段 tif（dtype 由数组决定，大数组分块写，控制内存）。

    LST 以 float32 写出（内存优化），掩膜为 uint8；float dtype 保留 NaN 语义。
    """
    import rasterio

    dtype = str(arr.dtype)
    p = dict(profile)
    p.update(dtype=dtype, count=1, compress="lzw", tiled=True,
             blockxsize=256, blockysize=256,
             height=int(arr.shape[0]), width=int(arr.shape[1]))
    if dtype.startswith("float"):
        p["nodata"] = nodata
    else:
        p["nodata"] = None
    with rasterio.open(path, "w", **p) as dst:
        dst.write(arr, 1)
