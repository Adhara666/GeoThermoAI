"""
数据获取 Skill (Planetary Computer / Copernicus Data Space 版本 / rasterio 实现)

从 Microsoft Planetary Computer 下载遥感数据：
    - Landsat 8/9 Collection 2 Level-2 (地表温度 lwir11 + qa_pixel)
    - Sentinel-2 Level-2A (多光谱 + SCL)（优先 Copernicus Data Space，国内更快）
    - DEM (Copernicus GLO-30 / SRTM)

Planetary Computer 是微软托管的公开数据目录，无需注册即可免费下载。

处理流程：并发下载 COG → 保存到临时文件 → rasterio reproject(mosaic + UTM + clip) → 合并多波段
"""

import os
import json
import time
import threading
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

# Copernicus Data Space (Sentinel-2 加速下载，国内访问更快)
_DS_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
_DS_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
_DS_SENTINEL2_COLLECTION = "sentinel-2-l2a"

# 并发下载线程数（Landsat 提速）
_DOWNLOAD_WORKERS = 3

# 超时设置
SAT_API_TIMEOUT = 30  # STAC API 请求超时(秒)


class DataAcquisitionSkill(BaseSkill):
    """从 Planetary Computer 下载遥感数据（Landsat, Sentinel-2, DEM）"""

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
            _ = rasterio.__version__
            _ = transform_bounds
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

        # Sentinel-2 优先使用 Copernicus Data Space（国内更快），失败/未配置则回退 Planetary Computer
        ds_cfg = self._load_dataspace_config()
        ds_token = ""          # 用于 STAC 搜索（账号密码 或 OAuth2 Client 均可）
        s3_creds = None        # 用于 eodata 下载（S3 密钥，优先）
        if ds_cfg.get("username") or ds_cfg.get("client_id"):
            try:
                ds_token = self._get_dataspace_token(ds_cfg)
                if log_callback and not selected_pair and ds_token:
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
                    log_callback("INFO", f"[Planetary Computer] 找到 {len(sentinel2_items)} 景 Sentinel-2 影像")
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

        # Data Space 下载：优先 S3 签名（s3_creds），否则用 Bearer token（ds_token）
        # 两者都没有时走 Planetary Computer SAS
        ds_headers = {"Authorization": f"Bearer {ds_token}"} if ds_token else None

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
            auth_headers=ds_headers,
            s3_creds=s3_creds,
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
            auth_headers=ds_headers,
            s3_creds=s3_creds,
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
            },
            artifacts=list(output_paths.values()),
        )

    # ── Copernicus Data Space (Sentinel-2) ──────────────────────────

    @staticmethod
    def _load_dataspace_config() -> dict:
        """从 config/settings.json 读取 data_space 配置"""
        try:
            cfg_path = _ROOT / "config" / "settings.json"
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    return json.load(f).get("data_space", {})
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

    # ── 下载与合成（rasterio 实现）──────────────────────────────────

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
        auth_headers: Optional[dict] = None,
        s3_creds: Optional[dict] = None,
    ):
        """下载影像、mosaic合并多景（rasterio reproject 实现）

        流程：
        1. 签名所有 item，收集各波段 URL（并发下载提速）
        2. 逐波段 reproject(mosaic + UTM + clip) 一步完成
        3. 合并多波段

        逻辑与 GDAL 版 data_acquisition._download_composite 完全一致：
        - 按云量升序排列场景（云最小的优先）
        - 由 bbox 中心经度计算 UTM zone，直接重投影到 UTM 网格（对应 gdal.Warp 一步完成）
        - 逐波段：下载到临时文件 → reproject（bilinear/near 按波段类型）
        - mosaic 采用"后者优先"填充（与 gdal.Warp 默认行为一致）
        - nodata 约定与 GDAL 版一致：qa_pixel=1，DEM=None，其余=0
        - 研究区裁剪（对应 gdal.Warp 的 cutline/cropToCutline）

        auth_headers: 下载时附加的 HTTP 请求头（如 Data Space 的 Bearer token）。
                      s3_creds: Data Space eodata S3 密钥（access_key/secret_key），
                      提供时用 SigV4 签名下载，优先于 auth_headers。
                      两者都为 None 时走 Planetary Computer 的 SAS 签名逻辑。
        """
        import tempfile
        import rasterio
        from rasterio.warp import reproject, transform as _rio_transform, Resampling
        from rasterio.transform import from_origin
        from rasterio.windows import from_bounds as _window_from_bounds
        from rasterio.windows import Window as _Window
        from rasterio.features import geometry_mask
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _band_nodata(b):
            """各波段 nodata 约定（与 GDAL 版一致）：qa_pixel=1，DEM=None，其余=0"""
            if b in ("qa_pixel", "QA_PIXEL"):
                return 1
            if b == "data" or "dem" in str(b).lower():
                return None
            return 0

        if not items:
            if log_callback:
                log_callback("ERROR", f"未找到任何 {band} 影像，无法继续后续流程")
            raise RuntimeError(f"未找到任何 {band} 影像，请检查时间范围或云量阈值")

        bands_list = band if isinstance(band, list) else [band]
        band_count = len(bands_list)
        p_min, p_max = progress_range

        # 按云量排序，云最小的优先（与 GDAL 版一致）
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
        utm_crs = rasterio.crs.CRS.from_epsg(utm_epsg)

        if log_callback:
            log_callback("INFO", f"目标坐标系: {utm_epsg_str} (UTM Zone {utm_zone})")

        # ── 2. 转换 bbox 到 UTM（对应 GDAL 版 osr.CoordinateTransformation）──
        xs, ys = _rio_transform("EPSG:4326", utm_crs, [bbox[0], bbox[2]], [bbox[1], bbox[3]])
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)

        # 目标网格（对应 gdal.Warp 的 outputBounds + xRes/yRes）
        width = max(int(round((x2 - x1) / scale)), 1)
        height = max(int(round((y2 - y1) / scale)), 1)
        dst_transform = from_origin(x1, y2, scale, scale)

        # ── 3. 并发下载所有场景到本地临时文件 ───────────────────────
        band_files = {b: [] for b in bands_list}
        download_errors = []
        tmp_dir = tempfile.mkdtemp(prefix="rio_dl_")

        def _ds_https_url(asset_href):
            """把 Data Space 的 s3://eodata/... 转成可下载的 HTTPS 地址

            eodata S3 的 endpoint 是 https://eodata.dataspace.copernicus.eu，
            bucket 名是 eodata，所以 HTTPS 路径必须保留 /eodata/ 前缀。
            """
            if asset_href.startswith("s3://eodata/"):
                return "https://eodata.dataspace.copernicus.eu/eodata/" + asset_href[len("s3://eodata/"):]
            return asset_href

        def _find_ds_asset(assets, band_key):
            """Data Space 的 asset 名带分辨率后缀（如 B02_10m），做前缀匹配"""
            if band_key in assets:
                return assets[band_key]
            for k, v in assets.items():
                if k.startswith(band_key + "_"):
                    return v
            return None

        def _download_one(idx, item, b):
            """下载单个波段文件。返回 (idx, b, error_msg, data_bytes)"""
            if s3_creds or auth_headers:
                # Copernicus Data Space：asset 是 s3://eodata/ 形式
                asset = _find_ds_asset(item.assets, b)
                if not asset:
                    return idx, b, f"{item.id}: 缺少波段 {b}", None
                url = _ds_https_url(asset.href)
                if s3_creds:
                    # S3 SigV4 签名下载（推荐，不受账号密码/2FA 影响）
                    path = url.split("eodata.dataspace.copernicus.eu", 1)[1]
                    sig_headers = self._sign_s3_headers(
                        s3_creds["access_key"], s3_creds["secret_key"],
                        "GET", "eodata.dataspace.copernicus.eu", path,
                    )
                    data_bytes = self._fetch_asset(url, log_callback, headers=sig_headers)
                else:
                    # Bearer token 下载（需要 password grant 可用）
                    data_bytes = self._fetch_asset(url, log_callback, headers=auth_headers)
            else:
                # Planetary Computer：每个波段下载前重新签名（SAS token 有时效，避免 403）
                signed_item = self._sign_item(item)
                if not signed_item:
                    return idx, b, f"{item.id}: 签名失败", None
                asset = signed_item.assets.get(b)
                if not asset:
                    return idx, b, f"{item.id}: 缺少波段 {b}", None
                data_bytes = self._fetch_asset(asset.href, log_callback, headers=None)
            if data_bytes is None:
                return idx, b, f"{item.id}/{b}: 下载失败", None
            return idx, b, None, data_bytes

        jobs = [(idx, item, b) for idx, item in enumerate(items_sorted) for b in bands_list]
        if progress_callback:
            p = p_min + (p_max - p_min) * 0.3
            progress_callback(skill_name, p, f"并发下载 {len(jobs)} 个文件（{_DOWNLOAD_WORKERS} 线程）...")

        with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as executor:
            future_map = {executor.submit(_download_one, *j): j for j in jobs}
            for fut in as_completed(future_map):
                idx, b, err, data_bytes = fut.result()
                if err:
                    download_errors.append(err)
                    continue
                tmp_path = os.path.join(tmp_dir, f"{idx:03d}_{b}.tif")
                with open(tmp_path, "wb") as f:
                    f.write(data_bytes)
                band_files[b].append(tmp_path)

        # ── 4. 逐波段 reproject（mosaic + UTM + clip 一步到位）───
        band_arrays = []

        try:
            for b_idx, b in enumerate(bands_list):
                if progress_callback:
                    p = p_min + (p_max - p_min) * (0.3 + 0.7 * b_idx / max(len(bands_list), 1))
                    progress_callback(skill_name, p, f"处理 {b}...")

                urls = band_files[b]
                if not urls:
                    # 对应 GDAL 版"写入零值空波段"
                    if log_callback:
                        log_callback("WARN", f"  {b}: 无可用数据，写入空波段")
                    band_arrays.append(np.zeros((height, width), dtype=np.float32))
                    continue

                resample = Resampling.nearest if b in ("SCL", "qa_pixel", "QA_PIXEL") else Resampling.bilinear

                # 各波段 nodata 设置（与 GDAL 版一致）
                if b in ("qa_pixel", "QA_PIXEL"):
                    _src_nodata = 1
                    _dst_nodata = 1
                elif b == "data" or "dem" in b.lower():
                    _src_nodata = None
                    _dst_nodata = None
                else:
                    _src_nodata = 0
                    _dst_nodata = 0

                # 研究区裁剪掩膜（目标 UTM 网格上，对应 gdal.Warp 的 cutline/cropToCutline）
                clip_mask = None
                if study_area_geojson and os.path.isfile(study_area_geojson):
                    try:
                        with open(study_area_geojson, "r", encoding="utf-8") as f:
                            geojson_data = json.load(f)
                        polygons = _extract_polygons(geojson_data)
                        if polygons:
                            clip_mask = geometry_mask(
                                polygons, transform=dst_transform, invert=True,
                                out_shape=(height, width),
                            )
                    except Exception as e:
                        if log_callback:
                            log_callback("WARN", f"研究区裁剪掩膜生成失败（跳过裁剪）: {e}")

                # 内部统一用 NaN 标记无效像素，最后再转成目标 nodata
                mosaic_arr = np.full((height, width), np.nan, dtype=np.float32)

                for url in urls:
                    try:
                        with rasterio.open(url) as src:
                            src_crs = src.crs or utm_crs
                            # 读取源上覆盖目标范围的最小窗口（省内存，结果等价）
                            if src_crs != utm_crs:
                                xso, yso = _rio_transform(
                                    utm_crs, src_crs,
                                    [x1, x2, x2, x1], [y1, y1, y2, y2],
                                )
                                win_left, win_right = min(xso), max(xso)
                                win_bottom, win_top = min(yso), max(yso)
                            else:
                                win_left, win_right = x1, x2
                                win_bottom, win_top = y1, y2
                            window = _window_from_bounds(
                                win_left, win_bottom, win_right, win_top,
                                src.transform,
                            ).round_offsets().round_shape()
                            window = window.intersection(
                                _Window(0, 0, src.width, src.height))
                            if window.width < 1 or window.height < 1:
                                continue
                            # 带掩膜读取：文件自身 nodata 一并视为无效
                            src_masked = src.read(1, window=window, masked=True)
                            src_arr = np.asarray(src_masked.filled(np.nan), dtype=np.float32)
                            src_transform = src.window_transform(window)
                            # 显式 nodata 值也转 NaN（对应 GDAL 的 srcNodata 过滤）
                            if _src_nodata is not None:
                                src_arr[src_arr == _src_nodata] = np.nan
                    except Exception as e:
                        download_errors.append(f"{url}: {e}")
                        continue

                    dst_arr = np.full((height, width), np.nan, dtype=np.float32)
                    try:
                        reproject(
                            source=src_arr, destination=dst_arr,
                            src_transform=src_transform, src_crs=src_crs,
                            src_nodata=np.nan,
                            dst_transform=dst_transform, dst_crs=utm_crs,
                            dst_nodata=np.nan,  # 内部用 NaN 记录无效像素
                            resampling=resample,
                        )
                    except Exception as e:
                        download_errors.append(f"{url}: 重投影失败 {e}")
                        continue

                    # mosaic：后者优先（与 gdal.Warp 默认行为一致：
                    # 当前景有效像素覆盖已有结果，无效像素不覆盖）
                    valid = ~np.isnan(dst_arr)
                    if np.any(valid):
                        mosaic_arr[valid] = dst_arr[valid]

                # 研究区裁剪（对应 gdal.Warp cutline）
                if clip_mask is not None:
                    mosaic_arr[~clip_mask] = np.nan

                # NaN → 目标 nodata（DEM 无 nodata，用 0 占位）
                band_arrays.append(np.where(
                    np.isnan(mosaic_arr),
                    (_dst_nodata if _dst_nodata is not None else 0.0),
                    mosaic_arr,
                ).astype(np.float32))

            # ── 5. 写多波段 GeoTIFF（nodata 与 GDAL 版一致）──────────
            profile = {
                "driver": "GTiff",
                "height": height,
                "width": width,
                "count": band_count,
                "dtype": "float32",
                "crs": utm_crs,
                "transform": dst_transform,
                "compress": "lzw",
                "tiled": True,
                "blockxsize": 256,
                "blockysize": 256,
                "BIGTIFF": "YES",
            }
            out_nodata = _band_nodata(bands_list[0])
            if out_nodata is not None:
                profile["nodata"] = out_nodata
            with rasterio.open(output_path, "w", **profile) as dst_ds:
                for i, arr in enumerate(band_arrays):
                    dst_ds.write(arr, i + 1)

            # ── 6. 错误报告 ──────────────────────────────────────────
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
    def _fetch_asset(url: str, log_callback, headers: Optional[dict] = None) -> Optional[bytes]:
        """下载 asset 原始字节，带重试（最多 5 次，总超时 300s）

        每次失败用新 session（避免梯子切换导致连接池脏连接），
        重试前强制重建 socket。
        headers: 附加请求头（如 Data Space 的 Bearer token）。
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
                resp = sess.get(url, timeout=timeout, headers=headers)
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
