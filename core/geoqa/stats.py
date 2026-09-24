# -*- coding: utf-8 -*-
"""结果统计：对已完成的 LST GeoTIFF 做点缓冲/多边形统计，并判定覆盖范围。

设计原则：
  - 只做确定性计算（rasterio/numpy），数字可溯源；
  - 位置在结果范围外或区域无有效像元时，明确返回 out_of_coverage /
    no_valid_data，绝不返回误导性数值；
  - 全部输入输出坐标为 WGS84（栅格内部 CRS 由 rasterio 自动换算）。
"""

import math
from pathlib import Path
from typing import Any, Dict, Optional

NODATA_EPS = 1e-6


def _valid_stats(arr) -> Dict[str, Any]:
    import numpy as np
    mask = np.isfinite(arr)
    vals = arr[mask]
    if vals.size == 0:
        return {"count": 0, "valid": 0, "mean_k": None, "min_k": None,
                "max_k": None}
    return {
        "count": int(arr.size),
        "valid": int(vals.size),
        "mean_k": float(vals.mean()),
        "min_k": float(vals.min()),
        "max_k": float(vals.max()),
        "std_k": float(vals.std()),
    }


def _open_valid(path: str):
    import rasterio
    src = rasterio.open(path)
    nodata = src.nodata
    return src, nodata


def _mask_nodata(arr, nodata):
    import numpy as np
    out = arr.astype("float64", copy=True)
    if nodata is not None:
        out[np.isclose(out, nodata)] = np.nan
    out[out < 100.0] = np.nan   # 物理下限（地表温度 <100K 视为无效；防 0 填充）
    out[out > 400.0] = np.nan   # 物理上限（>400K 视为无效）
    return out


def buffer_stats(tif_path: str, lon: float, lat: float,
                 radius_m: float) -> Dict[str, Any]:
    """对 (lon,lat) 周围 radius_m 米缓冲做统计。"""
    import numpy as np
    src, nodata = _open_valid(tif_path)
    try:
        from rasterio.warp import transform as _warp_tf
        xs, ys = _warp_tf("EPSG:4326", src.crs, [float(lon)], [float(lat)])
        x, y = xs[0], ys[0]
        b = src.bounds
        # 点与栅格范围的关系（缓冲半径内缩/外扩判断）
        margin = radius_m
        if (x < b.left - margin or x > b.right + margin
                or y < b.bottom - margin or y > b.top + margin):
            return {"ok": False, "coverage": "out",
                    "reason": "该位置不在已有地表温度结果的覆盖范围内"}

        # 缓冲窗口（按 CRS 单位 = 米；地理坐标系时用近似换算）
        if src.crs and src.crs.is_geographic:
            # 度单位：纬度 1°≈111320m；经度按 cos(lat) 修正
            deg_y = radius_m / 111320.0
            deg_x = radius_m / (111320.0 * max(0.1, math.cos(math.radians(lat))))
        else:
            deg_x = deg_y = radius_m
        left, right = x - deg_x, x + deg_x
        bottom, top = y - deg_y, y + deg_y
        window = src.window(left, bottom, right, top)
        win = window.round_offsets().round_lengths()
        if win.width <= 0 or win.height <= 0:
            return {"ok": False, "coverage": "out",
                    "reason": "该位置不在已有地表温度结果的覆盖范围内"}
        # 与栅格求交，判断覆盖是否完整
        inter = window.intersection(
            __import__("rasterio").windows.Window(
                0, 0, src.width, src.height))
        if inter.width <= 0 or inter.height <= 0:
            return {"ok": False, "coverage": "out",
                    "reason": "该位置不在已有地表温度结果的覆盖范围内"}
        ratio_area = (inter.width * inter.height) / max(
            1e-9, window.width * window.height)
        data = src.read(1, window=win)
        arr = _mask_nodata(np.asarray(data, dtype="float64"), nodata)
        stats = _valid_stats(arr)
        if stats["valid"] == 0:
            return {"ok": False, "coverage": "in",
                    "reason": "该范围内没有有效的地表温度像元"}
        stats.update({
            "ok": True,
            "coverage": "full" if ratio_area > 0.995 else "partial",
            "buffer_radius_m": radius_m,
            "center_lon": float(lon), "center_lat": float(lat),
            "pixel_size_m": _pixel_size_m(src),
            "unit": "K",
        })
        if stats["coverage"] == "partial":
            stats["note"] = ("该地点贴近结果边界，统计仅覆盖落在结果范围内的"
                             "部分（约 %.0f%%）" % (ratio_area * 100))
        return stats
    finally:
        src.close()


def polygon_stats(tif_path: str, geometry: Dict[str, Any]) -> Dict[str, Any]:
    """对 WGS84 GeoJSON 几何做分区统计（自动与栅格求交）。"""
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rmask

    src, nodata = _open_valid(tif_path)
    try:
        b = src.bounds
        from rasterio.warp import transform_bounds
        wb = transform_bounds(src.crs, "EPSG:4326",
                              b.left, b.bottom, b.right, b.top,
                              densify_pts=8) if src.crs else [b.left, b.bottom,
                                                              b.right, b.top]
        gb = _geom_bbox_4326(geometry)
        if gb is None:
            return {"ok": False, "coverage": "out", "reason": "目标边界无效"}
        inter_w = min(wb[2], gb[2]) - max(wb[0], gb[0])
        inter_h = min(wb[3], gb[3]) - max(wb[1], gb[1])
        if inter_w <= 0 or inter_h <= 0:
            return {"ok": False, "coverage": "out",
                    "reason": "该位置不在已有地表温度结果的覆盖范围内"}
        area_w = (gb[2] - gb[0]) * (gb[3] - gb[1])
        area_i = inter_w * inter_h
        ratio = area_i / max(1e-9, area_w)

        # ⚠ rasterio.mask 不做坐标系转换：必须先把 WGS84 几何投影到
        # 栅格自身的 CRS（否则"输入形状与栅格不重叠"）
        from rasterio.warp import transform_geom
        geom_proj = geometry
        if src.crs and str(src.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
            geom_proj = transform_geom("EPSG:4326", src.crs, geometry)
        try:
            out, _ = rmask(src, [geom_proj], crop=True, filled=False,
                           nodata=nodata if nodata is not None else np.nan)
        except ValueError:
            return {"ok": False, "coverage": "out",
                    "reason": "该位置不在已有地表温度结果的覆盖范围内"}
        arr = np.ma.asarray(out[0], dtype="float64").filled(np.nan)
        arr = _mask_nodata(arr, nodata)
        stats = _valid_stats(arr)
        if stats["valid"] == 0:
            return {"ok": False, "coverage": "in",
                    "reason": "该范围内没有有效的地表温度像元"}
        stats.update({
            "ok": True,
            "coverage": "full" if ratio > 0.995 else "partial",
            "polygon_area_ratio": round(ratio, 4),
            "pixel_size_m": _pixel_size_m(src),
            "unit": "K",
        })
        if ratio <= 0.995:
            stats["note"] = ("该边界仅与结果范围部分重叠（约 %.0f%%），"
                             "统计仅覆盖重叠部分" % (ratio * 100))
        return stats
    finally:
        src.close()


def coverage_of_point(tif_path: str, lon: float, lat: float) -> Dict[str, Any]:
    """仅判定点是否在结果范围内（不取值）。"""
    src, _ = _open_valid(tif_path)
    try:
        from rasterio.warp import transform as _warp_tf
        xs, ys = _warp_tf("EPSG:4326", src.crs, [float(lon)], [float(lat)])
        x, y = xs[0], ys[0]
        b = src.bounds
        inside = (b.left <= x <= b.right) and (b.bottom <= y <= b.top)
        return {"ok": True, "inside": bool(inside)}
    finally:
        src.close()


def _pixel_size_m(src) -> Optional[float]:
    try:
        t = src.transform
        px = abs(t.a)
        if src.crs and src.crs.is_geographic:
            return float(px * 111320.0)
        return float(px)
    except Exception:  # noqa: BLE001
        return None


def _geom_bbox_4326(geometry: Dict[str, Any]):
    xs, ys = [], []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
            return
        for x in c:
            walk(x)

    if not geometry:
        return None
    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            r = _geom_bbox_4326(g)
            if r:
                xs.extend([r[0], r[2]])
                ys.extend([r[1], r[3]])
    else:
        walk(geometry.get("coordinates"))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def result_bbox_wgs84(tif_path: str) -> Optional[list]:
    """返回结果栅格的 WGS84 边界 [w, s, e, n]（供覆盖范围优先消歧用）。"""
    try:
        src, _ = _open_valid(tif_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        b = src.bounds
        from rasterio.warp import transform_bounds
        wb = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom,
                              b.right, b.top,
                              densify_pts=8) if src.crs else \
            [b.left, b.bottom, b.right, b.top]
        return [float(wb[0]), float(wb[1]), float(wb[2]), float(wb[3])]
    except Exception:  # noqa: BLE001
        return None
    finally:
        src.close()


def find_result(task_id: str = "") -> Optional[Dict[str, Any]]:
    """在台账里找某任务（或最近完成 full_lst 任务）的正式 LST 产物。

    两级定位：
      1) artifact 表中 available 且文件存在的 export geotiff；
      2) 运行目录产物已被搬到项目视图（availability=cleaned）时，
         按 run.project_dir / 任务名目录回退查找 results/predict_10m
         与根目录下的 rf_10m_lst_final*.tif。
    返回 {path, label, task_id} 或 None。
    """
    import glob
    import sqlite3
    from core.deployment import DeploymentConfig
    db = Path(DeploymentConfig.load().state_db)
    if not db.is_file():
        return None
    conn = sqlite3.connect(str(db))
    try:
        base = ("SELECT a.path, a.availability, t.label, t.id, r.project_dir"
                " FROM artifacts a"
                " JOIN attempts x ON x.id = a.attempt_id"
                " JOIN nodes n ON n.id = x.node_id"
                " JOIN runs r ON r.id = n.run_id"
                " JOIN tasks t ON t.id = r.task_id"
                " WHERE n.node_type = 'export'"
                " AND a.type = 'geotiff'")
        if task_id:
            row = conn.execute(base + " AND r.task_id = ?"
                               " ORDER BY a.rowid DESC LIMIT 1",
                               (task_id,)).fetchone()
        else:
            row = conn.execute(base + " AND t.summary_status = 'completed'"
                               " ORDER BY a.rowid DESC LIMIT 1").fetchone()
        if not row:
            return None
        path, availability, label, tid, project_dir = row
        if availability == "available" and path and Path(path).is_file():
            return {"path": str(path), "label": str(label or ""),
                    "task_id": str(tid or ""), "kind": "main"}
        # 回退：项目视图目录（run 目录产物已搬走/清理时链接可能悬空，
        # 命中后必须用 is_file（跟随软链）校验真身存在）。
        # 优先级：主图 > 无空洞结果（无空洞是更完整的替代）。
        roots = []
        if project_dir:
            if label:
                roots.append(Path(project_dir) / str(label))
            roots.append(Path(project_dir))
        picks = (("results/predict_10m/rf_10m_lst_final*.tif", "main"),
                 ("rf_10m_lst_final*.tif", "main"),
                 ("**/rf_10m_lst_final*.tif", "main"),
                 ("lst_filled*.tif", "filled"),
                 ("**/lst_filled*.tif", "filled"))
        for root in roots:
            if not root.is_dir():
                continue
            for pat, kind in picks:
                hits = [h for h in sorted(glob.glob(str(root / pat),
                                                   recursive=True))
                        if Path(h).is_file()]
                if hits:
                    return {"path": hits[-1], "label": str(label or ""),
                            "task_id": str(tid or ""), "kind": kind}
        return None
    finally:
        conn.close()


def find_result_tif(task_id: str = "") -> Optional[str]:
    """兼容入口：只返回路径（内部调用 find_result）。"""
    r = find_result(task_id)
    return r["path"] if r else None
