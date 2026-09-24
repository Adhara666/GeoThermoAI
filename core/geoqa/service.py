# -*- coding: utf-8 -*-
"""地理问答编排：目标解析（库/地理编码）→ 统计 → 结构化回答。

对外唯一入口 answer_geo_query()。设计要点：
  - 目标解析优先级：地图选点 > 本地边界库（区/街道）> 高德 > Nominatim；
  - 「唯一命中才直接回答，多命中必追问」——绝不在有歧义时猜测；
  - 覆盖范围守卫：位置不在结果范围内时只说明、不给数字；
  - 数字全部来自统计结果（模板化输出，不经过模型，保证可溯源）。
"""

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Optional

from core.geoqa import geocode as gc
from core.geoqa import stats as gs


def _km_between(lon1, lat1, lon2, lat2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _is_same_place(cands: list) -> bool:
    """候选是否属于同一个地物的不同段/表述（无需追问）：

    - 仅 1 条候选；
    - 候选彼此邻近（前两条 <5km，原有规则）；
    - 短名相同且彼此 <20km（长街/园区多段命中，如「纽约百老汇」
      在 OSM 里是多个路段，短名都是“百老匯”；跨城重名相距上百公里，
      不会被合并）。
    """
    if len(cands) <= 1:
        return True
    top = cands[0]
    if all(_km_between(top["lon"], top["lat"], c["lon"], c["lat"]) < 5.0
           for c in cands[1:3]):
        return True
    short = str(top.get("short") or "").strip()
    return bool(short) and all(
        str(c.get("short") or "").strip() == short
        and _km_between(top["lon"], top["lat"], c["lon"], c["lat"]) < 20.0
        for c in cands[1:])


def _load_entry_geometry(lib, entry: dict) -> Optional[dict]:
    try:
        p = lib.resolve_path(entry)
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        feats = data.get("features") or []
        if feats:
            return feats[0].get("geometry")
    except (OSError, ValueError):
        return None
    return None


def _date_phrase(task_label: str) -> str:
    """从任务名中提取数据时段（如「2024 年 7 月」），用于回答正文。"""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", task_label or "")
    return "%s 年 %d 月" % (m.group(1), int(m.group(2))) if m else ""


def _full_date_phrase(acquisition_date: str, task_label: str,
                      lang: str = "zh") -> str:
    """观测日期短语：优先具体观测日（如 Sentinel-2 2024-07-22），

    缺失时退回任务名的月份（如 2024 年 7 月）。地表温度产品的观测日
    以 Sentinel-2 成像日期为准（降尺度特征源）。
    """
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$",
                 (acquisition_date or "").strip())
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return ("%s 年 %d 月 %d 日" % (y, mo, d)
                if lang == "zh" else "%s-%02d-%02d" % (y, mo, d))
    return _date_phrase(task_label)


def _fmt_stats(stats: Dict[str, Any], label: str, task_label: str,
               *, radius_m: int = 0, default_radius: bool = False,
               date: str = "", lang: str = "zh") -> str:
    mean_c = stats["mean_k"] - 273.15
    min_c = stats["min_k"] - 273.15
    max_c = stats["max_k"] - 273.15
    date = date or _date_phrase(task_label)
    note = stats.get("note") or ""
    gapf = "，无空洞版本" if "无空洞" in (task_label or "") else ""
    if lang == "zh":
        if radius_m:
            scope = "「%s」周围 %d 米范围内" % (label, radius_m)
            if default_radius:
                scope += "（未指定统计半径时的默认值）"
        else:
            scope = "「%s」范围内" % label
        text = ("%s，%s平均地表温度约 %.1f ℃"
                "（最低 %.1f ℃、最高 %.1f ℃%s）。"
                % (scope, date, mean_c, min_c, max_c, gapf))
        return text + ("（%s）" % note if note else "")
    if radius_m:
        scope = ("within %d m of \"%s\"" % (radius_m, label)
                 + (" (default radius)" if default_radius else ""))
    else:
        scope = "within \"%s\"" % label
    text = ("Mean land surface temperature %s%s: %.1f C"
            " (min %.1f C, max %.1f C)."
            % (scope, (" in %s" % date) if date else "", mean_c, min_c, max_c))
    return text + (" Note: %s" % note if note else "")


def _reply_out(label: str, task_label: str, lang: str,
               date: str = "") -> str:
    date = date or _date_phrase(task_label)
    if lang == "zh":
        when = ("当前已有结果对应 %s数据。" % date) if date else ""
        return (f"「{label}」不在已有地表温度结果的覆盖范围内，"
                f"所以没法给出这个位置的温度数值。{when}"
                f"可以先针对该地区生成地表温度任务后再查询。")
    when = ("The available result covers %s. " % date) if date else ""
    return (f"\"{label}\" is outside the coverage of the available LST "
            f"result, so no temperature value can be given. {when}"
            f"You can first run an LST task for that area and ask again.")


def _reply_no_data(label: str, lang: str) -> str:
    if lang == "zh":
        return (f"「{label}」落在结果范围内，但该范围内没有有效的地表温度"
                f"像元（可能为水体或无效区），无法给出温度数值。")
    return (f"\"{label}\" is inside the result extent but contains no valid "
            f"LST pixels; no temperature value is provided.")


def _reply_not_found(label: str, lang: str) -> str:
    if lang == "zh":
        return (f"没有找到「{label}」对应的位置。可以换更完整的名称"
                f"（如「武汉市洪山区」），或在地图上点选位置后直接提问。")
    return (f"Could not locate \"{label}\". Try a fuller name, or pick a "
            f"point on the map and ask about it.")


def resolve_target(*, target_text: str = "", lon: Optional[float] = None,
                   lat: Optional[float] = None, amap_key: str = "",
                   library=None, country_hint: str = "",
                   boundary_key: str = "",
                   coverage_bbox: Optional[list] = None,
                   lang: str = "zh") -> Dict[str, Any]:
    """解析查询目标。

    boundary_key：直接指定边界库条目（用户在“多个同名边界”追问卡中选定的）。
    coverage_bbox：已有结果的 WGS84 边界 [w,s,e,n]；用于重名消歧——
      若多个同名地点候选中恰有一个落在覆盖范围内，直接采用它（“能分清就不用问”）。
    返回 {status, kind, point, geometry, entry, label, candidates}。
    status：ok / ambiguous / not_found
    """
    label = (target_text or "").strip()

    # 0) 用户在追问卡中选定的边界条目（最高优先）
    if boundary_key and library is not None:
        entry = library.get_entry(boundary_key)
        if entry:
            geom = _load_entry_geometry(library, entry)
            if geom is not None:
                display = str(entry.get("name") or "")
                if entry.get("parent"):
                    display = f"{display}（{entry['parent']}）"
                return {"status": "ok", "kind": "polygon",
                        "geometry": geom, "entry": entry, "label": display}

    # 1) 地图选点优先
    if lon is not None and lat is not None:
        return {"status": "ok", "kind": "point", "point": (float(lon), float(lat)),
                "label": label or ("所选位置" if lang == "zh" else "selected point")}

    # 2) 本地边界库（区/街道等行政边界）
    if library is not None and label:
        hits = library.search(label, levels=("district", "street", "city"))
        strong = [h for h in hits if h.get("score", 0) >= 70]
        if strong:
            top = strong[0]
            same = [h for h in strong if h.get("score") == top.get("score")
                    and h.get("key") != top.get("key")]
            if not same:
                geom = _load_entry_geometry(library, top)
                if geom is not None:
                    return {"status": "ok", "kind": "polygon", "geometry": geom,
                            "entry": top,
                            "label": f"{top['name']}" + (
                                f"（{top.get('parent')}）" if top.get("parent") else "")}
            else:
                cands = [{"name": f"{h['name']}（{h.get('parent') or ''}）",
                          "value": h.get("key"), "kind": "boundary",
                          "detail": h.get("path")} for h in strong[:6]]
                return {"status": "ambiguous", "label": label,
                        "candidates": cands}

    # 3) 地理编码（高德 / Nominatim）
    if label:
        result = gc.geocode(label, amap_key=amap_key,
                            country_hint=country_hint)
        cands = result.get("candidates") or []
        # 覆盖范围优先消歧：同名地点里恰有一个落在已有结果范围内时直接用它
        if coverage_bbox and len(cands) > 1:
            w, s, e, n = coverage_bbox[:4]
            inside = [c for c in cands
                      if w <= float(c["lon"]) <= e and s <= float(c["lat"]) <= n]
            if inside:
                cands = inside
        if cands:
            # 同一地物的不同段/表述合并取第一条；跨城市重名则追问。
            # 例："纽约百老汇"命中同一条街的多个路段（短名相同）→ 取首条。
            top = cands[0]
            if _is_same_place(cands):
                return {"status": "ok", "kind": "point",
                        "point": (top["lon"], top["lat"]),
                        # 回答正文用短名（如「武商梦时代」），不用整条地址链
                        "label": str(top.get("short") or top.get("name")
                                     or label),
                        "source": result.get("provider")}
            return {"status": "ambiguous", "label": label,
                    "candidates": [
                        {"name": c.get("name"),
                         "short": c.get("short") or "",
                         "value": f'{c["lon"]:.6f},{c["lat"]:.6f}',
                         "kind": "place", "detail": c.get("source")}
                        for c in cands[:6]]}
    return {"status": "not_found", "label": label, "candidates": []}


def _library_region_desc(lon: float, lat: float, library) -> str:
    """本地边界库点在多边形兜底：返回「洪山区（武汉市）」级描述或空串。

    外网反向地理编码不可用时（实测偶发整段超时），用已入库边界给出
    区县/街道级位置，保证“这是哪里”至少能答到行政层级。
    """
    if library is None:
        return ""
    try:
        from shapely.geometry import Point, shape
    except Exception:  # noqa: BLE001
        return ""
    try:
        entries = (library.load() or {}).get("entries") or []
    except Exception:  # noqa: BLE001
        return ""
    pt = Point(float(lon), float(lat))
    # 按粒度取最具体命中（street > district > city）：
    # 条目列表中市/区混排，不能“先命中即占位”；需提升到最细层级
    _rank = {"city": 1, "district": 2, "street": 3}
    best_rank = 0
    best_desc = ""
    for e in entries:
        lvl = str(e.get("level") or "")
        if lvl not in _rank:
            continue
        geom_json = _load_entry_geometry(library, e)
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
            if not (geom.contains(pt) or geom.distance(pt) < 0.0006):
                continue
        except Exception:  # noqa: BLE001
            continue
        name = str(e.get("name") or "")
        parent = str(e.get("parent") or "")
        desc = name + (("（%s）" % parent) if parent else "")
        if _rank[lvl] > best_rank:
            best_desc = desc
            best_rank = _rank[lvl]
            if lvl == "street":
                break  # 已是最具体层级
    return best_desc


def identify_place(*, target_text: str = "", lon: Optional[float] = None,
                   lat: Optional[float] = None, amap_key: str = "",
                   country_hint: str = "", lang: str = "zh",
                   library=None) -> Dict[str, Any]:
    """位置识别（“这是哪里”）：选点反向地理编码 / 地名的位置描述。

    与温度问答无关，不含统计与覆盖守卫。返回 {status, reply, facts}：
      - status：ok（识别到位置）/ failed（识别失败）/ no_point（无选点无名）
      - reply：模板文本（模型不可用/校验不过时的回退）
      - facts：kind=identify + desc（位置描述，模型只能引用不得改动）
    """
    zh = lang == "zh"
    if lon is not None and lat is not None:
        try:
            desc = gc.reverse_geocode(float(lon), float(lat),
                                      amap_key=amap_key, lang=lang)
        except Exception:  # noqa: BLE001
            desc = ""
        if not desc:
            # 网络反向地理编码不可用 → 本地边界库兜底（区县/街道级）
            try:
                if library is None:
                    from core.boundaries import get_library
                    library = get_library()
                desc = _library_region_desc(float(lon), float(lat), library)
            except Exception:  # noqa: BLE001
                desc = ""
        if desc:
            return {"status": "ok",
                    "reply": (("这个点位于%s一带。" % desc) if zh else
                              ("The selected point is around %s." % desc)),
                    "facts": {"kind": "identify", "label": "", "desc": desc}}
        return {"status": "failed", "facts": {},
                "reply": ("没能识别出这个点的具体位置。可以在地图上微调一下再试，"
                          "或告诉我附近的道路 / 地标名称。" if zh else
                          "I could not identify this point. Try adjusting the "
                          "location on the map, or tell me a nearby road or "
                          "landmark.")}
    label = (target_text or "").strip()
    if label:
        result = gc.geocode(label, amap_key=amap_key,
                            country_hint=country_hint)
        cands = result.get("candidates") or []
        if cands:
            top = cands[0]
            segs = [s.strip() for s in str(top.get("name") or "").split(",")
                    if s.strip()]
            # 跳过门牌号段（如“388号”），取道路/街道/区等层级
            segs2 = [s for s in segs[1:] if not s.endswith("号")]
            area = "、".join(segs2[:3])
            if area:
                return {"status": "ok",
                        "reply": (("「%s」位于%s一带。" % (label, area)) if zh else
                                  ('"%s" is located around %s.' % (label, area))),
                        "facts": {"kind": "identify", "label": label,
                                  "desc": area}}
            if segs:
                return {"status": "ok",
                        "reply": (("找到了「%s」。" % label) if zh
                                  else ('Found "%s".' % label)),
                        "facts": {"kind": "identify", "label": label,
                                  "desc": segs[0]}}
        return {"status": "failed", "facts": {},
                "reply": (("没有找到「%s」对应的位置，可以换更完整的名称再试。"
                           % label) if zh else ('Could not locate "%s".' % label))}
    return {"status": "no_point", "facts": {},
            "reply": ("请先在地图上点选一个位置，或告诉我地名，我再告诉你它在哪儿。"
                      if zh else
                      "Pick a point on the map or tell me a place name first.")}


def answer_geo_query(*, target_text: str = "", lon: Optional[float] = None,
                     lat: Optional[float] = None, radius_m: int = 0,
                     task_id: str = "", amap_key: str = "", lang: str = "zh",
                     library=None, country_hint: str = "",
                     boundary_key: str = "",
                     pair_dates_resolver=None) -> Dict[str, Any]:
    """完整问答：解析目标 → 找结果 → 统计 → 生成回答。

    pair_dates_resolver：可选回调 task_id→{"sentinel2_date": "YYYY-MM-DD"}，
    用于把回答里的“数据时段”精确到 Sentinel-2 观测日（与该结果任务绑定）。
    """
    lib = library
    if lib is None:
        from core.boundaries import get_library
        lib = get_library()

    # 先定位结果（供覆盖范围优先消歧与统计使用）
    found = gs.find_result(task_id)
    coverage_bbox = gs.result_bbox_wgs84(found["path"]) if found else None
    # 配对的观测日期（具体到日）：跟随实际使用的结果任务解析
    acq_date = ""
    if pair_dates_resolver and found:
        try:
            pd = pair_dates_resolver(str(found.get("task_id") or "")) or {}
            acq_date = str(pd.get("sentinel2_date") or "")
        except Exception:  # noqa: BLE001 — 日期元数据缺失不影响统计
            acq_date = ""

    res = resolve_target(target_text=target_text, lon=lon, lat=lat,
                         amap_key=amap_key, library=lib,
                         country_hint=country_hint,
                         boundary_key=boundary_key,
                         coverage_bbox=coverage_bbox, lang=lang)
    if res["status"] == "ambiguous":
        return {"status": "ambiguous", "candidates": res["candidates"],
                "label": res["label"], "reply": ""}
    if res["status"] == "not_found":
        return {"status": "not_found", "label": res["label"],
                "reply": _reply_not_found(res["label"] or target_text, lang),
                "facts": {"kind": "guard", "guard": "not_found",
                          "label": res["label"] or target_text, "date": ""}}

    if not found:
        reply = ("当前没有已完成的地表温度结果，暂时无法查询地点温度。"
                 if lang == "zh" else
                 "There is no completed LST result yet, so place queries "
                 "cannot be answered right now.")
        return {"status": "no_result", "reply": reply,
                "facts": {"kind": "guard", "guard": "no_result",
                          "label": "", "date": ""}}

    task_label = found["label"] or found["task_id"]
    if found.get("kind") == "filled":
        task_label = (task_label + "（无空洞结果）") if lang == "zh" \
            else (task_label + " (gap-filled)")
    # 数据时段：优先具体观测日（Sentinel-2 成像日期），缺失退回月份
    date_text = _full_date_phrase(acq_date, task_label, lang)
    if res["kind"] == "point":
        default_radius = not radius_m
        r = radius_m or 500
        stats = gs.buffer_stats(found["path"], res["point"][0],
                                res["point"][1], float(r))
        scope_label, radius_used = res["label"], int(r)
    else:
        default_radius = False
        stats = gs.polygon_stats(found["path"], res["geometry"])
        scope_label, radius_used = res["label"], 0

    if not stats.get("ok"):
        reason = stats.get("reason") or ""
        if stats.get("coverage") == "out":
            reply = _reply_out(scope_label, task_label, lang, date=date_text)
            status = "out_of_coverage"
        else:
            reply = _reply_no_data(scope_label, lang)
            status = "no_data"
        return {"status": status, "reply": reply, "target": res,
                "tif": found, "stats": stats,
                "facts": {"kind": "guard", "guard": status,
                          "label": scope_label, "date": date_text}}

    # 结构化事实（供“模型说话”润色使用）：数字在此一次性定妥，
    # 后续任何文本层（模板/模型）只能引用，不得再计算。
    facts = {
        "kind": "temperature",
        "label": scope_label,
        "geometry": res["kind"],
        "radius_m": radius_used,
        "default_radius": bool(default_radius and res["kind"] == "point"),
        "date": date_text,
        "source": ("Sentinel-2" if acq_date else ""),
        "mean_c": round(stats["mean_k"] - 273.15, 1),
        "min_c": round(stats["min_k"] - 273.15, 1),
        "max_c": round(stats["max_k"] - 273.15, 1),
        "mean_k": round(stats["mean_k"], 1),
        "gap_filled": "无空洞" in (task_label or ""),
        "note": str(stats.get("note") or ""),
    }
    reply = _fmt_stats(stats, scope_label, task_label,
                       radius_m=radius_used, default_radius=default_radius,
                       date=date_text, lang=lang)
    return {"status": "ok", "reply": reply, "facts": facts,
            "stats": stats, "target": res, "tif": found}
