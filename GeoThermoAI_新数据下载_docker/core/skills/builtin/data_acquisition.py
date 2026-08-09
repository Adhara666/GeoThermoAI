"""
数据获取 Skill (Planetary Computer / Copernicus Data Space / GDAL 实现)

下载遥感数据：
    - Landsat 8/9 Collection 2 Level-2 (地表温度 lwir11 + qa_pixel)
    - Sentinel-2 Level-2A (多光谱 + SCL，按景应用 BOA_ADD_OFFSET 定标后再拼接；
      优先 Copernicus Data Space，失败回退 Planetary Computer)
    - DEM (Copernicus GLO-30；优先 Copernicus Data Space，失败回退 Planetary Computer)

处理流程：下载 COG → 保存到临时文件 →（Sentinel-2 光谱波段：按景应用 BOA_ADD_OFFSET
定标）→ gdal.Warp(mosaic + UTM + clip) → 合并多波段 → 重新打开校验 → 原子替换为正式文件

可靠性约定：
    - bbox 坐标变换统一使用 core.geo_transform（显式传统 GIS 轴序 + 异常模式 +
      四角加密取样 + 有限性校验）；
    - 缺波段/下载失败/Warp 失败时返回结构化失败，不写全零占位；BuildVRT/Translate
      返回值显式检查；成功前重新打开输出文件校验波段数/尺寸/CRS/有效覆盖率；
      下载与合成合并进同一个 try/finally，临时目录在任何失败路径下都会被清理；
    - Sentinel-2 光谱波段在拼接前按景读取 BOA_ADD_OFFSET/quantification 定标，
      不对所有影像盲减固定值；定标 provenance 写入固定 sentinel2_provenance.json。
"""

import os
import json
import re
import time
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import numpy as np

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ...geo_transform import bbox_wgs84_to_utm_bounds, enable_gdal_osr_exceptions, utm_epsg_for_lonlat
from ...atomic_io import write_verified
from . import sentinel2_calibration as s2cal

# 项目根目录
_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Planetary Computer STAC API
_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"

# Copernicus Data Space (Sentinel-2 / DEM 加速下载，国内访问更快；失败回退 Planetary Computer)
_DS_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
_DS_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
_DS_SENTINEL2_COLLECTION = "sentinel-2-l2a"
# CDSE 的 Copernicus DEM GLO-30 collection 名（与 Planetary Computer 的 cop-dem-glo-30 不同）
_DS_DEM_COLLECTION_GLO30 = "cop-dem-glo-30-dged-cog"

# 超时设置
SAT_API_TIMEOUT = 30  # STAC API 请求超时(秒)

# STAC 搜索重试次数与单次 HTTP 请求超时
_SEARCH_ATTEMPTS = 3
_STAC_TIMEOUT = 60

# Sentinel-2 波段定标后使用的 nodata 哨兵值（不用 0，避免和合法的近零校正反射率混淆）
_S2_CALIBRATED_NODATA = -9999.0

# DEM 输出 nodata 哨兵值（高程值远大于此，安全）：gdal.Warp cutline 外区域填充并
# 标记为 nodata，否则多边形外的填充值会被渲染端当作有效高程参与色带拉伸 → 紫边
_DEM_NODATA = -9999.0


def _open_catalog(log_callback=None, attempts: int = _SEARCH_ATTEMPTS):
    """连接 Planetary Computer STAC 目录，带重试与超时。

    成功返回 (catalog, None)；全部失败返回 (None, 最后一次错误信息)。
    """
    from pystac_client import Client
    from pystac_client.stac_api_io import StacApiIO
    last_err = ""
    for i in range(attempts):
        try:
            return Client.open(
                _STAC_URL,
                headers={"Accept": "application/json"},
                stac_io=StacApiIO(timeout=_STAC_TIMEOUT, max_retries=2),
            ), None
        except Exception as e:
            last_err = str(e)
            if log_callback:
                log_callback("WARN", f"连接 Planetary Computer 失败（第{i+1}/{attempts}次）: {e}")
            time.sleep(2 * (i + 1))
    return None, last_err


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


class BandAcquisitionError(RuntimeError):
    """必需波段下载/拼接失败的结构化异常（禁止用全零占位代替）。"""


class DataAcquisitionSkill(BaseSkill):
    """从 Planetary Computer / Copernicus Data Space 下载遥感数据（Landsat, Sentinel-2, DEM）"""

    # Data Space token 缓存（token 有效期约 10 分钟，避免下载中途过期）
    _ds_token_cache: dict = {}
    _ds_token_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "data_acquisition"

    @property
    def group(self) -> str:
        return "data_process"

    @property
    def description(self) -> str:
        return "从 Microsoft Planetary Computer / Copernicus Data Space 下载 Landsat 8/9 L2 ST、Sentinel-2 L2A 多光谱、QA/SCL 和 DEM 数据到本地目录。Sentinel-2 与 DEM 优先 Copernicus Data Space（国内更快），失败回退行星计算机；无需注册，免费下载。"

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
                description="云覆盖阈值（百分比），默认 30",
                required=False,
                default=30,
            ),
            SkillParameter(
                name="dem_source",
                type="string",
                description="DEM 数据源（Copernicus GLO-30）",
                required=False,
                default="copernicus",
                choices=["copernicus"],
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
        """执行数据下载流程；任何必需波段的结构化失败都会转成 success=False 并给出
        清晰原因，不抛出未捕获异常、也不产出伪造的"成功"占位数据。"""
        try:
            return self._execute_impl(params, progress_callback, log_callback)
        except BandAcquisitionError as e:
            return SkillResult(success=False, message=f"数据获取失败（必需波段）: {e}")
        except Exception as e:
            return SkillResult(success=False, message=f"数据获取失败: {e}")

    def _execute_impl(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        region = params.get("region", "")
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        output_dir = params.get("output_dir", "")
        cloud_threshold = params.get("cloud_threshold", 30)
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
            enable_gdal_osr_exceptions()
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

        catalog, _catalog_err = _open_catalog(log_callback)
        if catalog is None:
            _detail = f"\n错误信息: {_catalog_err}" if _catalog_err else ""
            return SkillResult(
                success=False,
                message=f"无法连接 Planetary Computer（已重试 {_SEARCH_ATTEMPTS} 次）{_detail}\n请检查网络连接",
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

        # Sentinel-2 优先使用 Copernicus Data Space（国内更快），失败/未配置则回退 Planetary Computer
        ds_cfg = self._load_dataspace_config()
        ds_token = ""          # 用于 STAC 搜索（账号密码 或 OAuth2 Client 均可）
        s3_creds = None        # 用于 eodata 下载（S3 密钥，优先）
        if ds_cfg.get("username") or ds_cfg.get("client_id"):
            try:
                ds_token = self._get_dataspace_token(ds_cfg)
                if log_callback and not selected_pair:
                    log_callback("INFO", "Sentinel-2 数据源: Copernicus Data Space")
            except Exception as e:
                ds_token = ""
                if log_callback:
                    log_callback("WARN", f"Data Space 认证失败: {e}")
        if ds_cfg.get("s3_key") and ds_cfg.get("s3_secret"):
            s3_creds = {"access_key": ds_cfg["s3_key"], "secret_key": ds_cfg["s3_secret"]}
            if log_callback and not selected_pair:
                log_callback("INFO", "Sentinel-2 下载方式: eodata S3 签名（SigV4）")

        sentinel2_items = []
        if ds_token:
            try:
                ds_catalog = Client.open(_DS_STAC_URL, headers={"Authorization": f"Bearer {ds_token}"})
                s2_search = ds_catalog.search(
                    collections=[_DS_SENTINEL2_COLLECTION],
                    bbox=bbox,
                    datetime=f"{start_date}/{end_date}",
                    query={"eo:cloud_cover": {"lt": cloud_threshold}},
                    max_items=100,
                )
                sentinel2_items = list(s2_search.items())
                if log_callback and not selected_pair:
                    log_callback("INFO", f"[Data Space] 找到 {len(sentinel2_items)} 景 Sentinel-2 影像")
                    from collections import Counter
                    s2_dates = Counter(i.properties.get("datetime", "")[:10] for i in sentinel2_items)
                    for dt, cnt in sorted(s2_dates.items()):
                        log_callback("INFO", f"  {dt}: {cnt} 景")
            except Exception as e:
                sentinel2_items = []
                if log_callback:
                    log_callback("WARN", f"Data Space 搜索失败: {e}，Sentinel-2 回退 Planetary Computer")

        if not sentinel2_items:
            sentinel2_items = _search_items(
                catalog, log_callback, "Sentinel-2",
                collections=["sentinel-2-l2a"],
                bbox=bbox,
                datetime=f"{start_date}/{end_date}",
                query={"eo:cloud_cover": {"lt": cloud_threshold}},
                max_items=100,
            )
            if log_callback and not selected_pair:
                log_callback("INFO", f"[Planetary Computer] 找到 {len(sentinel2_items)} 景 Sentinel-2 影像")
                from collections import Counter
                s2_dates = Counter(i.properties.get("datetime", "")[:10] for i in sentinel2_items)
                for dt, cnt in sorted(s2_dates.items()):
                    log_callback("INFO", f"  {dt}: {cnt} 景")

        # CDSE 下载请求头（Sentinel-2 下载时使用；失败回退时行星计算机不需要）
        ds_headers = {"Authorization": f"Bearer {ds_token}"} if ds_token else None

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

        selected_landsat_date = selected_pair.get("landsat_date", "")
        selected_sentinel_date = selected_pair.get("sentinel2_date", "")
        selected_satellite = selected_pair.get("landsat_satellite", "")
        # 升级点 4：本地 LST / Sentinel-2 文件名带 YYYYMMDD 日期；DEM 不带日期
        _ldate = str(selected_landsat_date).replace("-", "")
        _sdate = str(selected_sentinel_date).replace("-", "")
        # 升级点 3：项目根目录（由执行引擎注入），用于已下载对跳过 / 重复影像复制 / DEM 复用
        _project_dir = str(params.get("project_dir") or "").strip()
        # 方案 A：对话级独立工作目录。对话 project_dir = {项目根}/convs/{对话id}，
        # 已下载影像/DEM 缓存在项目根共享目录，跨对话复用不重复下载。
        # 若路径不以 /convs/ 结尾（旧对话或兜底），则视为项目根本身。
        _shared_root = _project_dir
        if _project_dir:
            _pdir_norm = _project_dir.replace("\\", "/").rstrip("/")
            _parts = _pdir_norm.split("/")
            if len(_parts) >= 2 and _parts[-2] == "convs":
                _shared_root = "/".join(_parts[:-2])

        # 研究区标识：共享缓存必须按研究区隔离。缓存里存的是按当前研究区
        # GeoJSON 裁剪 + 重投影（UTM）后的影像，同日期缓存不能跨研究区复用，
        # 否则换研究区跑会拿到上一个研究区的范围/投影（D1 同类隐患）。
        # 保留中文（研究区文件名多为中文，如 鄂州市_市），仅替换路径分隔符等
        _region_key = "bbox"
        if study_area_geojson:
            _region_key = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_",
                                 os.path.splitext(os.path.basename(study_area_geojson))[0]) or "region"

        def _land(name: str) -> str:
            return f"{name}_{_ldate}.tif" if _ldate else f"{name}.tif"

        def _sent(name: str) -> str:
            return f"{name}_{_sdate}.tif" if _sdate else f"{name}.tif"

        def _cache_to_shared(filename: str, path: str) -> None:
            """下载完成后把影像缓存到项目级共享目录（方案 A），供其他对话复用。"""
            if not _shared_root or _shared_root == _project_dir:
                return
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                return
            cache_dir = os.path.join(_shared_root, "pairs", _region_key,
                                     f"L{_ldate}_S{_sdate}", "raw")
            try:
                os.makedirs(cache_dir, exist_ok=True)
                target = os.path.join(cache_dir, filename)
                if not (os.path.isfile(target) and os.path.getsize(target) > 0):
                    shutil.copy2(path, target)
            except OSError:
                pass

        def _reuse_local(path: str, desc: str) -> bool:
            """目标已存在且非空 → 跳过下载，直接复用本地文件（升级点 3）。"""
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                if log_callback:
                    log_callback("INFO", f"{desc} 已存在（{os.path.basename(path)}），跳过下载")
                return True
            return False

        def _copy_from_other_pair(filename: str, path: str, desc: str) -> bool:
            """其他配对/共享缓存中已有同名影像 → 直接复制，无需重复下载（升级点 3）。

            方案 A：扫描项目级共享缓存（{项目根}/pairs/...）与当前对话目录的配对，
            跨对话复用已下载的原始影像。
            """
            if os.path.isfile(path):
                return False
            candidates = []
            if _shared_root:
                candidates.append(os.path.join(_shared_root, "pairs"))
            if _project_dir and _project_dir != _shared_root:
                candidates.append(os.path.join(_project_dir, "pairs"))
            for pairs_root in candidates:
                if os.path.dirname(pairs_root).replace("\\", "/").rstrip("/").endswith("/convs"):
                    # 对话内配对目录（无研究区层级，本对话研究区固定，直接扫描）
                    _region_roots = [pairs_root]
                else:
                    # 项目级共享缓存：限定当前研究区子目录，禁止跨研究区复用
                    _region_roots = [os.path.join(pairs_root, _region_key)]
                for _rr in _region_roots:
                    if not os.path.isdir(_rr):
                        continue
                    for _d in sorted(os.listdir(_rr)):
                        cand = os.path.join(_rr, _d, "raw", filename)
                        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            shutil.copy2(cand, path)
                            if log_callback:
                                log_callback("INFO", f"{desc} 复用已下载影像（{filename}）")
                            return True
            return False

        def _need_download(path: str, filename: str, desc: str) -> bool:
            """返回 True 表示需要真正下载；否则已复用本地/其他配对文件。"""
            if _reuse_local(path, desc):
                return False
            if _copy_from_other_pair(filename, path, desc):
                return False
            return True

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
            progress_callback("data_acquisition", 0.13, "下载 Landsat lwir11 (地表温度)...")

        landsat_path = os.path.join(output_dir, _land("landsat_lst"))
        if _need_download(landsat_path, os.path.basename(landsat_path), "Landsat LST"):
            self._download_composite(
                items=landsat_items, band="lwir11", output_path=landsat_path,
                bbox=bbox, scale=30, progress_callback=progress_callback, log_callback=log_callback,
                progress_range=(0.13, 0.26), skill_name="data_acquisition",
                study_area_geojson=study_area_geojson,
            )
            _cache_to_shared(os.path.basename(landsat_path), landsat_path)
        output_paths["landsat_path"] = landsat_path

        # ── 下载 Landsat qa_pixel ────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.26, "下载 Landsat qa_pixel...")

        qa_path = os.path.join(output_dir, _land("landsat_qa_pixel"))
        if _need_download(qa_path, os.path.basename(qa_path), "Landsat QA"):
            self._download_composite(
                items=landsat_items, band="qa_pixel", output_path=qa_path,
                bbox=bbox, scale=30, progress_callback=progress_callback, log_callback=log_callback,
                progress_range=(0.26, 0.36), skill_name="data_acquisition",
                study_area_geojson=study_area_geojson,
            )
            _cache_to_shared(os.path.basename(qa_path), qa_path)
        output_paths["qa_path"] = qa_path

        # ── 下载 Sentinel-2 多光谱（按景定标 BOA_ADD_OFFSET）────
        if progress_callback:
            progress_callback("data_acquisition", 0.42, "下载 Sentinel-2 多光谱...")

        sentinel2_path = os.path.join(output_dir, _sent("sentinel2_bands"))
        s2_provenance = None
        if _need_download(sentinel2_path, os.path.basename(sentinel2_path), "Sentinel-2 多光谱"):
            s2_provenance = self._download_composite(
                items=sentinel2_items, band=["B02", "B03", "B04", "B08", "B11"],
                output_path=sentinel2_path, bbox=bbox, scale=10,
                progress_callback=progress_callback, log_callback=log_callback,
                progress_range=(0.42, 0.62), skill_name="data_acquisition",
                study_area_geojson=study_area_geojson, apply_s2_calibration=True,
                auth_headers=ds_headers, s3_creds=s3_creds,
            )
            _cache_to_shared(os.path.basename(sentinel2_path), sentinel2_path)
        output_paths["sentinel2_path"] = sentinel2_path

        if s2_provenance:
            provenance_path = os.path.join(output_dir, f"sentinel2_provenance_{_sdate}.json"
                                           if _sdate else "sentinel2_provenance.json")
            with open(provenance_path, "w", encoding="utf-8") as f:
                json.dump({
                    "formula": "reflectance = (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE; DN==0 仍是 NoData",
                    "scenes": s2_provenance,
                }, f, ensure_ascii=False, indent=2)
            output_paths["sentinel2_provenance_path"] = provenance_path

        # ── 下载 Sentinel-2 SCL ──────────────────────────────────────
        if progress_callback:
            progress_callback("data_acquisition", 0.62, "下载 Sentinel-2 SCL...")

        scl_path = os.path.join(output_dir, _sent("sentinel2_scl"))
        if _need_download(scl_path, os.path.basename(scl_path), "Sentinel-2 SCL"):
            self._download_composite(
                items=sentinel2_items, band="SCL", output_path=scl_path,
                bbox=bbox, scale=20, progress_callback=progress_callback, log_callback=log_callback,
                progress_range=(0.62, 0.72), skill_name="data_acquisition",
                study_area_geojson=study_area_geojson,
                auth_headers=ds_headers, s3_creds=s3_creds,
            )
            _cache_to_shared(os.path.basename(scl_path), scl_path)
        output_paths["scl_path"] = scl_path

        # ── 下载 DEM（优先 Copernicus Data Space，失败/未配置回退 Planetary Computer）──
        if progress_callback:
            progress_callback("data_acquisition", 0.72, f"下载 DEM ({dem_source})...")

        dem_path = os.path.join(output_dir, "dem.tif")
        dem_collection = "cop-dem-glo-30"
        # 升级点 3 + 方案 A：DEM 项目级共享缓存（{项目根}/dem_{研究区}.tif），
        # 各对话独立目录均从共享缓存复制，全项目只下载一次；缓存按研究区隔离，
        # 避免换研究区复用上一个研究区已裁剪的 DEM
        _project_dem = os.path.join(_shared_root, f"dem_{_region_key}.tif") if _shared_root else ""
        if _project_dem and os.path.isfile(_project_dem) and os.path.getsize(_project_dem) > 0:
            os.makedirs(output_dir, exist_ok=True)
            shutil.copy2(_project_dem, dem_path)
            if log_callback:
                log_callback("INFO", "DEM 复用项目级本地文件，跳过下载")
            output_paths["dem_path"] = dem_path
        else:
            _dem_target = _project_dem or dem_path

            dem_items = []
            dem_from_ds = False  # DEM 瓦片是否来自 CDSE（决定下载时是否附加 CDSE 认证）
            # DEM 属 CCM（Contributing Missions）数据：CDSE 下载仅支持 S3 SigV4 签名
            # （Bearer token 会 403，已实测），因此只有配置了 S3 密钥时才走 CDSE
            if ds_token and s3_creds:
                try:
                    dem_items = list(ds_catalog.search(
                        collections=[_DS_DEM_COLLECTION_GLO30], bbox=bbox, max_items=10,
                    ).items())
                    dem_from_ds = len(dem_items) > 0
                    if log_callback and not selected_pair:
                        log_callback("INFO", f"[Data Space] 找到 {len(dem_items)} 个 DEM 瓦片")
                except Exception as e:
                    dem_items = []
                    if log_callback:
                        log_callback("WARN", f"Data Space DEM 搜索失败: {e}，DEM 回退 Planetary Computer")

            if not dem_items:
                dem_items = _search_items(
                    catalog, log_callback, "DEM",
                    collections=[dem_collection], bbox=bbox, max_items=10,
                )

            self._download_composite(
                items=dem_items, band="data", output_path=_dem_target,
                bbox=bbox, scale=30, progress_callback=progress_callback, log_callback=log_callback,
                progress_range=(0.72, 0.95), skill_name="data_acquisition",
                study_area_geojson=study_area_geojson,
                auth_headers=ds_headers if dem_from_ds else None,
                s3_creds=s3_creds if dem_from_ds else None,
            )
            if _project_dem:
                os.makedirs(output_dir, exist_ok=True)
                shutil.copy2(_project_dem, dem_path)
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
            artifacts=[v for v in output_paths.values() if v],
        )

    # ── Copernicus Data Space (Sentinel-2) ──────────────────────────

    @staticmethod
    def _load_dataspace_config() -> dict:
        """读取当前用户的 data_space 配置

        前端"数据源"面板把凭据保存在 data/users/{uid}/settings.json 的
        data_space 段，凭据只跟用户走，不读全局 config/settings.json。
        单用户部署取第一个非空配置即可。
        """
        try:
            users_dir = _ROOT / "data" / "users"
            if users_dir.is_dir():
                for up in sorted(users_dir.iterdir()):
                    sp = up / "settings.json"
                    if sp.is_file():
                        ucfg = json.loads(sp.read_text(encoding="utf-8")).get("data_space", {}) or {}
                        if ucfg.get("username") or ucfg.get("client_id") or ucfg.get("s3_key"):
                            return ucfg
        except Exception:
            pass
        return {}

    @classmethod
    def _get_dataspace_token(cls, cfg: dict) -> str:
        """获取 Copernicus Data Space access_token（带缓存，避免下载中途过期）

        优先用账号邮箱+密码换取 public token（cdse-public + password grant，
        可搜索 STAC 且可下载 eodata 数据）；
        无账号密码时回退 OAuth2 client_credentials token（仅能搜索 STAC）。
        """
        cache_key = (cfg.get("username", ""), cfg.get("password", ""),
                     cfg.get("client_id", ""), cfg.get("client_secret", ""))
        now = time.time()
        with cls._ds_token_lock:
            cached = cls._ds_token_cache.get(cache_key)
            if cached and cached.get("expires_at", 0) > now + 120:
                return cached["token"]

        resp = None
        errs = []
        if cfg.get("username") and cfg.get("password"):
            # 官方标准：cdse-public + password grant（可搜索+下载）
            try:
                resp = requests.post(
                    _DS_TOKEN_URL,
                    data={
                        "client_id": "cdse-public",
                        "grant_type": "password",
                        "username": cfg.get("username", ""),
                        "password": cfg.get("password", ""),
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                errs.append(str(e))
                resp = None
        if resp is None:
            # 回退：client_credentials（可搜索 STAC；下载请配 S3 密钥）
            if not (cfg.get("client_id") and cfg.get("client_secret")):
                raise RuntimeError(
                    "Data Space 未配置有效凭据：请填写 账号邮箱+密码 或 OAuth2 Client ID/Secret"
                    f"（账号密码方式失败原因: {'; '.join(errs) or '未知'}）"
                )
            resp = requests.post(
                _DS_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cfg.get("client_id", ""),
                    "client_secret": cfg.get("client_secret", ""),
                },
                timeout=30,
            )
            resp.raise_for_status()

        token = resp.json().get("access_token", "")
        expires_in = int(resp.json().get("expires_in", 600) or 600)
        with cls._ds_token_lock:
            cls._ds_token_cache[cache_key] = {"token": token, "expires_at": now + expires_in}
        return token

    @staticmethod
    def _sign_s3_headers(access_key: str, secret_key: str, method: str,
                         host: str, path: str, query: str = "",
                         region: str = "default", service: str = "s3",
                         payload: bytes = b"") -> dict:
        """AWS Signature V4 签名，返回请求头（用于 Data Space eodata S3 下载）

        CDSE 的 S3 endpoint 用 region='default'（官方 boto3 示例）。
        """
        import datetime as _dt
        import hashlib as _hl
        import hmac as _hm
        import urllib.parse as _up

        now = _dt.datetime.utcnow()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = _hl.sha256(payload).hexdigest()

        canonical_uri = _up.quote(path, safe="/")
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join([
            method, canonical_uri, query, canonical_headers, signed_headers, payload_hash,
        ])

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = "\n".join([
            algorithm, amz_date, credential_scope,
            _hl.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        def _hmac(key, msg):
            return _hm.new(key, msg.encode("utf-8"), _hl.sha256).digest()

        k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
        k_region = _hmac(k_date, region)
        k_service = _hmac(k_region, service)
        k_signing = _hmac(k_service, "aws4_request")
        signature = _hm.new(k_signing, string_to_sign.encode("utf-8"), _hl.sha256).hexdigest()

        authorization = (
            f"{algorithm} Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        }

    @staticmethod
    def _ds_https_url(asset_href: str) -> str:
        """把 Data Space 的 s3://eodata/... 转成可下载的 HTTPS 地址

        eodata S3 的 endpoint 是 https://eodata.dataspace.copernicus.eu，
        bucket 名是 eodata，所以 HTTPS 路径必须保留 /eodata/ 前缀。
        """
        if asset_href.startswith("s3://eodata/"):
            return "https://eodata.dataspace.copernicus.eu/eodata/" + asset_href[len("s3://eodata/"):]
        return asset_href

    @staticmethod
    def _find_ds_asset(assets, band_key: str):
        """Data Space 的 asset 名带分辨率后缀（如 B02_10m），做前缀匹配"""
        if band_key in assets:
            return assets[band_key]
        for k, v in assets.items():
            if k.startswith(band_key + "_"):
                return v
        return None

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

        if not os.path.isfile(region) and DataAcquisitionSkill._looks_like_region_file(region):
            from pathlib import Path
            candidate_roots = [
                Path(__file__).resolve().parent.parent.parent.parent,
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

            if ext == ".shp":
                try:
                    import shapefile as shp
                    reader = shp.Reader(region)
                    bounds = reader.bbox
                    return list(bounds)
                except ImportError:
                    pass
                except Exception:
                    pass

            if ext in (".geojson", ".json"):
                try:
                    import geopandas as gpd
                    gdf = gpd.read_file(region)
                    b = list(gdf.total_bounds)
                    if len(b) == 4:
                        return b
                except Exception:
                    pass
                try:
                    with open(region, "r", encoding="utf-8") as f:
                        geojson = json.load(f)
                    coords = _extract_coords(geojson)
                    if coords:
                        xs = [c[0] for c in coords]
                        ys = [c[1] for c in coords]
                        return [min(xs), min(ys), max(xs), max(ys)]
                except Exception:
                    pass
                return None

        try:
            parts = [float(x.strip()) for x in region.split(",")]
        except (ValueError, TypeError):
            parts = []
        if len(parts) == 4:
            return parts

        _CITY_BBOX = {
            "武汉": "113.7,29.9,114.9,31.3",
            "北京": "115.4,39.4,117.5,41.1",
            "上海": "120.8,30.6,122.2,31.9",
            "广州": "112.8,22.8,114.0,23.8",
        }
        for city, bbox_str in _CITY_BBOX.items():
            if city in region:
                return [float(x.strip()) for x in bbox_str.split(",")]
        return None

    # ── 影像配对 ────────────────────────────────────────────────────

    @staticmethod
    def _build_pairs(landsat_items: list, sentinel2_items: list,
                     bbox: list = None, study_area_geojson: str = None,
                     log_callback=None) -> List[dict]:
        """构建 Landsat-Sentinel2 拼接配对（时间差 ≤ 2天）

        规则：
        - Landsat 8 只和 L8 拼接，L9 只和 L9 拼接
        - 每组 mosaic 覆盖度 ≥ 70% 才合格
        - Sentinel 按日期分组

        返回每对含：每景详情（日期、L8/L9/S2、云量）+ 综合覆盖度
        """
        from datetime import datetime, timedelta

        def _warn(msg):
            if log_callback:
                log_callback("WARN", msg)

        def _satellite_type(item):
            item_id = item.id.lower()
            if item_id.startswith("lc08") or "landsat_8" in item_id:
                return "L8"
            elif item_id.startswith("lc09") or "landsat_9" in item_id:
                return "L9"
            elif item_id.startswith("s2a") or "sentinel-2a" in item_id:
                return "S2A"
            elif item_id.startswith("s2b") or "sentinel-2b" in item_id:
                return "S2B"
            return "?"

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
            if not items_list or not geojson_path:
                return 1.0
            try:
                with open(geojson_path, "r", encoding="utf-8") as f:
                    gj = json.load(f)
                study_polys = _extract_polygons(gj)
                if not study_polys:
                    return 1.0

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

            l_coverage = _check_coverage(l_items, study_area_geojson)
            if l_coverage < 0.7:
                _warn(f"Landsat {l_date} ({l_sat}) 覆盖度 {l_coverage*100:.0f}% < 70%，跳过")
                continue

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

    # ── Sentinel-2 按景定标 ────────────────────────────────────

    @staticmethod
    def _apply_s2_offset_correction(src_path: str, dst_path: str, offset: float) -> None:
        """把已下载的原始 DN 波段文件按给定 offset 校正为 corrected_DN = DN + offset，
        原始 DN==0（NoData）像元保持为 nodata（不参与校正），nodata 哨兵为 -9999，
        避免和合法但接近 0 的校正反射率混淆。写出 Float32（因为校正后可能出现负值）。
        """
        from osgeo import gdal

        src_ds = gdal.Open(src_path)
        if src_ds is None:
            raise BandAcquisitionError(f"无法打开待定标的 Sentinel-2 临时文件: {src_path}")
        try:
            band = src_ds.GetRasterBand(1)
            arr = band.ReadAsArray().astype(np.float64)
            geotransform = src_ds.GetGeoTransform()
            projection = src_ds.GetProjection()
            width, height = src_ds.RasterXSize, src_ds.RasterYSize
        finally:
            src_ds = None

        is_nodata = arr == 0
        corrected = np.where(is_nodata, _S2_CALIBRATED_NODATA, arr + offset)

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(
            dst_path, width, height, 1, gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES"],
        )
        if out_ds is None:
            raise BandAcquisitionError(f"无法创建定标输出文件: {dst_path}")
        try:
            out_ds.SetGeoTransform(geotransform)
            out_ds.SetProjection(projection)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(_S2_CALIBRATED_NODATA)
            out_band.WriteArray(corrected.astype(np.float32))
            out_band.FlushCache()
            out_ds.FlushCache()
        finally:
            out_ds = None

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
        required: bool = True,
        apply_s2_calibration: bool = False,
        auth_headers: Optional[dict] = None,
        s3_creds: Optional[dict] = None,
    ):
        """下载影像、mosaic合并多景（GDAL /vsicurl/ 直接读取 COG）

        流程：
        1. 签名所有 item，收集各波段的本地临时文件（Sentinel-2 光谱波段按景定标）
        2. 逐波段 gdal.Warp(mosaic + UTM + clip) 一步完成，显式检查返回值
        3. 合并多波段，显式检查 BuildVRT/Translate 返回值
        4. 重新打开输出校验波段数/尺寸/CRS/有效覆盖率后，原子替换为正式文件名

        Args:
            required: True 时任一必需波段缺失/下载失败/Warp失败都会抛出
                      BandAcquisitionError（不写全零占位）；False 时
                      静默跳过，不产生输出文件，也不抛异常。
            apply_s2_calibration: True 时对每个 (景,波段) 文件按景定标后再加入
                                  mosaic 输入列表（仅用于 Sentinel-2 光谱波段）。
            auth_headers: Data Space 下载时附加的 HTTP 请求头（Bearer token）；
                          s3_creds: Data Space eodata S3 密钥（access_key/secret_key），
                          提供时用 SigV4 签名下载，优先于 auth_headers。
                          两者都为 None 时走 Planetary Computer 的 SAS 签名逻辑。

        Returns:
            当 apply_s2_calibration=True 时，返回本次调用涉及的按景定标 provenance 列表；
            否则返回 None。
        """
        from osgeo import gdal, osr

        if not items:
            msg = f"未找到任何 {band} 影像，无法继续后续流程"
            if log_callback:
                log_callback("ERROR" if required else "WARN", msg)
            if required:
                raise BandAcquisitionError(msg)
            return None

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
        utm_epsg = utm_epsg_for_lonlat(center_lon, center_lat)
        utm_epsg_str = f"EPSG:{utm_epsg}"

        if log_callback:
            log_callback("INFO", f"目标坐标系: {utm_epsg_str}")

        # ── 2. bbox → UTM（统一显式传统 GIS 轴序 + 四角加密 + 有限性校验）──
        x1, y1, x2, y2 = bbox_wgs84_to_utm_bounds(bbox, utm_epsg)

        tmp_dir = tempfile.mkdtemp(prefix="gdal_dl_")
        s2_provenance: List[dict] = []
        try:
            # ── 3. 下载所有场景到本地临时文件（含 Sentinel-2 按景定标）─────
            band_files = {b: [] for b in bands_list}
            download_errors = []

            for idx, item in enumerate(items_sorted):
                if s3_creds or auth_headers:
                    # Copernicus Data Space：asset 无需 SAS 签名，直接带 Bearer/S3 下载
                    scene_item = item
                else:
                    # Planetary Computer：每个波段下载前重新签名（SAS token 有时效，避免 403）
                    scene_item = self._sign_item(item)
                    if scene_item is None:
                        download_errors.append(f"{item.id}: 签名失败")
                        continue

                scene_calibration = None
                if apply_s2_calibration:
                    # 按景定标元数据（MTD_MSIL2A.xml）下载认证：
                    # CDSE 的 product_metadata 是 s3://eodata/...，需 S3 SigV4 签名（或 Bearer）；
                    # 行星计算机的 product-metadata 已带 SAS token，无需额外头
                    pm = scene_item.assets.get("product-metadata") or scene_item.assets.get("product_metadata")
                    xml_headers = None
                    if pm is not None and getattr(pm, "href", "").startswith("s3://eodata/"):
                        if s3_creds:
                            _pm_url = self._ds_https_url(pm.href)
                            _pm_path = _pm_url.split("eodata.dataspace.copernicus.eu", 1)[1]
                            xml_headers = self._sign_s3_headers(
                                s3_creds["access_key"], s3_creds["secret_key"],
                                "GET", "eodata.dataspace.copernicus.eu", _pm_path,
                            )
                        elif auth_headers:
                            xml_headers = auth_headers
                    scene_calibration = s2cal.fetch_scene_calibration(
                        scene_item, log_callback=log_callback, headers=xml_headers)
                    s2_provenance.append(scene_calibration)

                for b in bands_list:
                    if s3_creds or auth_headers:
                        # Data Space：asset 名带分辨率后缀（如 B02_10m），前缀匹配
                        asset = self._find_ds_asset(scene_item.assets, b)
                        if not asset:
                            download_errors.append(f"{item.id}: 缺少波段 {b}")
                            continue
                        url = self._ds_https_url(asset.href)
                        if s3_creds:
                            path = url.split("eodata.dataspace.copernicus.eu", 1)[1]
                            headers = self._sign_s3_headers(
                                s3_creds["access_key"], s3_creds["secret_key"],
                                "GET", "eodata.dataspace.copernicus.eu", path,
                            )
                        else:
                            headers = auth_headers
                    else:
                        # Planetary Computer：每波段下载前重新签名（SAS 短时效，
                        # 长下载后旧签名可能已过期返回 403 —— B02/B03/B04 先下载
                        # 成功、B08/B11 等靠后波段 403，就是签名过期所致）
                        signed_item = self._sign_item(item)
                        if signed_item is None:
                            download_errors.append(f"{item.id}: 签名失败")
                            continue
                        asset = signed_item.assets.get(b)
                        if not asset:
                            download_errors.append(f"{item.id}: 缺少波段 {b}")
                            continue
                        url = asset.href
                        headers = None

                    if progress_callback:
                        p = p_min + (p_max - p_min) * 0.3 * (idx + 1) / len(items_sorted)
                        progress_callback(skill_name, p, f"下载 {b} / {item.id}")

                    def _dl_progress(downloaded, total_bytes, label):
                        # 大文件下载期间实时更新气泡：已下载 MB / 总 MB（若服务端告知）
                        if not progress_callback:
                            return
                        mb = downloaded / 1024 / 1024
                        if total_bytes:
                            pct_t = downloaded / total_bytes
                            progress_callback(
                                skill_name, p,
                                f"下载 {label} ({mb:.0f}/{total_bytes/1024/1024:.0f}MB, {pct_t:.0%})",
                            )
                        else:
                            progress_callback(skill_name, p, f"下载 {label} ({mb:.0f}MB)")

                    raw_path = os.path.join(tmp_dir, f"{idx:03d}_{b}_raw.tif")
                    data_bytes = self._fetch_asset(
                        url, log_callback, headers=headers,
                        progress_callback=_dl_progress, progress_label=f"{b} / {item.id}",
                    )
                    if data_bytes is None:
                        download_errors.append(f"{item.id}/{b}: 下载失败")
                        continue
                    with open(raw_path, "wb") as f:
                        f.write(data_bytes)

                    if apply_s2_calibration and scene_calibration is not None:
                        offset = s2cal.offset_for_band(scene_calibration, b)
                        corrected_path = os.path.join(tmp_dir, f"{idx:03d}_{b}_corrected.tif")
                        try:
                            self._apply_s2_offset_correction(raw_path, corrected_path, offset)
                        except Exception as e:
                            download_errors.append(f"{item.id}/{b}: 按景定标失败 ({e})")
                            continue
                        band_files[b].append(corrected_path)
                        if log_callback:
                            log_callback(
                                "INFO",
                                f"  {item.id}/{b}: 已按 {scene_calibration.get('source')} "
                                f"应用 offset={offset:g}",
                            )
                    else:
                        band_files[b].append(raw_path)

            # ── 4. 逐波段 GDAL Warp（mosaic + UTM + clip 一步到位）───────
            band_outputs = []
            for b_idx, b in enumerate(bands_list):
                if progress_callback:
                    p = p_min + (p_max - p_min) * (0.3 + 0.7 * b_idx / max(len(bands_list), 1))
                    progress_callback(skill_name, p, f"处理 {b}...")

                urls = band_files[b]
                if not urls:
                    msg = f"波段 {b} 没有任何可用数据（下载失败或缺失）"
                    if required:
                        raise BandAcquisitionError(msg)
                    if log_callback:
                        log_callback("WARN", f"  {b}: {msg}，该波段为可选，跳过")
                    return None

                resample = "near" if b in ("SCL", "qa_pixel", "QA_PIXEL") else "bilinear"

                if b in ("qa_pixel", "QA_PIXEL"):
                    _src_nodata, _dst_nodata = 1, 1
                elif b == "data" or "dem" in b.lower():
                    # DEM：cutline 外区域必须填充并标记 nodata（_DEM_NODATA），
                    # 否则渲染端会把多边形外的填充值当作有效高程参与色带拉伸 → 紫边
                    _src_nodata, _dst_nodata = None, _DEM_NODATA
                elif apply_s2_calibration:
                    _src_nodata, _dst_nodata = _S2_CALIBRATED_NODATA, _S2_CALIBRATED_NODATA
                else:
                    _src_nodata, _dst_nodata = 0, 0

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

                if study_area_geojson and os.path.isfile(study_area_geojson):
                    warp_kwargs["cutlineDSName"] = study_area_geojson
                    warp_kwargs["cropToCutline"] = True

                warp_opts = gdal.WarpOptions(**warp_kwargs)
                tmp_out = os.path.join(tmp_dir, f"band_{b}.tif")

                try:
                    ds = gdal.Warp(tmp_out, urls, options=warp_opts)
                except Exception as e:
                    msg = f"波段 {b}: gdal.Warp 失败 ({e})"
                    if required:
                        raise BandAcquisitionError(msg) from e
                    if log_callback:
                        log_callback("WARN", f"  {msg}，该波段为可选，跳过")
                    return None

                if ds is None or ds.RasterXSize < 1 or ds.RasterYSize < 1:
                    msg = f"波段 {b}: gdal.Warp 返回空或零尺寸影像"
                    if required:
                        raise BandAcquisitionError(msg)
                    if log_callback:
                        log_callback("WARN", f"  {msg}，该波段为可选，跳过")
                    return None
                ds.FlushCache()
                ds = None
                band_outputs.append(tmp_out)
                if log_callback:
                    log_callback("INFO", f"  {b}: {len(urls)} 景 → mosaic 完成")

            # ── 5. 合并多波段（显式检查返回值）──────────────────
            final_tmp = os.path.join(tmp_dir, "final_output.tif")
            if len(band_outputs) == 1:
                shutil.copy2(band_outputs[0], final_tmp)
            else:
                vrt_path = os.path.join(tmp_dir, "stacked.vrt")
                vrt_ds = gdal.BuildVRT(vrt_path, band_outputs, separate=True, resolution="highest")
                if vrt_ds is None:
                    raise BandAcquisitionError(f"{band}: gdal.BuildVRT 返回空，无法合并多波段")
                vrt_ds = None
                translate_ds = gdal.Translate(
                    final_tmp, vrt_path,
                    format="GTiff",
                    creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES",
                                    "BLOCKXSIZE=256", "BLOCKYSIZE=256"],
                )
                if translate_ds is None:
                    raise BandAcquisitionError(f"{band}: gdal.Translate 返回空，无法生成最终波段合并文件")
                translate_ds = None

            # ── 6. 重新打开校验后原子替换为正式文件名 ───────────
            def _build(dst_tmp_path: str) -> None:
                shutil.copy2(final_tmp, dst_tmp_path)

            def _validator(dst_tmp_path: str):
                try:
                    check_ds = gdal.Open(dst_tmp_path)
                except Exception as e:
                    return False, f"重新打开失败: {e}"
                if check_ds is None:
                    return False, "重新打开返回 None"
                try:
                    if check_ds.RasterCount != band_count:
                        return False, f"波段数 {check_ds.RasterCount} != 预期 {band_count}"
                    if check_ds.RasterXSize < 1 or check_ds.RasterYSize < 1:
                        return False, f"尺寸异常: {check_ds.RasterXSize}x{check_ds.RasterYSize}"
                    if not check_ds.GetProjection():
                        return False, "缺少投影信息"
                    sample_band = check_ds.GetRasterBand(1)
                    win_x = min(256, check_ds.RasterXSize)
                    win_y = min(256, check_ds.RasterYSize)
                    sample = sample_band.ReadAsArray(0, 0, win_x, win_y)
                    if sample is None:
                        return False, "无法读取采样窗口"
                finally:
                    check_ds = None
                return True, ""

            write_verified(_build, output_path, _validator)

            # ── 7. 自动内建 overview 金字塔（新下载影像即带金字塔，加速瓦片渲染）──
            _band_label = band if isinstance(band, str) else "+".join(band)
            _ovr_resampling = "NEAREST" if band in ("SCL", "qa_pixel", "QA_PIXEL") else "AVERAGE"
            self._build_overviews(
                output_path, _ovr_resampling,
                log_callback=log_callback, band_label=_band_label,
            )

            if download_errors and log_callback:
                for err in download_errors[:5]:
                    log_callback("WARN", err)
                if len(download_errors) > 5:
                    log_callback("WARN", f"...还有 {len(download_errors)-5} 个错误")

            if log_callback:
                log_callback("INFO",
                    f"已保存并校验通过 {band}: {output_path} ({band_count} 波段, {len(items_sorted)} 景 mosaic)")

            return s2_provenance if apply_s2_calibration else None

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 内建 overview 金字塔 ────────────────────────────────────

    @staticmethod
    def _build_overviews(tif_path: str, resampling: str = "AVERAGE",
                         log_callback=None, band_label: str = "") -> None:
        """为已落盘的 GeoTIFF 内建 overview 金字塔（2/4/8/16/32），加速瓦片渲染。

        对连续数据（DEM/LST/反射率）用 AVERAGE，对分类数据（SCL/QA）用 NEAREST。
        overview 是性能优化而非正确性要求，失败仅记 WARN，不中断下载流程。
        """
        try:
            from osgeo import gdal
            ds = gdal.Open(tif_path, gdal.GA_Update)
            if ds is None:
                if log_callback:
                    log_callback("WARN", f"  {band_label}: 打开文件建金字塔失败，将跳过")
                return
            try:
                if ds.GetRasterBand(1).GetOverviewCount() > 0:
                    return
                ds.BuildOverviews(resampling, [2, 4, 8, 16, 32])
            finally:
                ds.FlushCache()
                ds = None
            if log_callback:
                log_callback("INFO", f"  {band_label}: overview 金字塔构建完成")
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"  {band_label}: overview 构建失败（不影响数据可用性）: {e}")

    # ── 下载 asset ──────────────────────────────────────────────────

    @staticmethod
    def _fetch_asset(url: str, log_callback, headers: Optional[dict] = None,
                     progress_callback=None, progress_label: str = "") -> Optional[bytes]:
        """下载 asset 原始字节（流式，带实时进度回调），带重试（最多 5 次）

        每次失败用新 session（避免梯子切换导致连接池脏连接），
        重试前强制重建 socket。
        headers: 附加请求头（如 Data Space 的 Bearer token / S3 SigV4 签名）。
        progress_callback: 回调 (downloaded_bytes, total_bytes|None, label)，
                           每约 2MB 推送一次，供大文件下载时气泡持续更新。
        """
        import time as _time
        last_err = None
        for attempt in range(5):
            t0 = _time.time()
            timeout = min(120 + attempt * 45, 300)
            try:
                sess = requests.Session()
                sess.mount("https://", requests.adapters.HTTPAdapter(
                    pool_connections=0, pool_maxsize=0,
                    max_retries=0,
                ))
                sess.mount("http://", requests.adapters.HTTPAdapter(
                    pool_connections=0, pool_maxsize=0,
                    max_retries=0,
                ))
                sess.trust_env = False
                if log_callback and attempt == 0:
                    log_callback("INFO", f"  开始下载 ({timeout}s超时): {url[:70]}...")
                resp = sess.get(url, timeout=timeout, headers=headers, stream=True)
                resp.raise_for_status()
                try:
                    total_header = resp.headers.get("Content-Length")
                    total_bytes = int(total_header) if total_header else None
                    chunks = []
                    downloaded = 0
                    last_report = 0
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            chunks.append(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                # 每约 2MB 或下载完成时推送一次，避免事件过多
                                if (downloaded - last_report >= 2 * 1024 * 1024
                                        or (total_bytes and downloaded >= total_bytes)):
                                    progress_callback(downloaded, total_bytes, progress_label)
                                    last_report = downloaded
                    data = b"".join(chunks)
                finally:
                    resp.close()
                sess.close()
                elapsed = _time.time() - t0
                if log_callback:
                    log_callback("INFO", f"  下载完成 ({elapsed:.1f}s, {len(data)/1024/1024:.1f}MB)")
                return data
            except requests.exceptions.RequestException as e:
                elapsed = _time.time() - t0
                last_err = e
                is_network_flap = isinstance(e, (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ProxyError,
                    requests.exceptions.SSLError,
                    requests.exceptions.Timeout,
                ))
                if log_callback:
                    if is_network_flap and attempt < 4:
                        log_callback("WARN", f"  网络抖动 ({type(e).__name__}), 第{attempt+1}/5次重试...")
                    else:
                        log_callback("WARN", f"  下载失败 ({elapsed:.1f}s, 第{attempt+1}次): {e}")
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

        注意：planetary_computer 对同一存储账户/容器有进程级 TOKEN_CACHE
        （剩余有效期 >60s 时不刷新，token 有效期约 24h45m）。长驻服务里
        缓存的旧 token 会在长时间下载中途过期 → 后段波段全部 403
        （B02/B03/B04 成功、B08/B11 403 即此现象）。因此每次签名前
        清空缓存，强制向 token 服务取全新 token（st=当前时刻，有效期
        24h45m，单次下载内绝不会过期）。
        """
        try:
            import planetary_computer
            from planetary_computer import sas as pc_sas
            cache = getattr(pc_sas, "TOKEN_CACHE", None)
            if cache is not None:
                cache.clear()
            return planetary_computer.sign(item)
        except ImportError:
            pass

        try:
            collection_id = item.collection_id
            item_id = item.id
            sas_url = f"{_SAS_URL}/{collection_id}/{item_id}"
            resp = requests.get(sas_url, timeout=10)
            resp.raise_for_status()
            sas_data = resp.json()

            import copy
            signed_item = copy.deepcopy(item)
            for key, asset in signed_item.assets.items():
                if hasattr(asset, 'href') and asset.href:
                    token = sas_data.get(key, sas_data.get("token", ""))
                    if token:
                        sep = "&" if "?" in asset.href else "?"
                        asset.href = f"{asset.href}{sep}{token}"
            return signed_item
        except Exception:
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
        return [{"type": "Polygon", "coordinates": coords} for coords in geojson.get("coordinates", [[]])]

    return []
