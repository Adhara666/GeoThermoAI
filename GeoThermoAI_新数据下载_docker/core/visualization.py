"""
GeoThermoAI 可视化模块

基于 Folium / Leaflet 的多图层交互式地图，支持 GeoTIFF 渲染（UTM→WGS84 转换）。
参考升级规划 v2 第 3.7 节 LayerVisualizer 设计。

v3.2 变更：
- 移除 NDVI / NDWI / NDBI 遥感指数图层（地图不再展示指数）
- 图层名称简化：30m LST / Sentinel-2 RGB / DEM / 10m LST
- Sentinel-2 RGB 改用分位数拉伸，兼容 0-255 与 0-10000 等不同量纲，避免全黑不显示
"""

import glob
import math
import os
import re
from functools import lru_cache
from typing import List, Optional, Tuple

import numpy as np

try:
    import folium
    from folium.raster_layers import ImageOverlay
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

try:
    import rasterio
    from rasterio.warp import transform as warp_transform
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False

try:
    from matplotlib import cm
    HAS_CM = True
except ImportError:
    HAS_CM = False


# 默认地图中心（武汉），未指定项目目录或无法读取时使用
_DEFAULT_CENTER = [30.59, 114.30]


class LayerVisualizer:
    """管理所有可用于地图的数据图层

    每个图层定义包含：
    - id: 唯一标识
    - label: 显示名称
    - group: 分组（数据获取 / 遥感指数 / 结果）
    - file: 相对项目目录的文件路径
    - band: 单波段索引（从 1 开始）
    - bands: 多波段 RGB 列表（如 [3, 2, 1]）
    - compute: 从 sentinel2_bands.tif 现算指数（ndvi / ndwi / ndbi）
    - colormap: matplotlib 色带名（如 "RdYlBu_r"）
    - opacity: 默认不透明度
    - visible: 默认是否可见
    """

    # sentinel2_bands.tif 波段顺序（与 data_acquisition 下载一致）：
    # 1=B02(Blue) 2=B03(Green) 3=B04(Red) 4=B08(NIR) 5=B11(SWIR1)

    LAYER_DEFS = [
        {
            "id": "landsat_lst",
            "label": "30m LST",
            "group": "数据获取",
            "file": "raw/landsat_lst.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "visible": True,
            "temperature": True,  # 地表温度图层（单位 K），供"显示温度"采样
            # Landsat Collection 2 Level-2 LST：栅格为原始 DN，需换算为 K
            "scale": 0.00341802,
            "offset": 149.0,
        },
        {
            "id": "sentinel_rgb",
            "label": "Sentinel-2 RGB",
            "group": "数据获取",
            "file": "raw/sentinel2_bands.tif",
            "bands": [3, 2, 1],  # B04(Red), B03(Green), B02(Blue)
            "opacity": 0.8,
            "visible": True,
        },
        {
            "id": "dem",
            "label": "DEM",
            "group": "数据获取",
            "file": "raw/dem.tif",
            "band": 1,
            "colormap": "terrain",
            "opacity": 0.6,
            "visible": True,
        },
        {
            "id": "lst_10m",
            "label": "10m LST",
            "group": "结果",
            "file": "results/rf_10m_lst_final.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "visible": True,
            "temperature": True,
        },
        {
            "id": "lst_10m_filled",
            "label": "10m LST（填洞后）",
            "group": "结果",
            "file": "results/rf_10m_lst_final_filled.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "visible": False,
            "temperature": True,
        },
    ]

    # ── 内部工具 ───────────────────────────────────────────────

    @staticmethod
    def _resolve_layer_path(project_dir: str, layer_def: dict) -> str:
        """解析图层文件实际路径（升级点 2/4）。

        优先取项目下最近修改的影像对目录（project_dir/pairs/L{date}_S{date}）中的文件；
        文件名带日期（如 landsat_lst_20240701.tif）时按前缀 glob 匹配最新的一个；
        兼容无 pairs 目录 / 无日期的旧布局。
        """
        if not project_dir:
            return ""
        base = project_dir
        pairs_root = os.path.join(project_dir, "pairs")
        if os.path.isdir(pairs_root):
            pair_dirs = [os.path.join(pairs_root, d) for d in sorted(os.listdir(pairs_root))
                         if os.path.isdir(os.path.join(pairs_root, d))]
            if pair_dirs:
                base = max(pair_dirs, key=lambda p: os.path.getmtime(p))
        fixed = os.path.join(base, layer_def.get("file", ""))
        if os.path.isfile(fixed):
            return fixed
        name, ext = os.path.splitext(os.path.basename(fixed))
        directory = os.path.dirname(fixed)
        if directory and os.path.isdir(directory):
            # 只匹配纯 8 位日期后缀（YYYYMMDD），避免 glob 的 * 误匹配
            # 到 _cloud_mask 等派生产物（升级点：结果后处理填洞图层隔离）
            pattern = re.compile(re.escape(name) + r"_[0-9]{8}" + re.escape(ext) + r"$")
            matches = [p for p in glob.glob(os.path.join(directory, f"{name}_[0-9]*{ext}"))
                       if pattern.match(os.path.basename(p))]
            if matches:
                return max(matches, key=lambda p: os.path.getmtime(p))
        return fixed

    @staticmethod
    def _bounds_to_wgs84(src) -> List[List[float]]:
        """将 rasterio 数据源的 bounds 从源 CRS 转换到 WGS84 (EPSG:4326)

        返回 [[south, west], [north, east]] 供 ImageOverlay 使用
        """
        left, bottom, right, top = src.bounds
        src_crs = src.crs
        if src_crs is None:
            # 无 CRS 信息，直接当作 WGS84
            return [[float(bottom), float(left)], [float(top), float(right)]]
        try:
            lons, lats = warp_transform(
                src_crs, "EPSG:4326",
                [left, right, right, left],
                [bottom, bottom, top, top],
            )
            return [
                [float(min(lats)), float(min(lons))],
                [float(max(lats)), float(max(lons))],
            ]
        except Exception:
            return [[float(bottom), float(left)], [float(top), float(right)]]

    @staticmethod
    def _file_bounds(tif_path: str) -> Optional[List[List[float]]]:
        """仅读取栅格边界（WGS84），不渲染像素"""
        if not HAS_RASTERIO or not tif_path or not os.path.isfile(tif_path):
            return None
        try:
            with rasterio.open(tif_path) as src:
                return LayerVisualizer._bounds_to_wgs84(src)
        except Exception:
            return None

    @staticmethod
    def _colorize(arr: np.ndarray, colormap: Optional[str] = None,
                  alpha: int = 255) -> Optional[np.ndarray]:
        """归一化 [min,max] → 色带 RGBA；无效像素（NaN）置为透明"""
        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            return None
        vmin = float(np.nanmin(valid))
        vmax = float(np.nanmax(valid))
        if vmax <= vmin:
            vmax = vmin + 1.0
        normed = np.clip((arr - vmin) / (vmax - vmin + 1e-10), 0, 1)

        rgba = None
        if colormap and HAS_CM:
            try:
                cmap = getattr(cm, colormap)
                rgba = (cmap(normed) * 255).astype(np.uint8)
            except Exception:
                rgba = None
        if rgba is None:
            gray = (normed * 255).astype(np.uint8)
            rgba = np.stack([gray, gray, gray, np.full_like(gray, alpha)], axis=-1)

        rgba[..., 3] = np.where(np.isnan(arr), 0, alpha)
        return rgba

    @staticmethod
    def _downsample_shape(h_orig: int, w_orig: int, max_size: int = 1024) -> Tuple[int, int, int]:
        """计算降采样步长与输出尺寸"""
        step = max(1, max(h_orig, w_orig) // max_size)
        out_h, out_w = (h_orig + step - 1) // step, (w_orig + step - 1) // step
        return step, out_h, out_w

    @staticmethod
    def _read_band(tif_path: str, band: int = 1, colormap: Optional[str] = None,
                   max_size: int = 1024) -> Optional[Tuple[np.ndarray, List[List[float]]]]:
        """读取单波段，返回 RGBA 数组 + WGS84 边界"""
        if not HAS_RASTERIO:
            return None
        with rasterio.open(tif_path) as src:
            h_orig, w_orig = src.height, src.width
            step, out_h, out_w = LayerVisualizer._downsample_shape(h_orig, w_orig, max_size)
            arr = src.read(band, out_shape=(out_h, out_w)).astype(np.float32)
            nodata = src.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            bounds = LayerVisualizer._bounds_to_wgs84(src)

        rgba = LayerVisualizer._colorize(arr, colormap=colormap)
        if rgba is None:
            return None
        return rgba, bounds

    @staticmethod
    def _read_rgb(tif_path: str, bands: List[int],
                  max_size: int = 1024) -> Optional[Tuple[np.ndarray, List[List[float]]]]:
        """读取多波段生成 RGB 真彩色

        用 2%~98% 分位数拉伸替代固定 scale_factor：兼容 0-255 / 0-10000
        等不同量纲的 S2 数据，避免固定缩放导致整幅全黑不显示。
        """
        if not HAS_RASTERIO:
            return None
        try:
            with rasterio.open(tif_path) as src:
                h_orig, w_orig = src.height, src.width
                step, out_h, out_w = LayerVisualizer._downsample_shape(h_orig, w_orig, max_size)
                nodata = src.nodata
                n_bands = src.count
                rgb = []
                for b in bands:
                    if b < 1 or b > n_bands:
                        rgb.append(np.full((out_h, out_w), np.nan, dtype=np.float32))
                        continue
                    arr = src.read(b, out_shape=(out_h, out_w)).astype(np.float32)
                    if nodata is not None:
                        arr = np.where(arr == nodata, np.nan, arr)
                    rgb.append(arr)
                bounds = LayerVisualizer._bounds_to_wgs84(src)
        except Exception:
            return None

        colored = np.empty((out_h, out_w, 3), dtype=np.uint8)
        for i in range(3):
            band = rgb[i]
            valid = band[~np.isnan(band)]
            if valid.size == 0:
                colored[..., i] = 0
                continue
            lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
            if hi <= lo:
                hi = lo + 1e-6
            norm = np.clip((band - lo) / (hi - lo), 0, 1)
            colored[..., i] = (norm * 255).astype(np.uint8)

        # 三个波段都是无效值的像素 → 透明
        alpha = np.where(
            np.isnan(rgb[0]) & np.isnan(rgb[1]) & np.isnan(rgb[2]), 0, 255
        ).astype(np.uint8)
        rgba = np.concatenate([colored, alpha[..., None]], axis=-1)
        return rgba, bounds

    # ── 瓦片金字塔渲染（按原生分辨率显示） ─────────────────────

    # 全局统计缓存：key = (文件路径, 大小, mtime, 类型, 波段)
    _STATS_CACHE: dict = {}

    @staticmethod
    def _tile_merc_bounds(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
        """Web Mercator (EPSG:3857) 瓦片范围 [minx, miny, maxx, maxy]"""
        n = 2 ** z
        R = 6378137.0
        half = math.pi * R
        step = 2 * half / n
        return (x * step - half, half - (y + 1) * step,
                (x + 1) * step - half, half - y * step)

    @staticmethod
    def _read_sample(src, band: int, max_size: int = 1024) -> np.ndarray:
        """降采样读取单波段样本（nodata→NaN），用于全局统计"""
        step, h, w = LayerVisualizer._downsample_shape(src.height, src.width, max_size)
        arr = src.read(band, out_shape=(h, w)).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        return arr

    @staticmethod
    def _cached_stats(file_path: str, kind: str, band: int = 1) -> Tuple[float, float]:
        """读取并缓存全局统计：kind='range' → (min,max)；kind='pct' → (2%,98%)"""
        key = (os.path.realpath(file_path), os.path.getsize(file_path),
               os.path.getmtime(file_path), kind, band)
        cached = LayerVisualizer._STATS_CACHE.get(key)
        if cached is not None:
            return cached
        with rasterio.open(file_path) as src:
            arr = LayerVisualizer._read_sample(src, band)
        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            result = (0.0, 1.0)
        elif kind == "range":
            result = (float(valid.min()), float(valid.max()))
        else:
            result = (float(np.percentile(valid, 2)), float(np.percentile(valid, 98)))
        LayerVisualizer._STATS_CACHE[key] = result
        return result

    @staticmethod
    def _colorize_with_range(arr: np.ndarray, vmin: float, vmax: float,
                             colormap: Optional[str] = None,
                             alpha: int = 255) -> np.ndarray:
        """用给定全局范围归一化着色（瓦片间颜色保持一致）"""
        if vmax <= vmin:
            vmax = vmin + 1.0
        normed = np.clip((arr - vmin) / (vmax - vmin + 1e-10), 0, 1)
        clean = np.nan_to_num(normed, nan=0.0)
        rgba = None
        if colormap and HAS_CM:
            try:
                cmap = getattr(cm, colormap)
                rgba = (cmap(clean) * 255).astype(np.uint8)
            except Exception:
                rgba = None
        if rgba is None:
            gray = (clean * 255).astype(np.uint8)
            rgba = np.stack([gray, gray, gray, np.full_like(gray, alpha)], axis=-1)
        rgba[..., 3] = np.where(np.isnan(arr), 0, alpha)
        return rgba

    @staticmethod
    def _native_zoom(src) -> int:
        """估算该栅格在 Web Mercator 下的原生缩放级别（1~20）"""
        try:
            a = abs(src.transform.a)
            if a <= 0:
                return 14
            if src.crs and src.crs.is_geographic:
                lat = (src.bounds.top + src.bounds.bottom) / 2
                px_m = a * 111320.0 * math.cos(math.radians(lat))
            else:
                cx = (src.bounds.left + src.bounds.right) / 2
                cy = (src.bounds.top + src.bounds.bottom) / 2
                lons, lats = rasterio.warp.transform(src.crs, "EPSG:4326", [cx], [cy])
                lat = float(lats[0])
                px_m = a  # 投影坐标单位视为米（UTM 等）
            if px_m <= 0:
                return 14
            z = math.log2(156543.03392 * math.cos(math.radians(abs(lat))) / px_m)
            return max(1, min(20, int(round(z))))
        except Exception:
            return 14

    @staticmethod
    def _file_meta(tif_path: str) -> Optional[Tuple[List[List[float]], int]]:
        """打开一次，返回 (WGS84 边界, 原生缩放级别)；失败返回 None"""
        if not HAS_RASTERIO or not tif_path or not os.path.isfile(tif_path):
            return None
        try:
            with rasterio.open(tif_path) as src:
                bounds = LayerVisualizer._bounds_to_wgs84(src)
                nz = LayerVisualizer._native_zoom(src)
            return bounds, nz
        except Exception:
            return None

    @staticmethod
    def render_layer_tile(layer_id: str, project_dir: str, z: int, x: int, y: int,
                          size: int = 256) -> Optional[bytes]:
        """渲染单个 Web Mercator 瓦片为 PNG；无数据/越界返回 None（前端视为透明）

        内部经 lru_cache 缓存 PNG（key 含文件 mtime，文件更新后自动失效），
        同一瓦片重复请求直接命中缓存，避免每次重新打开大 GeoTIFF，显著加速
        30m LST / Sentinel / DEM / 10m LST 等大影像图层渲染。
        """
        if not project_dir or not os.path.isdir(project_dir) or not HAS_RASTERIO:
            return None
        layer_def = next((d for d in LayerVisualizer.LAYER_DEFS if d["id"] == layer_id), None)
        if layer_def is None:
            return None
        file_path = LayerVisualizer._resolve_layer_path(project_dir, layer_def)
        if not os.path.isfile(file_path):
            return None
        n = 2 ** z
        if not (0 <= x < n and 0 <= y < n):
            return None
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return None
        return LayerVisualizer._render_tile_cached(layer_id, file_path, mtime, z, x, y, size)

    @staticmethod
    @lru_cache(maxsize=4096)
    def _render_tile_cached(layer_id: str, file_path: str, mtime: float, z: int, x: int, y: int,
                            size: int = 256) -> Optional[bytes]:
        """实际瓦片渲染（带缓存）；mtime 仅用于缓存失效，不参与计算"""
        layer_def = next((d for d in LayerVisualizer.LAYER_DEFS if d["id"] == layer_id), None)
        if layer_def is None:
            return None
        try:
            from rasterio.windows import Window, from_bounds as win_from_bounds
            import rasterio.warp
            minx, miny, maxx, maxy = LayerVisualizer._tile_merc_bounds(z, x, y)
            with rasterio.open(file_path) as src:
                try:
                    sx0, sy0, sx1, sy1 = rasterio.warp.transform_bounds(
                        "EPSG:3857", src.crs, minx, miny, maxx, maxy, densify_pts=4)
                except Exception:
                    return None
                tile_window = win_from_bounds(sx0, sy0, sx1, sy1, transform=src.transform)
                inter = tile_window.intersection(Window(0, 0, src.width, src.height))
                if inter.width <= 0 or inter.height <= 0:
                    return None
                # 有效区域在瓦片网格中的位置
                col0 = int(round((inter.col_off - tile_window.col_off) * size / tile_window.width))
                row0 = int(round((inter.row_off - tile_window.row_off) * size / tile_window.height))
                col1 = int(round((inter.col_off + inter.width - tile_window.col_off) * size / tile_window.width))
                row1 = int(round((inter.row_off + inter.height - tile_window.row_off) * size / tile_window.height))
                ow = max(col1 - col0, 1)
                oh = max(row1 - row0, 1)
                nodata = src.nodata

                if "bands" in layer_def:
                    # RGB 真彩色：全局 2%/98% 分位数拉伸
                    rgb = [None, None, None]
                    for i, b in enumerate(layer_def["bands"]):
                        if b < 1 or b > src.count:
                            continue
                        arr = src.read(b, window=inter, out_shape=(oh, ow)).astype(np.float32)
                        if nodata is not None:
                            arr = np.where(arr == nodata, np.nan, arr)
                        rgb[i] = arr
                    tile = np.full((size, size, 3), np.nan, dtype=np.float32)
                    for i, arr in enumerate(rgb):
                        if arr is not None:
                            tile[row0:row0 + oh, col0:col0 + ow, i] = arr
                    colored = np.empty((size, size, 3), dtype=np.uint8)
                    for i in range(3):
                        band = tile[..., i]
                        lo, hi = LayerVisualizer._cached_stats(file_path, "pct", layer_def["bands"][i]) \
                            if layer_def["bands"][i] <= src.count else (0.0, 1.0)
                        if hi <= lo:
                            hi = lo + 1e-6
                        norm = np.clip((band - lo) / (hi - lo), 0, 1)
                        colored[..., i] = np.where(np.isnan(band), 0, (norm * 255).astype(np.uint8))
                    alpha = np.where(
                        np.isnan(tile[..., 0]) & np.isnan(tile[..., 1]) & np.isnan(tile[..., 2]),
                        0, 255,
                    ).astype(np.uint8)
                    rgba = np.concatenate([colored, alpha[..., None]], axis=-1)
                else:
                    # 单波段：全局 min/max 着色
                    arr = src.read(layer_def.get("band", 1), window=inter,
                                   out_shape=(oh, ow)).astype(np.float32)
                    if nodata is not None:
                        arr = np.where(arr == nodata, np.nan, arr)
                    tile = np.full((size, size), np.nan, dtype=np.float32)
                    tile[row0:row0 + oh, col0:col0 + ow] = arr
                    vmin, vmax = LayerVisualizer._cached_stats(
                        file_path, "range", layer_def.get("band", 1))
                    rgba = LayerVisualizer._colorize_with_range(
                        tile, vmin, vmax, layer_def.get("colormap"))
            # 编码 PNG
            from PIL import Image
            import io
            pil = Image.fromarray(rgba)
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    # ── 公开接口 ───────────────────────────────────────────────

    @staticmethod
    def _rgba_to_data_url(img: np.ndarray) -> str:
        """将 RGBA 数组编码为 PNG data-URI

        folium ImageOverlay 的 image 参数不接受 Python 嵌套 list（会抛
        AttributeError 被静默跳过），统一编码为 data URI 保证图层可渲染。
        """
        try:
            from PIL import Image
            import base64
            import io
            pil = Image.fromarray(img)
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return img

    @staticmethod
    def render_layer_rgba(layer_id: str, project_dir: str) -> Optional[Tuple[np.ndarray, List[List[float]]]]:
        """渲染单个图层为 RGBA 数组 + WGS84 边界；不可用返回 None"""
        if not project_dir or not os.path.isdir(project_dir):
            return None
        layer_def = next((d for d in LayerVisualizer.LAYER_DEFS if d["id"] == layer_id), None)
        if layer_def is None:
            return None
        file_path = LayerVisualizer._resolve_layer_path(project_dir, layer_def)
        if not os.path.isfile(file_path):
            return None
        try:
            if "bands" in layer_def:
                return LayerVisualizer._read_rgb(
                    file_path, layer_def["bands"],
                )
            return LayerVisualizer._read_band(
                file_path, layer_def.get("band", 1), layer_def.get("colormap"),
            )
        except Exception:
            return None

    @staticmethod
    def render_layer_png(layer_id: str, project_dir: str) -> Optional[Tuple[bytes, List[List[float]]]]:
        """渲染单个图层为 PNG 字节 + WGS84 边界；不可用返回 None"""
        result = LayerVisualizer.render_layer_rgba(layer_id, project_dir)
        if result is None:
            return None
        img, bounds = result
        try:
            from PIL import Image
            import io
            pil = Image.fromarray(img)
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return buf.getvalue(), bounds
        except Exception:
            return None

    @staticmethod
    def _is_monthly_composite(file_path: str) -> bool:
        """文件是否属于月度合成产物：沿目录链向上查找 `.monthly_composite` 标记。

        月度合成在配对目录根（pairs/L{date}_S{date}/）写该标记，raw/processed/results
        三类子目录下的图层文件向上回溯 3 层都能命中。
        """
        d = os.path.dirname(file_path or "")
        for _ in range(3):
            if not d:
                return False
            if os.path.isfile(os.path.join(d, ".monthly_composite")):
                return True
            parent = os.path.dirname(d)
            if parent == d:
                return False
            d = parent
        return False

    @staticmethod
    def _layer_label_with_date(layer_def: dict, file_path: str) -> str:
        """图层标签附带影像日期（升级点 4：地图界面显示影像日期，DEM 除外）。

        从带日期的文件名（如 landsat_lst_20240701.tif）提取 YYYYMMDD：
        - 配对模式：格式化为 YYYY-MM-DD 追加到标签后；
        - 月度合成产物（目录含 .monthly_composite 标记）：显示为 YYYY-M（代表该月），
          避免月末代表日被误读成"单日影像"；
        无日期或 DEM 保持原名。
        """
        label = layer_def.get("label", "")
        if layer_def.get("id") == "dem":
            return label
        m = re.search(r"_(\d{8})(?=\.)", os.path.basename(file_path or ""))
        if not m:
            return label
        d = m.group(1)
        if LayerVisualizer._is_monthly_composite(file_path):
            return f"{label}（{d[:4]}-{int(d[4:6])}）"
        return f"{label}（{d[:4]}-{d[4:6]}-{d[6:]}）"

    @staticmethod
    def list_available_layers(project_dir: str) -> List[dict]:
        """列出所有图层：可用性、默认透明度、默认可见性、WGS84 边界、分组、原生缩放级别"""
        result = []
        for layer_def in LayerVisualizer.LAYER_DEFS:
            file_path = LayerVisualizer._resolve_layer_path(project_dir, layer_def)
            available = bool(file_path) and os.path.isfile(file_path)
            bounds = None
            max_native_zoom = None
            if available:
                meta = LayerVisualizer._file_meta(file_path)
                if meta:
                    bounds, max_native_zoom = meta
            result.append({
                "id": layer_def["id"],
                "label": LayerVisualizer._layer_label_with_date(layer_def, file_path),
                "group": layer_def.get("group", "图层"),
                "visible": layer_def.get("visible", False),
                "opacity": layer_def.get("opacity", 0.7),
                "available": available,
                "bounds": bounds,
                "max_native_zoom": max_native_zoom,
                "path": file_path,
                "is_lst": bool(layer_def.get("temperature", False)),
            })
        return result

    @staticmethod
    def sample_lst_value(project_dir: str, layer_id: str,
                         lat: float, lon: float) -> Optional[float]:
        """读取指定地表温度图层在 (lat, lon) 处单个像元的温度（单位 K）。

        返回 None 表示：图层不可用、坐标越界或像元为 NoData/非有限值。
        每次只读 1 个像元（rasterio Window），不加载整幅影像。
        """
        if not HAS_RASTERIO or not project_dir or not os.path.isdir(project_dir):
            return None
        layer_def = next((d for d in LayerVisualizer.LAYER_DEFS if d["id"] == layer_id), None)
        if layer_def is None or not layer_def.get("temperature"):
            return None
        file_path = LayerVisualizer._resolve_layer_path(project_dir, layer_def)
        if not os.path.isfile(file_path):
            return None
        try:
            with rasterio.open(file_path) as src:
                if src.crs and not src.crs.is_geographic:
                    xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
                else:
                    xs, ys = [lon], [lat]
                if not xs or not ys:
                    return None
                try:
                    row, col = src.index(float(xs[0]), float(ys[0]))
                except Exception:
                    return None  # 坐标落在栅格范围外
                if row < 0 or col < 0 or row >= src.height or col >= src.width:
                    return None
                window = rasterio.windows.Window(col, row, 1, 1)
                band = layer_def.get("band", 1)
                raw = src.read(band, window=window)[0, 0]
                if src.nodata is not None and raw == src.nodata:
                    return None
                if not np.isfinite(raw):
                    return None
                # 换算为 K：优先用图层定义里显式声明的 scale/offset
                # （如 Landsat L2 的 DN→K），否则回退栅格自带（多为默认 1/0）
                scale = float(layer_def.get("scale") or 1.0)
                offset = float(layer_def.get("offset") or 0.0)
                if scale == 1.0 and offset == 0.0:
                    try:
                        _s = getattr(src, "scales", None)
                        if isinstance(_s, (tuple, list)) and len(_s) >= band:
                            scale = float(_s[band - 1])
                    except Exception:
                        pass
                    try:
                        _o = getattr(src, "offsets", None)
                        if isinstance(_o, (tuple, list)) and len(_o) >= band:
                            offset = float(_o[band - 1])
                    except Exception:
                        pass
                return float(raw) * scale + offset
        except Exception:
            return None

    @staticmethod
    def build_map(project_dir: str) -> str:
        """生成包含所有可用图层的 Folium 地图 HTML（兼容旧版 iframe 用法）"""
        if not HAS_FOLIUM:
            return "<p style='color:#888;padding:12px'>⚠️ 未安装 folium，无法渲染地图。请运行: pip install folium</p>"

        if not project_dir or not os.path.isdir(project_dir):
            m = folium.Map(location=_DEFAULT_CENTER, zoom_start=10, control_scale=True)
            folium.LayerControl(collapsed=False).add_to(m)
            return m._repr_html_()

        m = folium.Map(control_scale=True, zoom_start=11)
        first_layer = True

        for layer_def in LayerVisualizer.LAYER_DEFS:
            if not os.path.isfile(LayerVisualizer._resolve_layer_path(project_dir, layer_def)):
                continue
            result = LayerVisualizer.render_layer_rgba(layer_def["id"], project_dir)
            if result is None:
                continue
            img, bounds = result

            if first_layer:
                center_lat = (bounds[0][0] + bounds[1][0]) / 2
                center_lon = (bounds[0][1] + bounds[1][1]) / 2
                m.location = [center_lat, center_lon]
                first_layer = False

            ImageOverlay(
                image=LayerVisualizer._rgba_to_data_url(img),
                bounds=bounds,
                opacity=layer_def.get("opacity", 0.7),
                name=layer_def["label"],
                show=layer_def.get("visible", True),
            ).add_to(m)

        # 底图选项
        folium.TileLayer("OpenStreetMap", name="街道地图").add_to(m)
        folium.TileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri", name="卫星影像",
        ).add_to(m)
        folium.LayerControl(collapsed=False).add_to(m)
        return m._repr_html_()

    @staticmethod
    def build_empty_map() -> str:
        """生成空地图（无项目目录时显示）"""
        if not HAS_FOLIUM:
            return "<p style='color:#888;padding:12px'>⚠️ 未安装 folium</p>"
        m = folium.Map(location=_DEFAULT_CENTER, zoom_start=10, control_scale=True)
        folium.TileLayer("OpenStreetMap", name="街道地图").add_to(m)
        folium.LayerControl(collapsed=False).add_to(m)
        return m._repr_html_()
