"""
数据获取 Skill (Planetary Computer 版本)

从 Microsoft Planetary Computer 下载遥感数据：
    - Landsat 8/9 Collection 2 Level-2 (地表温度 lwir11 + qa_pixel)
    - Sentinel-2 Level-2A (多光谱 + SCL)
    - DEM (Copernicus GLO-30)

Planetary Computer 是微软托管的公开数据目录，无需注册即可免费下载。
"""

import os
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import numpy as np

from ..base_skill import BaseSkill, SkillParameter, SkillResult

# 项目根目录
_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Planetary Computer STAC API
_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"

# 超时设置
_REQUEST_TIMEOUT = 120  # 下载单个文件超时(秒)
_INIT_TIMEOUT = 10      # API 搜索超时(秒)


class DataAcquisitionSkill(BaseSkill):
    """从 Planetary Computer 下载遥感数据（Landsat, Sentinel-2, DEM）"""

    @property
    def name(self) -> str:
        return "data_acquisition"

    @property
    def group(self) -> str:
        return "data_process"

    @property
    def description(self) -> str:
        return "从 Microsoft Planetary Computer 下载 Landsat 8/9 L2 ST、Sentinel-2 L2A 多光谱、QA/SCL 和 DEM 数据到本地目录。无需注册，免费下载。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="region",
                type="string",
                description="研究区域（GeoJSON文件路径 或 lon_min,lat_min,lon_max,lat_max 范围字符串）",
                required=True,
            ),
            SkillParameter(
                name="start_date",
                type="string",
                description="起始日期，格式 YYYY-MM-DD",
                required=True,
            ),
            SkillParameter(
                name="end_date",
                type="string",
                description="结束日期，格式 YYYY-MM-DD",
                required=True,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="cloud_threshold",
                type="number",
                description="云覆盖阈值（百分比），默认 20",
                required=False,
                default=20,
            ),
            SkillParameter(
                name="dem_source",
                type="string",
                description="DEM数据源，可选 'copernicus' 或 'srtm'",
                required=False,
                default="copernicus",
                choices=["copernicus", "srtm"],
            ),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "region": "研究区域GeoJSON/范围",
            "start_date": "起始日期 YYYY-MM-DD",
            "end_date": "结束日期 YYYY-MM-DD",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "landsat_path": "Landsat ST_B10 栅格路径",
            "sentinel2_path": "Sentinel-2 L2A 多光谱栅格路径",
            "qa_path": "Landsat QA_PIXEL 栅格路径",
            "scl_path": "Sentinel-2 SCL 栅格路径",
            "dem_path": "DEM 栅格路径",
            "image_pairs": "影像配对信息",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行数据下载流程。"""
        region = params.get("region", "")
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        output_dir = params.get("output_dir", "")
        cloud_threshold = params.get("cloud_threshold", 50)
        dem_source = params.get("dem_source", "copernicus")

        if not region:
            return SkillResult(success=False, message="参数 region 不能为空")
        if not start_date or not end_date:
            return SkillResult(success=False, message="参数 start_date 和 end_date 不能为空")
        if not output_dir:
            return SkillResult(success=False, message="参数 output_dir 不能为空")

        os.makedirs(output_dir, exist_ok=True)

        # 检查依赖
        try:
            import rasterio
            from rasterio.warp import transform_bounds
        except ImportError:
            return SkillResult(
                success=False,
                message="未安装 rasterio，请运行: pip install rasterio",
            )

        try:
            from pystac_client import Client
        except ImportError:
            return SkillResult(
                success=False,
                message="未安装 pystac-client，请运行: pip install pystac-client",
            )

        if progress_callback:
            progress_callback("data_acquisition", 0.0, "连接 Planetary Computer...")

        try:
            catalog = Client.open(_STAC_URL, headers={"Accept": "application/json"})
        except Exception as e:
            return SkillResult(
                success=False,
                message=f"无法连接 Planetary Computer: {e}\n请检查网络连接",
            )

        # 解析区域边界框
        bbox = self._parse_region(region)
        if bbox is None:
            return SkillResult(
                success=False,
                message=f"无法解析区域参数: {region}，请提供GeoJSON文件路径或 'lon_min,lat_min,lon_max,lat_max'",
            )

        if log_callback:
            log_callback("INFO", f"研究区域: {region}")
            log_callback("INFO", f"边界框: [{bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}]")
            log_callback("INFO", f"时间范围: {start_date} ~ {end_date}")
            log_callback("INFO", f"云覆盖阈值: {cloud_threshold}%（100% = 不过滤）")

        # 研究区 GeoJSON 文件路径（用于多边形裁剪）
        study_area_geojson = region if os.path.isfile(region) else None

        output_paths: Dict[str, str] = {}
        t_start = time.time()
        landsat_items = []
        sentinel2_items = []

        # ── 判断模式 ────────────────────────────────────────────────
        selected_pair = params.get("selected_pair", None)

        # ── 搜索影像（搜索模式和下载模式都需要）─────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.05, "搜索 Landsat 8/9 L2 影像...")

        try:
            landsat_search = catalog.search(
                collections=["landsat-c2-l2"],
                bbox=bbox,
                datetime=f"{start_date}/{end_date}",
                query={"eo:cloud_cover": {"lt": cloud_threshold}},
                max_items=100,
            )
            landsat_items = list(landsat_search.items())
            if log_callback and not selected_pair:
                log_callback("INFO", f"找到 {len(landsat_items)} 景 Landsat 影像")
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"Landsat 搜索失败: {e}")

        if progress_callback:
            progress_callback("data_acquisition", 0.10, "搜索 Sentinel-2 L2A 影像...")

        try:
            s2_search = catalog.search(
                collections=["sentinel-2-l2a"],
                bbox=bbox,
                datetime=f"{start_date}/{end_date}",
                query={"eo:cloud_cover": {"lt": cloud_threshold}},
                max_items=100,
            )
            sentinel2_items = list(s2_search.items())
            if log_callback and not selected_pair:
                log_callback("INFO", f"找到 {len(sentinel2_items)} 景 Sentinel-2 影像")
                # 按日期分组显示
                from collections import Counter
                s2_dates = Counter(i.properties.get("datetime", "")[:10] for i in sentinel2_items)
                for dt, cnt in sorted(s2_dates.items()):
                    log_callback("INFO", f"  {dt}: {cnt} 景")
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"Sentinel-2 搜索失败: {e}")

        # ── 搜索模式：构建配对并返回 ──────────────────────────────
        if not selected_pair:
            if log_callback:
                log_callback("INFO", f"开始构建配对: Landsat {len(landsat_items)} 景, Sentinel {len(sentinel2_items)} 景")
                if landsat_items:
                    dates = sorted(set(i.properties.get("datetime","")[:10] for i in landsat_items))
                    log_callback("INFO", f"Landsat 日期: {dates}")
                if sentinel2_items:
                    dates = sorted(set(i.properties.get("datetime","")[:10] for i in sentinel2_items))
                    log_callback("INFO", f"Sentinel 日期: {dates}")
            image_pairs = self._build_pairs(
                landsat_items, sentinel2_items,
                bbox=bbox, study_area_geojson=study_area_geojson,
                log_callback=log_callback,
            )
            if log_callback:
                log_callback("INFO", f"配对结果: {len(image_pairs)} 组")

            elapsed = time.time() - t_start
            return SkillResult(
                success=True,
                message=f"找到 {len(image_pairs)} 组影像配对（耗时 {elapsed:.1f}s）",
                data={
                    "image_pairs": image_pairs,
                    "region": region,
                    "start_date": start_date,
                    "end_date": end_date,
                    "output_dir": output_dir,
                },
            )

        # ── 下载模式：根据用户选择的配对下载 ────────────────────────
        if log_callback:
            lsat_date = selected_pair.get("landsat_date", "")
            s2_date = selected_pair.get("sentinel2_date", "")
            log_callback("INFO", f"用户已选择配对: Landsat {lsat_date} + Sentinel {s2_date}")

        # 筛选仅属于所选日期和卫星的影像
        selected_landsat_date = selected_pair.get("landsat_date", "")
        selected_sentinel_date = selected_pair.get("sentinel2_date", "")
        selected_satellite = selected_pair.get("landsat_satellite", "")

        def _satellite_match(item, target_sat):
            if not target_sat:
                return True
            item_id = item.id.lower()
            if target_sat == "L8":
                return item_id.startswith("lc08") or "landsat_8" in item_id
            elif target_sat == "L9":
                return item_id.startswith("lc09") or "landsat_9" in item_id
            return True

        landsat_items = [i for i in landsat_items
                         if i.properties.get("datetime", "").startswith(selected_landsat_date)
                         and _satellite_match(i, selected_satellite)]
        sentinel2_items = [i for i in sentinel2_items
                           if i.properties.get("datetime", "").startswith(selected_sentinel_date)]

        if log_callback:
            log_callback("INFO", f"筛选后: Landsat {len(landsat_items)} 景, Sentinel {len(sentinel2_items)} 景")

        if not landsat_items or not sentinel2_items:
            return SkillResult(
                success=False,
                message=f"所选配对无对应影像: Landsat={selected_landsat_date}, Sentinel={selected_sentinel_date}",
            )

        # ── 下载 Landsat ST_B10 ──────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.15, "下载 Landsat lwir11 (地表温度)...")

        landsat_path = os.path.join(output_dir, "landsat_lst.tif")
        self._download_composite(
            items=landsat_items,
            band="lwir11",
            output_path=landsat_path,
            bbox=bbox,
            scale=30,
            progress_callback=progress_callback,
            log_callback=log_callback,
            progress_range=(0.15, 0.30),
            skill_name="data_acquisition",
            study_area_geojson=study_area_geojson,
        )
        output_paths["landsat_path"] = landsat_path

        # ── 下载 Landsat qa_pixel ────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.30, "下载 Landsat qa_pixel...")

        qa_path = os.path.join(output_dir, "landsat_qa_pixel.tif")
        self._download_composite(
            items=landsat_items,
            band="qa_pixel",
            output_path=qa_path,
            bbox=bbox,
            scale=30,
            progress_callback=progress_callback,
            log_callback=log_callback,
            progress_range=(0.30, 0.42),
            skill_name="data_acquisition",
            study_area_geojson=study_area_geojson,
        )
        output_paths["qa_path"] = qa_path

        # ── 下载 Sentinel-2 多光谱 ──────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.42, "下载 Sentinel-2 多光谱...")

        sentinel2_path = os.path.join(output_dir, "sentinel2_bands.tif")
        self._download_composite(
            items=sentinel2_items,
            band=["B02", "B03", "B04", "B08", "B11"],
            output_path=sentinel2_path,
            bbox=bbox,
            scale=10,
            progress_callback=progress_callback,
            log_callback=log_callback,
            progress_range=(0.42, 0.62),
            skill_name="data_acquisition",
            study_area_geojson=study_area_geojson,
        )
        output_paths["sentinel2_path"] = sentinel2_path

        # ── 下载 Sentinel-2 SCL ──────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.62, "下载 Sentinel-2 SCL...")

        scl_path = os.path.join(output_dir, "sentinel2_scl.tif")
        self._download_composite(
            items=sentinel2_items,
            band="SCL",
            output_path=scl_path,
            bbox=bbox,
            scale=20,
            progress_callback=progress_callback,
            log_callback=log_callback,
            progress_range=(0.62, 0.72),
            skill_name="data_acquisition",
            study_area_geojson=study_area_geojson,
        )
        output_paths["scl_path"] = scl_path

        # ── 下载 DEM ─────────────────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.72, f"下载 DEM ({dem_source})...")

        dem_path = os.path.join(output_dir, "dem.tif")
        dem_collection = "cop-dem-glo-30" if dem_source.lower() == "copernicus" else "srtm"
        try:
            dem_search = catalog.search(
                collections=[dem_collection],
                bbox=bbox,
                max_items=10,
            )
            dem_items = list(dem_search.items())
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"DEM 搜索失败: {e}")
            dem_items = []

        self._download_composite(
            items=dem_items,
            band="data",
            output_path=dem_path,
            bbox=bbox,
            scale=30,
            progress_callback=progress_callback,
            log_callback=log_callback,
            progress_range=(0.72, 0.95),
            skill_name="data_acquisition",
            study_area_geojson=study_area_geojson,
        )
        output_paths["dem_path"] = dem_path

        elapsed = time.time() - t_start

        if progress_callback:
            progress_callback("data_acquisition", 1.0, f"数据下载完成，耗时 {elapsed:.1f}s")

        return SkillResult(
            success=True,
            message=f"数据下载完成: Landsat、Sentinel-2、DEM，耗时 {elapsed:.1f}s",
            data={
                **output_paths,
                "output_dir": output_dir,
                "image_pairs": image_pairs,
            },
            artifacts=list(output_paths.values()),
        )

    # ── 区域解析 ────────────────────────────────────────────────────

    @staticmethod
    def _parse_region(region: str) -> Optional[List[float]]:
        """解析区域参数，返回 [lon_min, lat_min, lon_max, lat_max]

        支持：
        - GeoJSON 文件路径 (.geojson)
        - Shapefile 路径 (.shp) - 自动转换为 GeoJSON 并提取边界框
        - 边界框字符串: "lon_min,lat_min,lon_max,lat_max"
        """
        if os.path.isfile(region):
            ext = os.path.splitext(region)[1].lower()

            # Shapefile → 自动转换为 GeoJSON
            if ext == ".shp":
                try:
                    import shapefile as shp
                    reader = shp.Reader(region)
                    bounds = reader.bbox  # [minx, miny, maxx, maxy]
                    return list(bounds)
                except ImportError:
                    pass
                except Exception:
                    pass

            # GeoJSON
            if ext in (".geojson", ".json"):
                try:
                    import geopandas as gpd
                    gdf = gpd.read_file(region)
                    return list(gdf.total_bounds)  # [minx, miny, maxx, maxy]
                except ImportError:
                    # 退回到手动解析
                    with open(region, "r", encoding="utf-8") as f:
                        geojson = json.load(f)
                    coords = _extract_coords(geojson)
                    if coords:
                        xs = [c[0] for c in coords]
                        ys = [c[1] for c in coords]
                        return [min(xs), min(ys), max(xs), max(ys)]
                    return None

        # 格式: "lon_min,lat_min,lon_max,lat_max"
        parts = [float(x.strip()) for x in region.split(",")]
        if len(parts) == 4:
            return parts
        return None

    # ── 影像配对 ────────────────────────────────────────────────────

    @staticmethod
    def _build_pairs(landsat_items: list, sentinel2_items: list,
                     bbox: list = None, study_area_geojson: str = None,
                     log_callback=None) -> List[dict]:
        """构建 Landsat-Sentinel2 拼接配对（时间差 ≤ 2天）

        规则：
        - Landsat 8 只和 L8 拼接，L9 只和 L9 拼接
        - 每组 mosaic 覆盖度 ≥ 80% 才合格
        - Sentinel 按日期分组

        返回每对含：每景详情（日期、L8/L9/S2、云量）+ 综合覆盖度
        """
        from datetime import datetime, timedelta

        def _warn(msg):
            if log_callback:
                log_callback("WARN", msg)

        def _satellite_type(item):
            """判断卫星类型（从 item.id 判断，非 collection_id）"""
            item_id = item.id.lower()
            if item_id.startswith("lc08") or "landsat_8" in item_id:
                return "L8"
            elif item_id.startswith("lc09") or "landsat_9" in item_id:
                return "L9"
            return "L?"

        def _scene_info(item):
            dt = item.properties.get("datetime", "?")[:10]
            cloud = item.properties.get("eo:cloud_cover", None)
            return {
                "id": item.id,
                "date": dt,
                "satellite": _satellite_type(item),
                "cloud_cover": round(cloud, 1) if cloud is not None else None,
            }

        def _check_coverage(items_list, geojson_path):
            """计算一组影像拼接后对研究区的覆盖度（0~1）

            用 STAC item 的 geometry（footprint）和 GeoJSON 研究区做多边形求交。
            """
            if not items_list or not geojson_path:
                return 1.0
            try:
                with open(geojson_path, "r", encoding="utf-8") as f:
                    gj = json.load(f)
                study_polys = _extract_polygons(gj)
                if not study_polys:
                    return 1.0

                # 解析研究区多边形为 shapely 对象
                try:
                    from shapely.geometry import shape, MultiPolygon
                    from shapely.ops import unary_union
                except ImportError:
                    return 1.0

                study_regions = []
                for poly_dict in study_polys:
                    try:
                        poly = shape(poly_dict)
                        study_regions.append(poly)
                    except Exception:
                        pass
                if not study_regions:
                    return 1.0
                study_area = unary_union(study_regions)
                study_area_sq = study_area.area if study_area else 0

                # 合并所有 item 的 footprint
                item_polys = []
                for item in items_list:
                    geom = getattr(item, "geometry", None)
                    if geom and geom.get("type"):
                        try:
                            item_poly = shape(geom)
                            item_polys.append(item_poly)
                        except Exception:
                            pass
                if not item_polys:
                    return 1.0

                mosaic_footprint = unary_union(item_polys)
                intersection = mosaic_footprint.intersection(study_area)
                covered_sq = intersection.area if intersection else 0

                if study_area_sq <= 0:
                    return 1.0

                ratio = covered_sq / study_area_sq
                return min(1.0, max(0.0, ratio))
            except Exception as e:
                _warn(f"覆盖度计算失败: {e}")
                return 1.0

        def _group_by_date_and_satellite(items, is_landsat=True):
            """按日期 + 卫星(L8/L9)分组"""
            groups = {}
            for item in items:
                dt_str = item.properties.get("datetime", "")
                if not dt_str:
                    continue
                date_key = dt_str[:10]
                sat = _satellite_type(item) if is_landsat else "S2"
                group_key = f"{date_key}_{sat}" if is_landsat else date_key
                if group_key not in groups:
                    groups[group_key] = {"date": date_key, "satellite": sat if is_landsat else "S2", "items": []}
                groups[group_key]["items"].append(item)
            return list(groups.values())

        landsat_groups = _group_by_date_and_satellite(landsat_items, is_landsat=True)
        sentinel_groups = _group_by_date_and_satellite(sentinel2_items, is_landsat=False)

        _warn(f"分组结果: {len(landsat_groups)} 个 Landsat 组, {len(sentinel_groups)} 个 Sentinel 组")
        for g in landsat_groups:
            _warn(f"  Landsat 组: {g['date']} {g['satellite']} ({len(g['items'])} 景)")
        for g in sentinel_groups:
            _warn(f"  Sentinel 组: {g['date']} ({len(g['items'])} 景)")

        pairs = []
        for lg in landsat_groups:
            l_date = lg["date"]
            l_sat = lg["satellite"]
            l_items = lg["items"]
            l_scenes = [_scene_info(i) for i in l_items]

            # Landsat 覆盖度
            l_coverage = _check_coverage(l_items, study_area_geojson)
            if l_coverage < 0.7:
                _warn(f"Landsat {l_date} ({l_sat}) 覆盖度 {l_coverage*100:.0f}% < 70%，跳过")
                continue  # 覆盖度不足

            l_clouds = [i.properties.get("eo:cloud_cover", 0) or 0 for i in l_items]
            avg_l_cloud = round(sum(l_clouds) / len(l_clouds), 1)

            l_dt = datetime.fromisoformat(l_date)
            for sg in sentinel_groups:
                s_date = sg["date"]
                s_items = sg["items"]
                s_scenes = [_scene_info(i) for i in s_items]

                _warn(f"Sentinel {s_date}: {len(s_items)} 景, 覆盖度检查前")
                s_dt = datetime.fromisoformat(s_date)
                if abs((l_dt - s_dt).days) > 2:
                    continue

                # Sentinel 覆盖度
                s_coverage = _check_coverage(s_items, study_area_geojson)
                if s_coverage < 0.7:
                    _warn(f"Sentinel {s_date} 覆盖度 {s_coverage*100:.0f}% < 70%，跳过")
                    continue

                s_clouds = [i.properties.get("eo:cloud_cover", 0) or 0 for i in s_items]
                avg_s_cloud = round(sum(s_clouds) / len(s_clouds), 1)

                pairs.append({
                    "landsat_date": l_date,
                    "landsat_satellite": l_sat,
                    "landsat_count": len(l_scenes),
                    "landsat_scenes": l_scenes,
                    "landsat_cloud_cover": avg_l_cloud,
                    "landsat_coverage": round(l_coverage * 100, 1),
                    "sentinel2_date": s_date,
                    "sentinel2_count": len(s_scenes),
                    "sentinel2_scenes": s_scenes,
                    "sentinel2_cloud_cover": avg_s_cloud,
                    "sentinel2_coverage": round(s_coverage * 100, 1),
                    "time_diff_days": abs((l_dt - s_dt).days),
                })
        return pairs

    # ── 下载与合成 ──────────────────────────────────────────────────

    def _download_composite(
        self,
        items: list,
        band,
        output_path: str,
        bbox: List[float],
        scale: float,
        progress_callback,
        log_callback,
        progress_range: tuple,
        skill_name: str,
        study_area_geojson: str = None,
    ):
        """下载影像、mosaic合并多景、按研究区裁剪，写入 GeoTIFF

        内存优化：逐波段处理，每个波段独立下载→mosaic→写入→释放。
        多景时用 "第一个有效值" 策略合并（云量最小的场景优先）。
        """
        import rasterio
        from rasterio.warp import transform_bounds, reproject, Resampling
        from rasterio.io import MemoryFile
        from rasterio.windows import from_bounds as window_from_bounds
        from rasterio.transform import from_bounds as transform_from_bounds
        from rasterio.features import geometry_mask

        if not items:
            if log_callback:
                log_callback("ERROR", f"未找到任何 {band} 影像，无法继续后续流程")
            raise RuntimeError(f"未找到任何 {band} 影像，请检查时间范围或云量阈值")

        bands = band if isinstance(band, list) else [band]
        band_count = len(bands)
        p_min, p_max = progress_range

        # 按云量排序，云最小的优先
        items_sorted = sorted(
            items,
            key=lambda x: x.properties.get("eo:cloud_cover", 100) or 100,
        )

        # 目标 CRS 和分辨率下的输出尺寸（先用 EPSG:4326 下载，之后再转 UTM）
        dst_crs = "EPSG:4326"
        dst_width = int((bbox[2] - bbox[0]) / (scale / 111320.0))
        dst_height = int((bbox[3] - bbox[1]) / (scale / 111320.0))
        dst_transform = transform_from_bounds(bbox[0], bbox[1], bbox[2], bbox[3],
                                               dst_width, dst_height)

        # 判断 Landsat（用于 src_nodata 和 footprint mask）
        _is_landsat = any(b in bands for b in ["lwir11", "qa_pixel", "ST_B10", "QA_PIXEL"])

        # 预计算 footprint 掩膜（仅 Landsat 需要）
        # S2: tile 边缘的 0 值通过 reproject 后的邻域检测处理，不用 footprint mask（会切掉重叠区）
        footprint_masks = []
        for item in items_sorted:
            geom = getattr(item, "geometry", None)
            mask = None
            if geom and geom.get("type") and _is_landsat:
                try:
                    from shapely.geometry import shape
                    item_poly = shape(geom)
                    mask = geometry_mask(
                        [item_poly], transform=dst_transform, invert=True,
                        out_shape=(dst_height, dst_width),
                    )
                except Exception:
                    pass
            footprint_masks.append(mask)

        # 预计算研究区裁剪掩膜（一次性，后续复用）
        clip_mask = None
        if study_area_geojson and os.path.isfile(study_area_geojson):
            try:
                with open(study_area_geojson, "r", encoding="utf-8") as f:
                    geojson_data = json.load(f)
                polygons = _extract_polygons(geojson_data)
                if polygons:
                    clip_mask = geometry_mask(
                        polygons, transform=dst_transform, invert=True,
                        out_shape=(dst_height, dst_width),
                    )
            except Exception:
                pass

        download_errors = []

        # 创建输出文件（先创建，逐波段写入）
        profile = {
            "driver": "GTiff",
            "height": dst_height,
            "width": dst_width,
            "count": band_count,
            "dtype": np.float32,
            "crs": dst_crs,
            "transform": dst_transform,
            "compress": "lzw",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "nodata": np.nan,
        }
        dst_ds = rasterio.open(output_path, "w", **profile)

        try:
            # ── 逐波段处理：每个波段独立下载→mosaic→写入→释放 ──
            for band_idx, b in enumerate(bands):
                band_arrays = []  # 该波段的所有景 reproject 后的数组

                for item_idx, item in enumerate(items_sorted):
                    progress = p_min + (p_max - p_min) * (band_idx / band_count + 0.5 / band_count * item_idx / max(len(items_sorted), 1))
                    if progress_callback:
                        progress_callback(skill_name, progress, f"下载 {b} / {item.id}")

                    signed_item = self._sign_item(item)
                    if not signed_item:
                        download_errors.append(f"{item.id}: 签名失败")
                        continue

                    asset = signed_item.assets.get(b)
                    if not asset:
                        download_errors.append(f"{item.id}: 缺少波段 {b}")
                        continue

                    data_bytes = self._fetch_asset(asset.href, log_callback)
                    if data_bytes is None:
                        download_errors.append(f"{item.id}/{b}: 下载失败")
                        continue

                    try:
                        with MemoryFile(data_bytes) as memfile:
                            with memfile.open() as src:
                                src_crs = src.crs
                                if src_crs and src_crs.to_string() != "EPSG:4326":
                                    bbox_src = transform_bounds("EPSG:4326", src_crs, *bbox)
                                else:
                                    bbox_src = bbox

                                try:
                                    window = window_from_bounds(*bbox_src, src.transform)
                                    window = window.round_offsets().round_shape()
                                    from rasterio.windows import Window
                                    window = window.intersection(
                                        Window(0, 0, src.width, src.height))
                                    if window.width < 1 or window.height < 1:
                                        continue
                                    src_arr = src.read(1, window=window)
                                    src_transform_clip = src.window_transform(window)
                                except Exception:
                                    src_arr = src.read(1)
                                    src_transform_clip = src.transform

                                _resampling = Resampling.bilinear if b != "SCL" else Resampling.nearest
                                dst_arr = np.full(
                                    (dst_height, dst_width), np.nan, dtype=np.float32)
                                if _is_landsat:
                                    # Landsat: nodata=0 + footprint mask
                                    reproject(
                                        source=src_arr, destination=dst_arr,
                                        src_transform=src_transform_clip, src_crs=src_crs,
                                        src_nodata=0,
                                        dst_transform=dst_transform, dst_crs=dst_crs,
                                        resampling=_resampling,
                                    )
                                    if footprint_masks[item_idx] is not None:
                                        dst_arr[~footprint_masks[item_idx]] = np.nan
                                else:
                                    # S2: src_nodata=0 + 无 footprint mask（避免重叠区被切）
                                    reproject(
                                        source=src_arr, destination=dst_arr,
                                        src_transform=src_transform_clip, src_crs=src_crs,
                                        src_nodata=0,
                                        dst_transform=dst_transform, dst_crs=dst_crs,
                                        resampling=_resampling,
                                    )
                                band_arrays.append(dst_arr)
                    except Exception as e:
                        download_errors.append(f"{item.id}/{b}: {e}")

                # mosaic 该波段（第一个有效值策略）
                if not band_arrays:
                    # 没有数据的波段，写全 NaN
                    nan_arr = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
                    if clip_mask is not None:
                        nan_arr[~clip_mask] = np.nan
                    dst_ds.write(nan_arr, band_idx + 1)
                    continue

                if len(band_arrays) == 1:
                    mosaic_arr = band_arrays[0]
                else:
                    mosaic_arr = band_arrays[0].copy()
                    for j in range(1, len(band_arrays)):
                        nan_mask = np.isnan(mosaic_arr)
                        if np.any(nan_mask):
                            mosaic_arr[nan_mask] = band_arrays[j][nan_mask]

                # 研究区裁剪
                if clip_mask is not None:
                    mosaic_arr[~clip_mask] = np.nan

                # 写入该波段
                dst_ds.write(mosaic_arr.astype(np.float32), band_idx + 1)

                # 释放该波段的内存
                band_arrays.clear()
                del band_arrays

        finally:
            dst_ds.close()

        # 后处理：将 EPSG:4326 转换到 UTM 投影
        try:
            self._reproject_to_utm(output_path, log_callback)
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"UTM 转换失败（保留 4326 版本）: {e}")

        if download_errors and log_callback:
            for err in download_errors[:5]:
                log_callback("WARN", err)
            if len(download_errors) > 5:
                log_callback("WARN", f"...还有 {len(download_errors)-5} 个错误")

        if log_callback:
            log_callback("INFO", f"已保存 {band}: {output_path} ({band_count} 波段, {len(items_sorted)} 景 mosaic)")

    # ── UTM 转换 ──────────────────────────────────────────────────

    @staticmethod
    def _reproject_to_utm(tif_path: str, log_callback=None):
        """将 GeoTIFF 从 EPSG:4326 转换到合适的 UTM 投影（原地替换）"""
        import rasterio
        from rasterio.warp import calculate_default_transform, reproject, Resampling

        with rasterio.open(tif_path) as src:
            src_crs = src.crs
            if not src_crs or src_crs.to_epsg() != 4326:
                return  # 已经不是 4326，跳过

            # 根据中心经度计算 UTM zone
            bounds = src.bounds
            center_lon = (bounds.left + bounds.right) / 2
            center_lat = (bounds.bottom + bounds.top) / 2
            utm_zone = int((center_lon + 180) / 6) + 1
            if center_lat >= 0:
                dst_crs = f"EPSG:326{utm_zone:02d}"
            else:
                dst_crs = f"EPSG:327{utm_zone:02d}"

            if log_callback:
                log_callback("INFO", f"UTM 转换: {src_crs} → {dst_crs}")

            # 计算变换参数
            dst_transform, dst_width, dst_height = calculate_default_transform(
                src_crs, dst_crs, src.width, src.height, *src.bounds,
            )
            dst_profile = src.profile.copy()
            dst_profile.update(
                crs=dst_crs,
                transform=dst_transform,
                width=dst_width,
                height=dst_height,
            )

            # 先写到临时文件，再替换原文件
            tmp_path = tif_path + ".utm.tif"
            with rasterio.open(tmp_path, "w", **dst_profile) as dst:
                for i in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, i),
                        destination=rasterio.band(dst, i),
                        src_transform=src.transform,
                        src_crs=src_crs,
                        dst_transform=dst_transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.nearest,
                    )

        # 替换原文件
        os.replace(tmp_path, tif_path)

    # ── 下载 asset ──────────────────────────────────────────────────

    @staticmethod
    def _fetch_asset(url: str, log_callback) -> Optional[bytes]:
        """下载 asset 原始字节，带重试"""
        import time as _time
        for attempt in range(3):
            t0 = _time.time()
            try:
                if log_callback and attempt == 0:
                    log_callback("INFO", f"  开始下载 ({_REQUEST_TIMEOUT}s超时): {url[:70]}...")
                resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
                elapsed = _time.time() - t0
                if log_callback:
                    log_callback("INFO", f"  下载完成 ({elapsed:.1f}s, {len(resp.content)/1024/1024:.1f}MB)")
                resp.raise_for_status()
                return resp.content
            except requests.exceptions.Timeout:
                elapsed = _time.time() - t0
                if log_callback:
                    log_callback("WARN", f"  下载超时 ({elapsed:.0f}s): {url[:70]}...")
                if attempt == 2:
                    return None
                time.sleep(2)
            except requests.exceptions.RequestException as e:
                elapsed = _time.time() - t0
                if log_callback:
                    log_callback("WARN", f"  下载失败 ({elapsed:.1f}s, 第{attempt+1}次): {e}")
                if attempt == 2:
                    return None
                time.sleep(2)
        return None

    # ── SAS Token 签名 ──────────────────────────────────────────────

    @staticmethod
    def _sign_item(item):
        """为 STAC Item 签名，添加 SAS token 到 asset URL

        Planetary Computer 要求对 Blob 存储的 asset 进行 SAS token 签名。
        如果未安装 planetary_computer 包，则使用内部 API 手动签名。
        """
        try:
            import planetary_computer
            return planetary_computer.sign(item)
        except ImportError:
            pass

        # 手动签名：调用 SAS API
        try:
            collection_id = item.collection_id
            item_id = item.id
            sas_url = f"{_SAS_URL}/{collection_id}/{item_id}"
            resp = requests.get(sas_url, timeout=10)
            resp.raise_for_status()
            sas_data = resp.json()

            # 将 SAS token 附加到每个 asset URL
            import copy
            from pystac import Item as STACItem
            signed_item = copy.deepcopy(item)
            for key, asset in signed_item.assets.items():
                if hasattr(asset, 'href') and asset.href:
                    token = sas_data.get(key, sas_data.get("token", ""))
                    if token:
                        sep = "&" if "?" in asset.href else "?"
                        asset.href = f"{asset.href}{sep}{token}"
            return signed_item
        except Exception:
            # 某些数据可能不需要签名
            return item


def _extract_coords(geojson: dict) -> Optional[List[List[float]]]:
    """从 GeoJSON 中递归提取所有坐标"""
    gtype = geojson.get("type", "")

    if gtype == "FeatureCollection":
        features = geojson.get("features", [])
        if not features:
            return None
        return _extract_coords(features[0])

    if gtype == "Feature":
        geometry = geojson.get("geometry", {})
        return _extract_coords(geometry)

    if gtype in ("Polygon",):
        return geojson.get("coordinates", [[[]]])[0]

    if gtype == "MultiPolygon":
        return geojson.get("coordinates", [[[[]]]])[0][0]

    if gtype == "Point":
        return [geojson.get("coordinates", [])]

    return None


def _extract_polygons(geojson: dict) -> List:
    """从 GeoJSON 中提取所有多边形（返回 GeoJSON 格式 dict，用于 rasterio geometry_mask）"""
    gtype = geojson.get("type", "")

    if gtype == "FeatureCollection":
        polys = []
        for f in geojson.get("features", []):
            polys.extend(_extract_polygons(f))
        return polys

    if gtype == "Feature":
        return _extract_polygons(geojson.get("geometry", {}))

    if gtype == "Polygon":
        return [{"type": "Polygon", "coordinates": geojson.get("coordinates", [])}]

    if gtype == "MultiPolygon":
        # 每个子多边形独立返回
        return [{"type": "Polygon", "coordinates": coords} for coords in geojson.get("coordinates", [[]])]

    return []
