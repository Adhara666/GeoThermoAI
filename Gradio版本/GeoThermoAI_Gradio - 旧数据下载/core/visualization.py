"""
GeoThermoAI 可视化模块

基于 Folium 的多图层交互式地图，支持 GeoTIFF 渲染（UTM→WGS84 转换）。
参考升级规划 v2 第 3.7 节 LayerVisualizer 设计。
"""

import os
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
    """管理所有可用于 Folium 地图的数据图层

    每个图层定义包含：
    - id: 唯一标识
    - label: 显示名称
    - file: 相对项目目录的文件路径
    - band: 单波段索引（从 1 开始）
    - bands: 多波段 RGB 列表（如 [4, 3, 2]）
    - colormap: matplotlib 色带名（如 "RdYlBu_r"）
    - opacity: 默认不透明度
    - visible: 默认是否可见
    """

    LAYER_DEFS = [
        {
            "id": "landsat_lst",
            "label": "Landsat LST 30m",
            "file": "raw/landsat_lst.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "visible": False,
        },
        {
            "id": "sentinel_rgb",
            "label": "Sentinel-2 真彩色",
            "file": "raw/sentinel2_bands.tif",
            "bands": [4, 3, 2],
            "scale_factor": 1.0 / 10000.0,
            "opacity": 0.8,
            "visible": False,
        },
        {
            "id": "dem",
            "label": "DEM 地形",
            "file": "raw/dem.tif",
            "band": 1,
            "colormap": "terrain",
            "opacity": 0.6,
            "visible": False,
        },
        {
            "id": "lst_10m",
            "label": "LST 10m 结果",
            "file": "results/rf_10m_lst_final.tif",
            "band": 1,
            "colormap": "RdYlBu_r",
            "opacity": 0.7,
            "visible": True,
        },
    ]

    # ── 内部工具 ───────────────────────────────────────────────

    @staticmethod
    def _bounds_to_wgs84(src) -> List[List[float]]:
        """将 rasterio 数据源的 bounds 从源 CRS 转换到 WGS84 (EPSG:4326)

        返回 [[south, west], [north, east]] 供 folium ImageOverlay 使用
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
    def _read_band(tif_path: str, band: int = 1, colormap: Optional[str] = None,
                    max_size: int = 1024) -> Optional[Tuple[np.ndarray, List[List[float]]]]:
        """读取单波段，返回 RGBA 数组 + WGS84 边界

        max_size 限制最大边长，避免大图导致前端卡顿
        """
        if not HAS_RASTERIO:
            return None
        with rasterio.open(tif_path) as src:
            h_orig, w_orig = src.height, src.width
            step = max(1, max(h_orig, w_orig) // max_size)
            out_h, out_w = (h_orig + step - 1) // step, (w_orig + step - 1) // step
            arr = src.read(band, out_shape=(out_h, out_w)).astype(np.float32)
            nodata = src.nodata
            if nodata is not None:
                arr = np.where(arr == nodata, np.nan, arr)
            bounds = LayerVisualizer._bounds_to_wgs84(src)

        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            return None, bounds
        vmin = float(np.nanmin(valid))
        vmax = float(np.nanmax(valid))
        if vmax <= vmin:
            vmax = vmin + 1.0
        normed = np.clip((arr - vmin) / (vmax - vmin + 1e-10), 0, 1)

        if colormap and HAS_CM:
            try:
                cmap = getattr(cm, colormap)
                rgba = (cmap(normed) * 255).astype(np.uint8)
            except Exception:
                gray = (normed * 255).astype(np.uint8)
                rgba = np.stack([gray, gray, gray, np.full_like(gray, 200)], axis=-1)
        else:
            gray = (normed * 255).astype(np.uint8)
            rgba = np.stack([gray, gray, gray, np.full_like(gray, 200)], axis=-1)

        # 无效像素透明
        nan_mask = np.isnan(arr)
        if nan_mask.any:
            rgba[nan_mask, 3] = 0
        return rgba, bounds

    @staticmethod
    def _read_rgb(tif_path: str, bands: List[int],
                  scale_factor: float = 1.0 / 10000.0,
                  max_size: int = 1024) -> Optional[Tuple[np.ndarray, List[List[float]]]]:
        """读取多波段生成 RGB 真彩色"""
        if not HAS_RASTERIO:
            return None
        with rasterio.open(tif_path) as src:
            h_orig, w_orig = src.height, src.width
            step = max(1, max(h_orig, w_orig) // max_size)
            out_h, out_w = (h_orig + step - 1) // step, (w_orig + step - 1) // step
            rgb = []
            for b in bands:
                arr = src.read(b, out_shape=(out_h, out_w)).astype(np.float32)
                arr = arr * scale_factor
                arr = np.clip(arr, 0, 1)
                rgb.append(arr)
            bounds = LayerVisualizer._bounds_to_wgs84(src)

        colored = (np.stack(rgb, axis=-1) * 255).astype(np.uint8)
        alpha = np.full(colored.shape[:2], 220, dtype=np.uint8)
        rgba = np.concatenate([colored, alpha[..., None]], axis=-1)
        return rgba, bounds

    # ── 公开接口 ───────────────────────────────────────────────

    @staticmethod
    def list_available_layers(project_dir: str) -> List[dict]:
        """列出项目目录下所有图层及其可用性"""
        result = []
        for layer_def in LayerVisualizer.LAYER_DEFS:
            file_path = os.path.join(project_dir, layer_def["file"]) if project_dir else ""
            result.append({
                "id": layer_def["id"],
                "label": layer_def["label"],
                "visible": layer_def.get("visible", False),
                "available": bool(file_path) and os.path.isfile(file_path),
                "path": file_path,
            })
        return result

    @staticmethod
    def build_map(project_dir: str) -> str:
        """生成包含所有可用图层的 Folium 地图 HTML

        若 project_dir 为空或无可用图层，返回占位 HTML
        """
        if not HAS_FOLIUM:
            return "<p style='color:#888;padding:12px'>⚠️ 未安装 folium，无法渲染地图。请运行: pip install folium</p>"

        if not project_dir or not os.path.isdir(project_dir):
            m = folium.Map(location=_DEFAULT_CENTER, zoom_start=10, control_scale=True)
            folium.LayerControl(collapsed=False).add_to(m)
            return m._repr_html_()

        m = folium.Map(control_scale=True, zoom_start=11)
        first_layer = True

        for layer_def in LayerVisualizer.LAYER_DEFS:
            file_path = os.path.join(project_dir, layer_def["file"])
            if not os.path.isfile(file_path):
                continue
            try:
                if "bands" in layer_def:
                    result = LayerVisualizer._read_rgb(
                        file_path, layer_def["bands"],
                        scale_factor=layer_def.get("scale_factor", 1.0 / 10000.0),
                    )
                else:
                    result = LayerVisualizer._read_band(
                        file_path, layer_def.get("band", 1),
                        layer_def.get("colormap"),
                    )
                if result is None:
                    continue
                img, bounds = result

                if first_layer:
                    center_lat = (bounds[0][0] + bounds[1][0]) / 2
                    center_lon = (bounds[0][1] + bounds[1][1]) / 2
                    m.location = [center_lat, center_lon]
                    first_layer = False

                ImageOverlay(
                    image=img.tolist() if hasattr(img, "tolist") else img,
                    bounds=bounds,
                    opacity=layer_def.get("opacity", 0.7),
                    name=layer_def["label"],
                    show=layer_def.get("visible", True),
                ).add_to(m)
            except Exception:
                continue

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
