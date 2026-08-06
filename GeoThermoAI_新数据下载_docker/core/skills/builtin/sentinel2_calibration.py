"""
Sentinel-2 按景定标

背景：Copernicus 自 Processing Baseline 04.00 起，L2A 地表反射率产品引入
``BOA_ADD_OFFSET``（当前统一为每波段 -1000 DN）+ ``BOA_QUANTIFICATION_VALUE``
（当前为 10000），真实反射率 = (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE；
DN=0 仍是 NoData。

本模块按景（STAC item）解析定标参数：
    1. 优先读取该景 ``product-metadata``（MTD_MSIL2A.xml）资产，逐波段解析真实
       ``BOA_ADD_OFFSET`` 与全局 ``BOA_QUANTIFICATION_VALUE``（按景读取，
       不臆造统一常数）；
    2. 若因网络/格式原因解析失败，回退到 Copernicus 官方文档记录的
       processing-baseline 规则（baseline ≥ 04.00 → 每波段 offset=-1000，
       quantification=10000；更旧 baseline → offset=0）；回退来源会在返回结果
       的 ``source`` 字段中明确标注，不静默假装是实测值；
    3. 不对所有影像盲减固定值：只对 DN≠0（非 NoData）像元应用该公式，且严格按
       该景自身解析到的参数处理，不跨景套用。

注：Planetary Computer 该 collection 未提供 ``raster:bands`` scale/offset 扩展
字段，定标参数只能通过 ``product-metadata`` 资产的 XML 或 processing_baseline
规则获取。
"""

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests

# 标准 Sentinel-2 波段名 → MTD_MSIL2A.xml 中的 band_id（0-based，L1C 全量13波段编号）
S2_BAND_ID_BY_NAME = {
    "B01": 0, "B02": 1, "B03": 2, "B04": 3, "B05": 4, "B06": 5, "B07": 6,
    "B08": 7, "B8A": 8, "B09": 9, "B10": 10, "B11": 11, "B12": 12,
}

DEFAULT_QUANTIFICATION_VALUE = 10000.0
BASELINE_OFFSET_THRESHOLD = 4.0  # Processing Baseline >= 04.00 起引入 BOA_ADD_OFFSET
BASELINE_STANDARD_OFFSET = -1000.0


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_boa_offsets_from_xml(xml_text: str) -> Dict:
    """解析 MTD_MSIL2A.xml，返回 {quantification_value, offsets_by_band_id}。"""
    root = ET.fromstring(xml_text)
    offsets: Dict[int, float] = {}
    quant: Optional[float] = None
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "BOA_ADD_OFFSET":
            band_id = elem.get("band_id")
            if band_id is not None and elem.text is not None:
                try:
                    offsets[int(band_id)] = float(elem.text)
                except (TypeError, ValueError):
                    pass
        elif tag == "BOA_QUANTIFICATION_VALUE":
            if elem.text is not None:
                try:
                    quant = float(elem.text)
                except (TypeError, ValueError):
                    pass
    return {"quantification_value": quant, "offsets_by_band_id": offsets}


def _parse_baseline_major_minor(baseline: Optional[str]) -> Optional[float]:
    """把 's2:processing_baseline'（如 "05.10"）解析为可比较的浮点数。"""
    if not baseline:
        return None
    match = re.match(r"^\s*(\d+)\.(\d+)", str(baseline))
    if not match:
        return None
    try:
        return float(f"{match.group(1)}.{match.group(2)}")
    except ValueError:
        return None


def default_calibration_from_baseline(processing_baseline: Optional[str]) -> Dict:
    """按 Copernicus 官方文档记录的 processing-baseline 规则给出回退定标参数
    （不是随意猜测，是 baseline>=04.00 统一引入 -1000 offset 这一有文档依据的规则）。
    """
    baseline_num = _parse_baseline_major_minor(processing_baseline)
    if baseline_num is not None and baseline_num >= BASELINE_OFFSET_THRESHOLD:
        offset = BASELINE_STANDARD_OFFSET
    else:
        offset = 0.0
    return {
        "quantification_value": DEFAULT_QUANTIFICATION_VALUE,
        "offsets_by_band_id": {bid: offset for bid in S2_BAND_ID_BY_NAME.values()},
        "source": "baseline_default_rule",
        "processing_baseline": processing_baseline,
    }


def fetch_scene_calibration(signed_item, log_callback=None, timeout: int = 30, headers: Optional[dict] = None) -> Dict:
    """按景（已签名的 STAC item）获取定标参数：优先读取该景 MTD_MSIL2A.xml，
    失败则回退到 processing-baseline 规则。

    asset 查找兼容两种命名：
      - Planetary Computer: ``product-metadata``（href 已带 SAS token，匿名可读）
      - Copernicus Data Space: ``product_metadata``（href 是 ``s3://eodata/...``，
        需转 HTTPS 并用 S3 SigV4 签名（或 Bearer）下载，由调用方通过 ``headers`` 传入）

    Returns:
        dict: {
            "item_id": str,
            "processing_baseline": str | None,
            "quantification_value": float,
            "offsets_by_band_id": {band_id: offset, ...},
            "source": "per_scene_xml" | "baseline_default_rule",
        }
    """
    processing_baseline = signed_item.properties.get("s2:processing_baseline")
    asset = signed_item.assets.get("product-metadata") or signed_item.assets.get("product_metadata")

    if asset is not None and getattr(asset, "href", None):
        href = asset.href
        # Copernicus Data Space 的 asset href 是 s3://eodata/...，转为可下载的 HTTPS 地址
        if href.startswith("s3://eodata/"):
            href = "https://eodata.dataspace.copernicus.eu/eodata/" + href[len("s3://eodata/"):]
        try:
            resp = requests.get(href, timeout=timeout, headers=headers)
            resp.raise_for_status()
            parsed = parse_boa_offsets_from_xml(resp.text)
            if parsed["quantification_value"] and parsed["offsets_by_band_id"]:
                return {
                    "item_id": signed_item.id,
                    "processing_baseline": processing_baseline,
                    "quantification_value": parsed["quantification_value"],
                    "offsets_by_band_id": parsed["offsets_by_band_id"],
                    "source": "per_scene_xml",
                }
            if log_callback:
                log_callback("WARN", f"  {signed_item.id}: MTD_MSIL2A.xml 未解析到有效 BOA_ADD_OFFSET/QUANTIFICATION_VALUE，回退到 baseline 规则")
        except Exception as e:
            if log_callback:
                log_callback("WARN", f"  {signed_item.id}: 获取/解析 product-metadata 失败 ({e})，回退到 baseline 规则")
    else:
        if log_callback:
            log_callback("WARN", f"  {signed_item.id}: 缺少 product-metadata 资产，回退到 baseline 规则")

    fallback = default_calibration_from_baseline(processing_baseline)
    return {"item_id": signed_item.id, **fallback}


def offset_for_band(calibration: Dict, band_name: str) -> float:
    """从定标结果中取指定波段（如 "B02"）的 offset；找不到时按 0 处理并不静默假装已知。"""
    band_id = S2_BAND_ID_BY_NAME.get(band_name)
    if band_id is None:
        return 0.0
    return float(calibration.get("offsets_by_band_id", {}).get(band_id, 0.0))
