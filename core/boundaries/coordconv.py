# -*- coding: utf-8 -*-
"""坐标系转换：GCJ-02（火星坐标，高德/腾讯）与 WGS-84（GPS/遥感栅格）。

使用公开的标准数学算法（无外部依赖、离线可用），单点误差在米级，
用于「高德地理编码结果 → 栅格采样」的位置匹配足够。

参照实现（广泛使用的公开版本）：
  - 椭球参数：克拉索夫斯基椭球（GCJ-02 定义的基准）。
"""

import math

_A = 6378245.0                     # 长半轴
_EE = 0.00669342162296594323       # 偏心率平方


def _in_china(lon: float, lat: float) -> bool:
    """粗略判断是否在中国大陆范围（GCJ-02 偏移只在此范围内存在）。"""
    return 72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi)
            + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi)
            + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi)
            + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi)
            + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _delta(lon: float, lat: float):
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return dlon, dlat


def wgs84_to_gcj02(lon: float, lat: float):
    """WGS-84 → GCJ-02。中国范围外原样返回。"""
    if not _in_china(lon, lat):
        return lon, lat
    dlon, dlat = _delta(lon, lat)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float):
    """GCJ-02 → WGS-84（迭代逼近，米级精度）。中国范围外原样返回。"""
    if not _in_china(lon, lat):
        return lon, lat
    # 以 GCJ 坐标为初值，迭代修正；两次迭代即可收敛到亚米级
    wlon, wlat = lon, lat
    for _ in range(2):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon += lon - glon
        wlat += lat - glat
    return wlon, wlat


def gcj_geojson_to_wgs84(geojson: dict) -> dict:
    """把 GCJ-02 的 GeoJSON（Feature/FeatureCollection/几何）整体转为 WGS84。"""
    def convert_coords(coords):
        if isinstance(coords, (int, float)):
            return coords
        if coords and isinstance(coords[0], (int, float)):
            wlon, wlat = gcj02_to_wgs84(coords[0], coords[1])
            rest = list(coords[2:]) if len(coords) > 2 else []
            return [wlon, wlat] + rest
        return [convert_coords(c) for c in coords]

    def convert_geom(geom):
        g = dict(geom)
        if "coordinates" in g:
            g["coordinates"] = convert_coords(g.get("coordinates"))
        if g.get("type") == "GeometryCollection":
            g["geometries"] = [convert_geom(x) for x in g.get("geometries", [])]
        return g

    def convert_feature(feat):
        f = dict(feat)
        if f.get("geometry"):
            f["geometry"] = convert_geom(f["geometry"])
        return f

    data = dict(geojson)
    if data.get("type") == "FeatureCollection":
        data["features"] = [convert_feature(x) for x in data.get("features", [])]
    elif data.get("type") == "Feature":
        data = convert_feature(data)
    elif data.get("type") in ("Polygon", "MultiPolygon", "LineString",
                              "MultiLineString", "Point", "MultiPoint"):
        data = convert_geom(data)
    data["crs"] = {"type": "name",
                   "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}
    return data
