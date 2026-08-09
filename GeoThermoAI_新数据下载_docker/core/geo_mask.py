"""
研究区多边形栅格化（像元占比按研究区口径统计的公共工具）

把研究区 GeoJSON（任意 CRS，如 EPSG:4490 经纬度）重投影到参考栅格的
坐标系，并按参考栅格网格栅格化，得到「像元中心是否落在研究区内」的
布尔掩膜。用于把所有像元占比统计的**分母**从 bbox 外接矩形全网格
改为研究区多边形内的像元数（口径统一，见会话决策）。

与 `core/geo_transform.py` 的约定一致：任何失败都返回 None，调用方回退
bbox 口径，绝不中断主流程。
"""

import logging
from typing import Optional

from osgeo import gdal, ogr

logger = logging.getLogger(__name__)


def rasterize_region(geojson_path: str, ref_tif: str) -> Optional[object]:
    """把研究区多边形栅格化到参考栅格网格。

    Args:
        geojson_path: 研究区 GeoJSON 文件路径（可为 EPSG:4490 等任意 CRS）
        ref_tif:      参考栅格（读取其 GeoTransform / CRS / 尺寸）

    Returns:
        numpy bool 掩膜（True=研究区内），失败返回 None。
    """
    if not geojson_path or not ref_tif:
        return None
    try:
        ref = gdal.Open(ref_tif)
        if ref is None:
            return None
        tmp = "/vsimem/_region_mask_input.geojson"
        vt = gdal.VectorTranslate(tmp, geojson_path, dstSRS=ref.GetProjection())
        if vt is None:
            return None
        src = ogr.Open(tmp)
        if src is None:
            gdal.Unlink(tmp)
            return None
        layer = src.GetLayer(0)
        if layer is None or layer.GetFeatureCount() == 0:
            gdal.Unlink(tmp)
            return None

        ds = gdal.GetDriverByName("MEM").Create(
            "", ref.RasterXSize, ref.RasterYSize, 1, gdal.GDT_Byte)
        ds.SetGeoTransform(ref.GetGeoTransform())
        ds.SetProjection(ref.GetProjection())
        err = gdal.RasterizeLayer(ds, [1], layer, burn_values=[1])
        mask = ds.ReadAsArray().astype(bool)
        gdal.Unlink(tmp)
        return mask
    except Exception as e:
        logger.warning(f"[geo_mask] 研究区栅格化失败（回退 bbox 口径）: {e}")
        try:
            gdal.Unlink(tmp)
        except Exception:
            pass
        return None


def region_ratio(mask, valid: object = None) -> Optional[float]:
    """研究区口径占比 = 研究区内有效像元 / 研究区内总像元。

    mask 为空或全 False 时返回 None（无法计算，调用方回退 bbox 口径）。
    """
    if mask is None or int(mask.sum()) == 0:
        return None
    if valid is None:
        valid = mask
    return float((valid & mask).sum()) / float(mask.sum())
