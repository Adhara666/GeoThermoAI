"""
GDAL/OSR 坐标变换共享工具

问题背景：GDAL 3 对 EPSG:4326 默认使用权威（纬度,经度）轴序，而代码历史上一直按
传统 GIS（经度,纬度）顺序传参，导致武汉经度 114.3 被当成纬度，产生
``PROJ: utm: Invalid latitude`` 和 ``(inf, inf)``。

修复：
    1. 对源/目标 SRS 都显式设置 osr.OAMS_TRADITIONAL_GIS_ORDER，不依赖不同 GDAL
       版本的默认轴序行为；
    2. 启用 GDAL/OSR exceptions；
    3. 不只转换两个对角点，而是对四条边加密取样后求包络，跨区/大范围时更稳健；
    4. 转换后显式校验所有坐标为有限值、包络宽高大于零，失败时抛出说明性错误
      （包含原始 bbox），而不是把 inf 继续传给下游 Warp。

本模块同时被 server.py（GDAL 自检 /api/test/gdal）与
core/skills/builtin/data_acquisition.py（实际下载时的 bbox 变换）复用，
避免出现"自检用一套逻辑、实际下载用另一套逻辑"的偏差。
"""

import math
import os
from typing import List, Optional, Tuple


def enable_gdal_osr_exceptions() -> None:
    """启用 GDAL/OSR 异常模式（默认是静默返回 None/错误码），让契约错误尽早暴露。"""
    from osgeo import gdal, osr

    try:
        gdal.UseExceptions()
    except Exception:
        pass
    try:
        osr.UseExceptions()
    except Exception:
        pass


def make_traditional_gis_order_srs(epsg: int):
    """创建显式设置为传统 GIS 轴序（经度,纬度 / x,y）的 SpatialReference，
    不依赖 GDAL 版本默认行为。"""
    from osgeo import osr

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def utm_epsg_for_lonlat(center_lon: float, center_lat: float) -> int:
    """根据中心经纬度计算合适的 UTM EPSG 代码（WGS84 基准）。"""
    utm_zone = int((center_lon + 180) / 6) + 1
    return (32600 if center_lat >= 0 else 32700) + utm_zone


def _region_center_lonlat(study_area_geojson: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """读取研究区 GeoJSON/SHP，返回 WGS84 下的代表性中心点 (lon, lat)。

    优先取面积最大的多边形（多部件时按面积加权）的质心；文件缺失/读取/转换
    失败返回 (None, None)，由调用方回退到 bbox 中心。仅用于选择 UTM 投影带，
    不做裁剪等用途。
    """
    if not study_area_geojson or not os.path.isfile(study_area_geojson):
        return None, None
    try:
        from osgeo import ogr, osr
        ds = ogr.Open(study_area_geojson)
        if ds is None:
            return None, None
        lyr = ds.GetLayer(0)
        srs_src = lyr.GetSpatialRef()
        ct = None
        if srs_src is not None:
            srs_wgs84 = osr.SpatialReference()
            srs_wgs84.ImportFromEPSG(4326)
            ct = osr.CoordinateTransformation(srs_src, srs_wgs84)
        lyr.ResetReading()
        best_geom = None
        best_area = -1.0
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is None:
                continue
            if ct is not None:
                g = g.Clone()
                try:
                    g.Transform(ct)
                except Exception:
                    return None, None
            try:
                area = g.GetArea()
            except Exception:
                area = -1.0
            if area > best_area:
                best_area = area
                best_geom = g
        if best_geom is None:
            return None, None
        centroid = best_geom.Centroid()
        if centroid is None:
            return None, None
        lon, lat, _ = centroid.GetPoint(0)
        if not (math.isfinite(lon) and math.isfinite(lat)
                and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None, None
        return float(lon), float(lat)
    except Exception:
        return None, None


def utm_epsg_for_region(bbox: List[float], study_area_geojson: Optional[str] = None) -> int:
    """按研究区选择 UTM EPSG：有 GeoJSON/SHP 时取多边形质心，否则取 bbox 中心。

    相比仅用 bbox 中心，不规则形状/跨投影带边界的市政研究区投影带选择更贴近
    实际范围（质心落入哪个 6° 带就用哪个带）。
    """
    lon, lat = _region_center_lonlat(study_area_geojson)
    if lon is None or lat is None:
        lon = (bbox[0] + bbox[2]) / 2.0
        lat = (bbox[1] + bbox[3]) / 2.0
    return utm_epsg_for_lonlat(lon, lat)


def bbox_wgs84_to_utm_bounds(
    bbox: List[float], utm_epsg: int, densify_points: int = 8,
) -> Tuple[float, float, float, float]:
    """把 WGS84 bbox [lon_min, lat_min, lon_max, lat_max] 转换为目标 UTM 的
    有限、有序包络 (x1, y1, x2, y2)。

    不只转换左下/右上两个对角点：对四条边各加密 densify_points 个采样点后
    求包络，减轻大范围/跨投影带场景下"只连两角"的失真；转换后显式校验
    所有坐标有限、包络宽高为正，失败时抛出包含原始 bbox 的说明性错误。

    Args:
        bbox: [lon_min, lat_min, lon_max, lat_max]（WGS84，传统经纬度顺序）
        utm_epsg: 目标 UTM EPSG 代码
        densify_points: 每条边加密采样点数（含端点）

    Returns:
        (x1, y1, x2, y2): 目标 UTM 下的有限、有序包络

    Raises:
        RuntimeError: 任一采样点变换结果非有限，或最终包络宽高不为正
    """
    from osgeo import osr

    enable_gdal_osr_exceptions()
    lon_min, lat_min, lon_max, lat_max = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise RuntimeError(f"坐标轴序/投影变换失败：输入 bbox 含非有限值: {bbox}")
    if not (-180.0 <= lon_min <= 180.0 and -180.0 <= lon_max <= 180.0
            and -90.0 <= lat_min <= 90.0 and -90.0 <= lat_max <= 90.0):
        raise RuntimeError(
            f"坐标轴序/投影变换失败：bbox {bbox} 不在合理经纬度范围内"
            f"（是否传入了投影坐标而非 WGS84 经纬度？请检查研究区 CRS）"
        )

    srs_wgs84 = make_traditional_gis_order_srs(4326)
    srs_utm = make_traditional_gis_order_srs(utm_epsg)
    ct = osr.CoordinateTransformation(srs_wgs84, srs_utm)

    n = max(2, densify_points)
    sample_points = []
    for i in range(n):
        t = i / (n - 1)
        lon = lon_min + t * (lon_max - lon_min)
        sample_points.append((lon, lat_min))
        sample_points.append((lon, lat_max))
        lat = lat_min + t * (lat_max - lat_min)
        sample_points.append((lon_min, lat))
        sample_points.append((lon_max, lat))

    xs, ys = [], []
    for lon, lat in sample_points:
        x, y, _z = ct.TransformPoint(lon, lat)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise RuntimeError(
                f"坐标轴序/投影变换失败：WGS84({lon:.6f},{lat:.6f}) → EPSG:{utm_epsg} "
                f"得到非有限值 ({x},{y})；原始 bbox={bbox}"
            )
        xs.append(x)
        ys.append(y)

    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    if not (x2 > x1 and y2 > y1):
        raise RuntimeError(
            f"坐标轴序/投影变换失败：变换后包络宽高不为正 ({x1},{y1})-({x2},{y2})；原始 bbox={bbox}"
        )
    return x1, y1, x2, y2
