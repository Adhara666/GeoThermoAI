# -*- coding: utf-8 -*-
"""边界库下载器：DataV（国内市区）/ geoBoundaries（海外）/ OSM（国内街道）。

约定：
  - 所有写库数据统一为 WGS84 GeoJSON；
  - DataV 返回 GCJ-02，入库前用 coordconv 转换；
  - bbox 由几何边界计算，写入索引供覆盖范围判断。
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.boundaries.coordconv import gcj_geojson_to_wgs84
from core.boundaries.library import BoundaryLibrary

_UA = {"User-Agent": "GeoThermoAI-BoundaryFetcher/1.0 (research)"}

DATAV = "https://geo.datav.aliyun.com/areas_v3/bound/{code}.json"
DATAV_FULL = "https://geo.datav.aliyun.com/areas_v3/bound/{code}_full.json"
GEOBOUNDARIES_API = ("https://www.geoboundaries.org/api/current/gbOpen/"
                     "{iso}/{adm}/")
OVERPASS = "https://overpass-api.de/api/interpreter"


def _http_get(url: str, timeout: int = 90, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"下载失败（已重试 {retries} 次）：{url} - {last}")


def _geom_bbox(geojson: dict) -> Optional[List[float]]:
    xs, ys = [], []

    def walk(coords):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
            return
        for c in coords:
            walk(c)

    def walk_geom(g):
        if not g:
            return
        if g.get("type") == "GeometryCollection":
            for sub in g.get("geometries") or []:
                walk_geom(sub)
        else:
            walk(g.get("coordinates"))

    if geojson.get("type") == "FeatureCollection":
        for f in geojson.get("features") or []:
            walk_geom(f.get("geometry"))
    elif geojson.get("type") == "Feature":
        walk_geom(geojson.get("geometry"))
    else:
        walk_geom(geojson)
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _write_geojson(path: Path, geojson: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(geojson, ensure_ascii=False),
                    encoding="utf-8")


# ── 国内：DataV 市区级 ────────────────────────────────────────────

def fetch_cn_city(code: str, lib: BoundaryLibrary,
                  parent: str = "") -> Dict[str, Any]:
    """按行政区代码抓取「市 + 其下辖区县」。

    参数 code：市级 adcode（如武汉 420100）。
    产出：
      cn/{市名}/{市名}.geojson       level=city
      cn/{市名}/{区县名}.geojson      level=district
    """
    raw = json.loads(_http_get(DATAV_FULL.format(code=code)).decode("utf-8"))
    feats = raw.get("features") or []
    if not feats:
        raise RuntimeError(f"DataV 未返回数据：{code}")
    # 第一个 feature 是市本身（acroutes 长度 2 或 level=city）
    city_feat = None
    child_feats = []
    for f in feats:
        lvl = str((f.get("properties") or {}).get("level") or "")
        if lvl == "city" and city_feat is None:
            city_feat = f
        else:
            child_feats.append(f)
    if city_feat is None:  # 兜底：直接按 adcode 取市边界
        raw2 = json.loads(_http_get(DATAV.format(code=code)).decode("utf-8"))
        city_feat = (raw2.get("features") or [None])[0]
        if city_feat is None:
            raise RuntimeError(f"DataV 无市边界：{code}")

    out = {"city": "", "districts": []}
    city_props = city_feat.get("properties") or {}
    city_name = str(city_props.get("name") or code)
    city_gj = gcj_geojson_to_wgs84({
        "type": "FeatureCollection", "features": [city_feat]})
    city_gj["features"][0]["properties"] = {
        "name": city_name, "adcode": str(city_props.get("adcode") or code),
        "level": "city",
    }
    city_path = Path("cn") / city_name / f"{city_name}.geojson"
    _write_geojson(lib.root / city_path, city_gj)
    lib.add_entry(name=city_name, level="city", path=str(city_path),
                  source="datav", parent=parent,
                  adcode=str(city_props.get("adcode") or code),
                  bbox=_geom_bbox(city_gj))
    out["city"] = city_name

    for f in child_feats:
        props = f.get("properties") or {}
        name = str(props.get("name") or "")
        if not name:
            continue
        gj = gcj_geojson_to_wgs84({"type": "FeatureCollection", "features": [f]})
        gj["features"][0]["properties"] = {
            "name": name, "adcode": str(props.get("adcode") or ""),
            "level": "district", "parent": city_name,
        }
        rel = Path("cn") / city_name / f"{name}.geojson"
        _write_geojson(lib.root / rel, gj)
        lib.add_entry(name=name, level="district", path=str(rel),
                      source="datav", parent=city_name,
                      adcode=str(props.get("adcode") or ""),
                      bbox=_geom_bbox(gj))
        out["districts"].append(name)
    lib.save()
    return out


# ── 海外：geoBoundaries ──────────────────────────────────────────

def fetch_global(iso: str, adm: int, lib: BoundaryLibrary,
                 name_field: str = "shapeName") -> Dict[str, Any]:
    """下载某国某层级（如 USA/ADM2）的整包边界，登记为懒加载大文件。

    注意：geoBoundaries 元数据里 staticDownloadLink 是 **zip 包**，
    gjDownloadURL 才是 GeoJSON 直链；优先直链，拿到 zip 时解包抽 GeoJSON。
    """
    meta = json.loads(_http_get(GEOBOUNDARIES_API.format(
        iso=iso.upper(), adm=f"ADM{adm}")).decode("utf-8"))
    url = meta.get("gjDownloadURL") or meta.get("staticDownloadLink")
    if not url:
        raise RuntimeError(f"geoBoundaries 未给出下载链接：{iso} ADM{adm}")
    data = _http_get(url, timeout=600)
    if data[:2] == b"PK":
        # 兜底：拿到 zip 包时取其中的 GeoJSON/JSON 成员
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in zf.namelist() if n.lower().endswith(".geojson")]
        if not names:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError(f"geoBoundaries 压缩包内无 GeoJSON：{url}")
        gj = json.loads(zf.read(names[0]).decode("utf-8"))
    else:
        gj = json.loads(data.decode("utf-8"))
    name_field = name_field if any(
        name_field in (f.get("properties") or {})
        for f in (gj.get("features") or [])[:5]) else "shapeName"
    rel = Path("global") / iso.upper() / f"ADM{adm}" / \
        f"geoBoundaries-{iso.upper()}-ADM{adm}.geojson"
    _write_geojson(lib.root / rel, gj)
    count = len(gj.get("features") or [])
    lib.register_global_file(iso=iso, adm=adm, path=str(rel),
                             source="geoboundaries",
                             name_field=name_field, count=count)
    lib.save()
    return {"iso": iso.upper(), "adm": adm, "count": count,
            "path": str(rel), "canonical": meta.get("boundaryCanonical")}


# ── 国内：OSM 街道（尽力抓取） ───────────────────────────────────

def _osm_query(ql: str, timeout: int = 120) -> dict:
    from urllib.parse import quote
    url = OVERPASS + "?data=" + quote(ql)
    return json.loads(_http_get(url, timeout=timeout, retries=4).decode("utf-8"))


def _relation_to_polygon(rel: Dict[str, Any]) -> Optional[dict]:
    """把 Overpass `out geom` 的 relation 成员拼成（Multi）Polygon。

    用 shapely polygonize 自动拼环（比手写贪心更鲁棒：不要求 way 严格首尾
    相接的输入顺序，只要整体能围成闭合区域）；拼不出面积区域时返回 None
    （宁缺毋滥：宁可无边界也不给错边界）。
    """
    try:
        from shapely.geometry import LineString, shape
        from shapely.ops import polygonize, unary_union
    except ImportError:
        return None

    outer_lines, inner_lines = [], []
    for m in rel.get("members") or []:
        if m.get("type") != "way":
            continue
        pts = [(float(p["lon"]), float(p["lat"]))
               for p in (m.get("geometry") or []) if p]
        if len(pts) < 2:
            continue
        line = LineString(pts)
        (inner_lines if m.get("role") == "inner" else outer_lines).append(line)

    if not outer_lines:
        return None
    polys = list(polygonize(unary_union(outer_lines)))
    if not polys:
        return None
    merged = unary_union(polys)
    if merged.is_empty or not merged.is_valid:
        merged = merged.buffer(0)
    if merged.is_empty:
        return None

    holes = None
    if inner_lines:
        hole_polys = list(polygonize(unary_union(inner_lines)))
        if hole_polys:
            holes = unary_union(hole_polys)

    if merged.geom_type == "Polygon":
        if holes is not None:
            merged = merged.difference(holes)
        g = merged.simplify(0.0001)
        return {"type": "Polygon",
                "coordinates": [[[round(x, 6), round(y, 6)]
                                 for x, y in g.exterior.coords]]}
    if merged.geom_type == "MultiPolygon":
        out = []
        for poly in merged.geoms:
            if holes is not None:
                poly = poly.difference(holes)
            if poly.is_empty:
                continue
            if poly.geom_type == "MultiPolygon":
                for sub in poly.geoms:
                    out.append([[[round(x, 6), round(y, 6)]
                                 for x, y in sub.exterior.coords]])
            else:
                out.append([[[round(x, 6), round(y, 6)]
                             for x, y in poly.exterior.coords]])
        if not out:
            return None
        return {"type": "MultiPolygon", "coordinates": out}
    return None


def fetch_cn_streets(area_name: str, lib: BoundaryLibrary, *,
                     city_name: str = "", admin_levels: str = "7|8",
                     pause: float = 2.0, limit: int = 0) -> Dict[str, Any]:
    """抓取某区/市范围内全部街道（admin_level 7/8）的边界（有则入库）。

    area_name：OSM 中的区/市名（如「洪山区」），用于生成 area 过滤器。
    city_name：入库目录的上级（如「武汉市」）。
    """
    ql = (f'[out:json][timeout:120];'
          f'area["name"="{area_name}"]["admin_level"="6"]->.a;'
          f'rel["admin_level"~"^({admin_levels})$"]'
          f'["boundary"="administrative"](area.a);'
          f'out ids tags;')
    listing = _osm_query(ql, timeout=180)
    rels = [e for e in listing.get("elements", []) if e.get("type") == "relation"]
    out = {"area": area_name, "candidates": len(rels),
           "saved": [], "skipped": []}
    for i, rel in enumerate(rels):
        if limit and i >= limit:
            break
        name = str((rel.get("tags") or {}).get("name") or "")
        if not name:
            out["skipped"].append(str(rel.get("id")))
            continue
        time.sleep(pause)
        try:
            g = _osm_query(f'[out:json][timeout:120];rel({rel["id"]});out geom;',
                           timeout=180)
            el = (g.get("elements") or [None])[0]
            if not el:
                out["skipped"].append(name)
                continue
            geom = _relation_to_polygon(el)
            if geom is None:
                out["skipped"].append(name)
                continue
            gj = {"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {
                    "name": name, "level": "street",
                    "parent": city_name or area_name,
                    "osm_id": rel["id"]},
                "geometry": geom}]}
            rel_dir = Path("cn") / (city_name or area_name)
            area_dir = rel_dir / area_name if city_name else rel_dir
            path = area_dir / f"{name}.geojson"
            _write_geojson(lib.root / path, gj)
            lib.add_entry(name=name, level="street", path=str(path),
                          source="osm", parent=city_name or area_name,
                          bbox=_geom_bbox(gj))
            out["saved"].append(name)
        except Exception as e:  # noqa: BLE001
            out["skipped"].append(f"{name}:{type(e).__name__}")
    lib.save()
    return out
