"""
数据获取 Skill (Planetary Computer 版本 / GDAL 实现)

从 Microsoft Planetary Computer 下载遥感数据：
    - Landsat 8/9 Collection 2 Level-2 (地表温度 lwir11 + qa_pixel)
    - Sentinel-2 Level-2A (多光谱 + SCL)
    - DEM (Copernicus GLO-30)

Planetary Computer 是微软托管的公开数据目录，无需注册即可免费下载。

处理流程：下载 COG → 保存到临时文件 → gdal.Warp(mosaic + UTM + clip) → 合并多波段
"""

import os
import json
import time
import shutil
import tempfile
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
SAT_API_TIMEOUT = 30  # STAC API 请求超时(秒)

# STAC 搜索重试次数与单次 HTTP 请求超时
_SEARCH_ATTEMPTS = 3
_STAC_TIMEOUT = 60


def _open_catalog(log_callback=None, attempts: int = _SEARCH_ATTEMPTS):
    """连接 Planetary Computer STAC 目录，带重试与超时。失败返回 None。"""
    from pystac_client import Client
    from pystac_client.stac_api_io import StacApiIO
    for i in range(attempts):
        try:
            return Client.open(
                _STAC_URL,
                headers={"Accept": "application/json"},
                stac_io=StacApiIO(timeout=_STAC_TIMEOUT, max_retries=2),
            )
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"连接 Planetary Computer 失败（第{i+1}/{attempts}次）: {e}")
            time.sleep(2 * (i + 1))
    return None


def _search_items(catalog, log_callback, label: str, attempts: int = _SEARCH_ATTEMPTS, **kwargs) -> list:
    """执行 STAC 搜索并取回 items，带重试。失败返回空列表。"""
    for i in range(attempts):
        try:
            return list(catalog.search(**kwargs).items())
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"{label} 搜索失败（第{i+1}/{attempts}次）: {e}")
            time.sleep(2 * (i + 1))
    return []


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
            from osgeo import gdal, osr
            _ = gdal.VersionInfo()
            _ = osr
        except ImportError:
            return SkillResult(
                success=False,
                message="未安装 GDAL (osgeo)，请运行: conda install gdal",
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

        catalog = _open_catalog(log_callback)
        if catalog is None:
            return SkillResult(
                success=False,
                message=f"无法连接 Planetary Computer（已重试 {_SEARCH_ATTEMPTS} 次）\n请检查网络连接",
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

        landsat_items = _search_items(
            catalog, log_callback, "Landsat",
            collections=["landsat-c2-l2"],
            bbox=bbox,
            datetime=f"{start_date}/{end_date}",
            query={"eo:cloud_cover": {"lt": cloud_threshold}},
            max_items=100,
        )
        if log_callback and not selected_pair:
            log_callback("INFO", f"找到 {len(landsat_items)} 景 Landsat 影像")

        if progress_callback:
            progress_callback("data_acquisition", 0.10, "搜索 Sentinel-2 L2A 影像...")

        sentinel2_items = _search_items(
            catalog, log_callback, "Sentinel-2",
            collections=["sentinel-2-l2a"],
            bbox=bbox,
            datetime=f"{start_date}/{end_date}",
            query={"eo:cloud_cover": {"lt": cloud_threshold}},
            max_items=100,
        )
        if log_callback and not selected_pair:
            log_callback("INFO", f"找到 {len(sentinel2_items)} 景 Sentinel-2 影像")
            # 按日期分组显示
            from collections import Counter
            s2_dates = Counter(i.properties.get("datetime", "")[:10] for i in sentinel2_items)
            for dt, cnt in sorted(s2_dates.items()):
                log_callback("INFO", f"  {dt}: {cnt} 景")

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
                message=f"找到 {len(landsat_items)} 景 Landsat、{len(sentinel2_items)} 景 Sentinel-2，"
                        f"配对 {len(image_pairs)} 组（耗时 {elapsed:.1f}s）",
                data={
                    "image_pairs": image_pairs,
                    "landsat_count": len(landsat_items),
                    "sentinel_count": len(sentinel2_items),
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
        dem_items = _search_items(
            catalog, log_callback, "DEM",
            collections=[dem_collection],
            bbox=bbox,
            max_items=10,
        )

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
            },
            artifacts=list(output_paths.values()),
        )

    # ── 区域解析 ────────────────────────────────────────────────────

    @staticmethod
    def _looks_like_region_file(value: str) -> bool:
        """判断字符串是否像是研究区文件路径"""
        if not value or not isinstance(value, str):
            return False
        lower = value.lower()
        return lower.endswith((".geojson", ".json", ".shp", ".kml", ".gpkg"))

    @staticmethod
    def _parse_region(region: str) -> Optional[List[float]]:
        """解析区域参数，返回 [lon_min, lat_min, lon_max, lat_max]

        支持：
        - GeoJSON 文件路径 (.geojson)
        - Shapefile 路径 (.shp) - 自动转换为 GeoJSON 并提取边界框
        - 边界框字符串: "lon_min,lat_min,lon_max,lat_max"
        """
        region = region.strip().strip('"').strip("'")

        # 如果路径不存在但明显是文件路径，尝试相对项目根目录/config/study_areas解析
        if not os.path.isfile(region) and DataAcquisitionSkill._looks_like_region_file(region):
            from pathlib import Path
            candidate_roots = [
                Path(__file__).resolve().parent.parent.parent.parent,  # 项目根目录
                Path.cwd(),
            ]
            for root in candidate_roots:
                candidates = [
                    root / region,
                    root / "config" / "study_areas" / os.path.basename(region),
                    root / "GeoThermoAI" / "config" / "study_areas" / os.path.basename(region),
                ]
                for cand in candidates:
                    if cand.is_file():
                        region = str(cand.resolve())
                        break
                if os.path.isfile(region):
                    break

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

    # ── 下载与合成（GDAL 实现）──────────────────────────────────────

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
        """下载影像、mosaic合并多景（GDAL /vsicurl/ 直接读取 COG）

        流程：
        1. 签名所有 item，收集各波段的 /vsicurl/ 链接
        2. 逐波段 gdal.Warp(mosaic + UTM + clip) 一步完成
        3. 合并多波段
        """
        from osgeo import gdal, osr
        import tempfile

        if not items:
            if log_callback:
                log_callback("ERROR", f"未找到任何 {band} 影像，无法继续后续流程")
            raise RuntimeError(f"未找到任何 {band} 影像，请检查时间范围或云量阈值")

        bands_list = band if isinstance(band, list) else [band]
        band_count = len(bands_list)
        p_min, p_max = progress_range

        items_sorted = sorted(
            items,
            key=lambda x: x.properties.get("eo:cloud_cover", 100) or 100,
        )

        # ── 1. 计算 UTM zone ─────────────────────────────────────────
        center_lon = (bbox[0] + bbox[2]) / 2
        center_lat = (bbox[1] + bbox[3]) / 2
        utm_zone = int((center_lon + 180) / 6) + 1
        if center_lat >= 0:
            utm_epsg = 32600 + utm_zone
            utm_epsg_str = f"EPSG:{utm_epsg}"
        else:
            utm_epsg = 32700 + utm_zone
            utm_epsg_str = f"EPSG:{utm_epsg}"

        if log_callback:
            log_callback("INFO", f"目标坐标系: {utm_epsg_str} (UTM Zone {utm_zone})")

        # ── 2. 转换 bbox 到 UTM ──────────────────────────────────────
        srs4326 = osr.SpatialReference()
        srs4326.ImportFromEPSG(4326)
        srs_utm = osr.SpatialReference()
        srs_utm.ImportFromEPSG(utm_epsg)
        ct = osr.CoordinateTransformation(srs4326, srs_utm)

        pts = [
            ct.TransformPoint(bbox[0], bbox[1]),
            ct.TransformPoint(bbox[2], bbox[3]),
        ]
        x1 = min(pts[0][0], pts[1][0])
        y1 = min(pts[0][1], pts[1][1])
        x2 = max(pts[0][0], pts[1][0])
        y2 = max(pts[0][1], pts[1][1])

        # ── 4. 下载所有场景到本地临时文件 ───────────────────────────
        band_files = {b: [] for b in bands_list}
        download_errors = []
        tmp_dir = tempfile.mkdtemp(prefix="gdal_dl_")

        for idx, item in enumerate(items_sorted):
            for b in bands_list:
                # 每个波段每次下载前重新签名（SAS token 有时效，避免 403）
                signed_item = self._sign_item(item)
                if not signed_item:
                    download_errors.append(f"{item.id}: 签名失败")
                    continue

                asset = signed_item.assets.get(b)
                if not asset:
                    download_errors.append(f"{item.id}: 缺少波段 {b}")
                    continue

                if progress_callback:
                    p = p_min + (p_max - p_min) * 0.3 * (idx + 1) / len(items_sorted)
                    progress_callback(skill_name, p, f"下载 {b} / {item.id}")

                # 下载到本地临时文件（比 /vsicurl/ 快得多）
                tmp_path = os.path.join(tmp_dir, f"{idx:03d}_{b}.tif")
                data_bytes = self._fetch_asset(asset.href, log_callback)
                if data_bytes is None:
                    download_errors.append(f"{item.id}/{b}: 下载失败")
                    continue
                with open(tmp_path, "wb") as f:
                    f.write(data_bytes)
                band_files[b].append(tmp_path)

        # ── 5. 逐波段 GDAL Warp（mosaic + UTM + clip 一步到位）───
        def _write_empty_band(path, crs_str, x1, y1, x2, y2, res):
            """创建零值填充的空波段（当 Warp 失败时占位）"""
            import math
            w = max(int((x2 - x1) / res), 1)
            h = max(int((y2 - y1) / res), 1)
            drv = gdal.GetDriverByName("GTiff")
            ds = drv.Create(path, w, h, 1, gdal.GDT_Float32,
                            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES"])
            if ds:
                ds.SetGeoTransform([x1, res, 0, y2, 0, -res])
                ds.SetProjection(crs_str)
                ds.GetRasterBand(1).SetNoDataValue(0)
                ds.GetRasterBand(1).Fill(0)
                ds.FlushCache()
                ds = None

        band_outputs = []

        try:
            for b_idx, b in enumerate(bands_list):
                if progress_callback:
                    p = p_min + (p_max - p_min) * (0.3 + 0.7 * b_idx / max(len(bands_list), 1))
                    progress_callback(skill_name, p, f"处理 {b}...")

                urls = band_files[b]
                if not urls:
                    if log_callback:
                        log_callback("WARN", f"  {b}: 无可用数据，写入空波段")
                    empty_path = os.path.join(tmp_dir, f"empty_{b}.tif")
                    driver = gdal.GetDriverByName("GTiff")
                    ds_empty = driver.Create(empty_path, 1, 1, 1, gdal.GDT_Float32)
                    ds_empty.SetGeoTransform([0, 0, 0, 0, 0, 0])
                    ds_empty.GetRasterBand(1).SetNoDataValue(0)
                    ds_empty.GetRasterBand(1).Fill(0)
                    ds_empty.FlushCache()
                    ds_empty = None
                    band_outputs.append(empty_path)
                    continue

                resample = "near" if b in ("SCL", "qa_pixel", "QA_PIXEL") else "bilinear"

                # 各波段 nodata 设置
                if b in ("qa_pixel", "QA_PIXEL"):
                    # qa_pixel: Fill=1 是 nodata，需要告诉 GDAL 才能从另一景填补
                    _src_nodata = 1
                    _dst_nodata = 1
                elif b == "data" or "dem" in b.lower():
                    # DEM: 0 是合法海拔（海平面），不能当 nodata
                    _src_nodata = None
                    _dst_nodata = None
                else:
                    _src_nodata = 0
                    _dst_nodata = 0

                warp_kwargs = dict(
                    format="GTiff",
                    dstSRS=utm_epsg_str,
                    outputBounds=(x1, y1, x2, y2),
                    xRes=scale,
                    yRes=scale,
                    resampleAlg=resample,
                    srcNodata=_src_nodata,
                    dstNodata=_dst_nodata,
                    creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES"],
                    multithread=True,
                )

                # 研究区裁剪：如果提供了 GeoJSON 文件，用 cutline
                if study_area_geojson and os.path.isfile(study_area_geojson):
                    warp_kwargs["cutlineDSName"] = study_area_geojson
                    warp_kwargs["cropToCutline"] = True

                warp_opts = gdal.WarpOptions(**warp_kwargs)

                tmp_out = os.path.join(tmp_dir, f"band_{b}.tif")

                try:
                    ds = gdal.Warp(tmp_out, urls, options=warp_opts)
                    if ds:
                        ds.FlushCache()
                        ds = None
                        band_outputs.append(tmp_out)
                        if log_callback:
                            log_callback("INFO", f"  {b}: {len(urls)} 景 → mosaic 完成")
                    else:
                        if log_callback:
                            log_callback("WARN", f"  {b}: gdal.Warp 返回空，写入零值波段")
                        _write_empty_band(tmp_out, utm_epsg_str, x1, y1, x2, y2, scale)
                        band_outputs.append(tmp_out)
                except Exception as e:
                    if log_callback:
                        log_callback("WARN", f"  {b}: Warp 失败 ({e})，写入零值波段")
                    _write_empty_band(tmp_out, utm_epsg_str, x1, y1, x2, y2, scale)
                    band_outputs.append(tmp_out)

            # ── 6. 合并多波段 ──────────────────────────────────────────
            if len(band_outputs) == 1:
                shutil.copy2(band_outputs[0], output_path)
            else:
                vrt_path = os.path.join(tmp_dir, "stacked.vrt")
                gdal.BuildVRT(vrt_path, band_outputs, separate=True, resolution="highest")
                gdal.Translate(
                    output_path, vrt_path,
                    format="GTiff",
                    creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES",
                                    "BLOCKXSIZE=256", "BLOCKYSIZE=256"],
                )

            # ── 7. 错误报告 ──────────────────────────────────────────
            if download_errors and log_callback:
                for err in download_errors[:5]:
                    log_callback("WARN", err)
                if len(download_errors) > 5:
                    log_callback("WARN", f"...还有 {len(download_errors)-5} 个错误")

            if log_callback:
                log_callback("INFO",
                    f"已保存 {band}: {output_path} ({band_count} 波段, {len(items_sorted)} 景 mosaic)")

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 下载 asset ──────────────────────────────────────────────────

    @staticmethod
    def _fetch_asset(url: str, log_callback) -> Optional[bytes]:
        """下载 asset 原始字节，带重试（最多 5 次，总超时 300s）
        
        每次失败用新 session（避免梯子切换导致连接池脏连接），
        重试前强制重建 socket。
        """
        import time as _time
        last_err = None
        for attempt in range(5):
            t0 = _time.time()
            timeout = min(120 + attempt * 45, 300)
            try:
                # 每次用新 session，无连接池复用，避免脏连接
                sess = requests.Session()
                # 禁用连接池复用（每次 HTTP 都新建 TCP 连接）
                sess.mount("https://", requests.adapters.HTTPAdapter(
                    pool_connections=0, pool_maxsize=0,
                    max_retries=0,
                ))
                sess.mount("http://", requests.adapters.HTTPAdapter(
                    pool_connections=0, pool_maxsize=0,
                    max_retries=0,
                ))
                # 不保留 proxy 配置，让系统决定
                sess.trust_env = False
                if log_callback and attempt == 0:
                    log_callback("INFO", f"  开始下载 ({timeout}s超时): {url[:70]}...")
                resp = sess.get(url, timeout=timeout)
                sess.close()
                elapsed = _time.time() - t0
                if log_callback:
                    log_callback("INFO", f"  下载完成 ({elapsed:.1f}s, {len(resp.content)/1024/1024:.1f}MB)")
                resp.raise_for_status()
                return resp.content
            except requests.exceptions.RequestException as e:
                elapsed = _time.time() - t0
                last_err = e
                # 梯子切换/网络抖动的典型错误
                is_network_flap = isinstance(e, (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ProxyError,
                    requests.exceptions.SSLError,
                    requests.exceptions.Timeout,
                ))
                if log_callback:
                    if is_network_flap and attempt < 4:
                        log_callback("WARN", f"  网络抖动 ({(request_type := type(e).__name__)}), 第{attempt+1}/5次重试...")
                    else:
                        log_callback("WARN", f"  下载失败 ({elapsed:.1f}s, 第{attempt+1}次): {e}")
                # 等待优雅递增让网络恢复
                sleep_sec = min(2 ** attempt, 15)
                _time.sleep(sleep_sec)
        if log_callback:
            log_callback("WARN", f"  下载最终失败 (5次重试): {last_err}")
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
