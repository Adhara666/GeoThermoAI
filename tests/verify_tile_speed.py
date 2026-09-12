# -*- coding: utf-8 -*-
"""真实 10m LST 产物：原图直读 vs 概览副本 基准（可删除）。"""
import sys
import time

sys.path.insert(0, "/app")

import rasterio  # noqa: E402
import core.visualization as viz  # noqa: E402
from core.visualization import LayerVisualizer  # noqa: E402

FILE = ("/app/data/users/Adhara/workspace/最后测试/convs/8e55b8d22ffe/pairs/"
        "L20240721_S20240722/results/rf_10m_lst_final_20240722.tif")
with rasterio.open(FILE) as src:
    print(f"真实文件: {src.width}x{src.height} = "
          f"{src.width*src.height/1e6:.1f}M 像素, dtype={src.dtypes[0]}, crs={src.crs}")
    b = src.bounds
    cx = (b.left + b.right) / 2
    cy = (b.bottom + b.top) / 2
    from rasterio.warp import transform as _wtransform
    lon, lat = _wtransform(src.crs, "EPSG:4326", [cx], [cy])
    lon, lat = lon[0], lat[0]
    print(f"中心经纬度: {lon:.4f}, {lat:.4f}")

# 计算文件中心对应的 z=11/12/13 瓦片
import math  # noqa: E402


def merc_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_r) + 1/math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


lon = lon
lat = lat
mtime = __import__("os").path.getmtime(FILE)


def bench(zxy, label):
    # 预热一片（含首次 open / 统计 / 概览构建），再计平均
    LayerVisualizer._render_tile_cached(
        "lst_10m", FILE, mtime, zxy[0][0], zxy[0][1], zxy[0][2])
    t0 = time.perf_counter()
    n = 0
    for z, x, y in zxy:
        LayerVisualizer._render_tile_cached("lst_10m", FILE, mtime, z, x, y)
        n += 1
    dt = time.perf_counter() - t0
    print(f"  {label}: {n} 瓦片共 {dt*1000:.0f} ms（平均 {dt/n*1000:.0f} ms/片，已预热）")
    return dt / n


import core.visualization as _viz2  # noqa: E402

for z in (11, 12, 13, 14, 15):
    x, y = merc_tile(lon, lat, z)
    tiles = [(z, x + i % 3, y + i // 3) for i in range(6)]
    print(f"== z={z} ==")
    # 原图组：强制绕过概览（预热片也计入预热，不污染计时）
    _viz2._OVERVIEW_MAX_Z = -1
    LayerVisualizer._render_tile_cached.cache_clear()
    t_src = bench(tiles, "原图直读")
    # 概览组
    _viz2._OVERVIEW_MAX_Z = 15
    LayerVisualizer._render_tile_cached.cache_clear()
    t_ov = bench(tiles, "概览副本")
    speed = t_src / t_ov if t_ov else 0
    print(f"  → 加速 {speed:.1f}x")
