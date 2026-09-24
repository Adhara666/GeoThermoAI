# -*- coding: utf-8 -*-
"""地理编码：把地名/POI/商圈解析为坐标或边界。

Provider 顺序（国内）：
  1) 本地边界库（区/街道已有边界 → 直接用面，无需 API）；
  2) 高德 Web 服务 API（需在「数据源」面板配置 Key；返回 GCJ-02，转 WGS84）；
  3) Nominatim（OSM，海外主用；国内可兜底但与高德相比 POI 覆盖弱）。

海外：
  1) 本地边界库（已下载的乡镇面）；
  2) Nominatim（中文提问走「中文名 + 英文别名」策略：OSM 收录了
     大量中文名称；构造查询时优先原词，其次去常见中文后缀）。

所有返回统一为 WGS84 {name, lon, lat, kind, source, extra}。
"""

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.boundaries.coordconv import gcj02_to_wgs84, wgs84_to_gcj02

_UA = {"User-Agent": "GeoThermoAI-GeoQA/1.0 (research; contact: local)"}
AMAP_GEOCODE = "https://restapi.amap.com/v3/geocode/geo"
AMAP_PLACE = "https://restapi.amap.com/v3/place/text"
AMAP_REGEO = "https://restapi.amap.com/v3/geocode/regeo"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

# 成功查询的进程内缓存（含变体结果）：同一地名重复提问秒回，
# 失败（空）不缓存——否则偶发网络失败会被永久固化为“查无此地”。
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 86400.0
_CACHE_MAX = 256


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: Any) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        try:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
        except ValueError:
            pass
    _CACHE[key] = (time.time(), val)


def _http_json(url: str, timeout: int = 15,
               retries: int = 2) -> Optional[dict]:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            if i == retries - 1:
                return None
            time.sleep(1.0)
    return None


# ── 高德 ────────────────────────────────────────────────────────

def amap_geocode(name: str, api_key: str,
                 city: str = "") -> List[Dict[str, Any]]:
    """高德地理编码 + POI 搜索（返回 GCJ→WGS84 后的候选）。

    city：可选城市限定（用于「重名」问题的第一层消歧——若用户提供了上下文）。
    """
    out: List[Dict[str, Any]] = []
    # 1) 地理编码（结构化地址）
    q = urllib.parse.urlencode({"key": api_key, "address": name,
                                "city": city or ""})
    data = _http_json(f"{AMAP_GEOCODE}?{q}")
    if data and data.get("status") == "1":
        for item in data.get("geocodes") or []:
            loc = str(item.get("location") or "")
            if "," not in loc:
                continue
            glon, glat = (float(x) for x in loc.split(",")[:2])
            wlon, wlat = gcj02_to_wgs84(glon, glat)
            full = str(item.get("formatted_address") or name)
            out.append({
                "name": full,
                "short": full.split(",")[0].strip() or name,
                "lon": wlon, "lat": wlat, "kind": "address",
                "source": "amap",
                "extra": {"level": item.get("level"),
                          "adcode": item.get("adcode"),
                          "city": item.get("city")},
            })
    # 2) POI 搜索（商圈、地标、机构）
    q2 = urllib.parse.urlencode({"key": api_key, "keywords": name,
                                 "city": city or "", "page_size": 8})
    data2 = _http_json(f"{AMAP_PLACE}?{q2}")
    if data2 and data2.get("status") == "1":
        for item in (data2.get("pois") or [])[:8]:
            loc = str(item.get("location") or "")
            if "," not in loc:
                continue
            glon, glat = (float(x) for x in loc.split(",")[:2])
            wlon, wlat = gcj02_to_wgs84(glon, glat)
            pname = str(item.get("name") or name)
            out.append({
                "name": pname,
                "short": pname,
                "lon": wlon, "lat": wlat, "kind": "poi",
                "source": "amap",
                "extra": {"type": item.get("type"),
                          "city": item.get("cityname"),
                          "adname": item.get("adname"),
                          "address": item.get("address")},
            })
    return out


# ── Nominatim（OSM） ────────────────────────────────────────────

def nominatim_search(name: str, *, limit: int = 6,
                     country: str = "") -> List[Dict[str, Any]]:
    """Nominatim 搜索（WGS84 直接返回）。country 可用英文国名做限定。"""
    query = name if not country else f"{name}, {country}"
    q = urllib.parse.urlencode({"q": query, "format": "json",
                                "limit": limit, "accept-language": "zh"})
    data = _http_json(f"{NOMINATIM}?{q}")
    out: List[Dict[str, Any]] = []
    for item in data or []:
        try:
            disp = str(item.get("display_name") or name)
            out.append({
                "name": disp,
                # 短名（display_name 首段）：回答正文用它，避免整条地址链
                "short": disp.split(",")[0].strip() or name,
                "lon": float(item["lon"]), "lat": float(item["lat"]),
                "kind": str(item.get("type") or "place"),
                "source": "nominatim",
                "extra": {"class": item.get("class"),
                          "importance": item.get("importance")},
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _query_variants(name: str) -> List[str]:
    """带括号/附加后缀的中文地名变体（提高地理编码命中率）。

    例：「中国地质大学（武汉）」→「中国地质大学」、「中国地质大学 武汉」。
    只做通用形态变换，不内置任何地名。
    """
    out: List[str] = []
    stripped = re.sub(r"[（(][^（）()]*[）)]", " ", name).strip()
    if stripped and stripped != name:
        out.append(stripped)
    inner = re.findall(r"[（(]([^（）()]*)[）)]", name)
    if inner and stripped:
        cand = (stripped + " " + " ".join(x for x in inner if x)).strip()
        if cand != name and cand not in out:
            out.append(cand)
    return out


def _describe_address(addr: Dict[str, Any]) -> str:
    """把 Nominatim address 字典整理成简短描述：主名，区域，城市（去重）。"""
    main = ""
    for k in ("amenity", "building", "leisure", "tourism", "shop",
              "office", "historic", "road", "pedestrian", "footway", "path"):
        if addr.get(k):
            main = str(addr[k])
            break
    area = ""
    for k in ("neighbourhood", "suburb", "quarter", "village", "town",
              "city_district", "district", "county", "state_district"):
        if addr.get(k):
            area = str(addr[k])
            break
    city = str(addr.get("city") or addr.get("state") or "")
    uniq: List[str] = []
    for p in (main, area, city):
        if p and p not in uniq:
            uniq.append(p)
    return "、".join(uniq)


def reverse_geocode(lon: float, lat: float, *, amap_key: str = "",
                    lang: str = "zh") -> str:
    """坐标 → 位置描述（选点识别用）：高德 regeo 优先（WGS84→GCJ-02），
    否则 Nominatim reverse。返回简短描述（如「鲁磨路，关山街道，武汉市」），
    无法识别时返回空串。"""
    key = "rev|%.5f,%.5f|%s" % (float(lon), float(lat), "amap" if amap_key else "nom")
    cached = _cache_get(key)
    if cached is not None:
        return str(cached)
    desc = ""
    if amap_key:
        try:
            glon, glat = wgs84_to_gcj02(float(lon), float(lat))
            q = urllib.parse.urlencode({
                "key": amap_key,
                "location": "%.6f,%.6f" % (glon, glat)})
            data = _http_json(f"{AMAP_REGEO}?{q}", timeout=8)
            if data and data.get("status") == "1":
                rg = data.get("regeocode") or {}
                comp = rg.get("addressComponent") or {}
                sn = comp.get("streetNumber") or {}
                street = str(sn.get("street") or "")
                district = str(comp.get("district") or "")
                city = str(comp.get("city") or comp.get("province") or "")
                parts = [p for p in (street, district, city) if p]
                desc = "、".join(parts) or str(rg.get("formatted_address") or "")
        except Exception:  # noqa: BLE001 — 高德失败自动回退 Nominatim
            desc = ""
    if not desc:
        try:
            q = urllib.parse.urlencode({
                "lat": "%.6f" % float(lat), "lon": "%.6f" % float(lon),
                "format": "json", "zoom": 18,
                "accept-language": "zh" if lang == "zh" else "en"})
            data = _http_json(f"{NOMINATIM_REVERSE}?{q}", timeout=8)
            if data:
                desc = _describe_address(data.get("address") or {})
        except Exception:  # noqa: BLE001
            desc = ""
    if desc:
        _cache_put(key, desc)
    return desc


# ── 统一入口 ────────────────────────────────────────────────────

def geocode(name: str, *, amap_key: str = "", prefer: str = "auto",
            country_hint: str = "") -> Dict[str, Any]:
    """统一地理编码入口。

    返回 {ok, candidates:[{name,lon,lat,kind,source}], provider}。
    prefer: auto|amap|nominatim；auto 时按「是否中国范围关键词」粗判。
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "candidates": [], "provider": ""}
    ck = "geo|%s|%s|%s|%s" % (name, prefer, country_hint,
                                "1" if amap_key else "0")
    hit = _cache_get(ck)
    if hit is not None:
        return dict(hit)

    candidates: List[Dict[str, Any]] = []
    provider = ""
    if prefer in ("auto", "amap") and amap_key and _looks_chinese(name):
        candidates = amap_geocode(name, amap_key)
        provider = "amap"
    if not candidates and prefer in ("auto", "nominatim"):
        candidates = nominatim_search(name, country=country_hint)
        provider = provider or "nominatim"
    # 主查询无结果 → 通用变体（去括号内容等）再试，提高括号类名称命中率
    if not candidates:
        for v in _query_variants(name):
            if prefer in ("auto", "amap") and amap_key and _looks_chinese(v):
                candidates = amap_geocode(v, amap_key)
                if candidates:
                    provider = "amap"
            if not candidates and prefer in ("auto", "nominatim"):
                candidates = nominatim_search(v, country=country_hint)
                if candidates and not provider:
                    provider = "nominatim"
            if candidates:
                break
    result = {"ok": bool(candidates), "candidates": candidates[:8],
              "provider": provider}
    if candidates:
        _cache_put(ck, result)
    return result


def _looks_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)
