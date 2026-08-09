"""
数据轻反思：确定性规则 D1–D7（技术方案 5.3）

在 `data_acquisition → data_pipeline → ttri_compute` 三步全部执行完之后统一做一次。
**任一规则不通过 → 禁止往下跑**（修复 1.5(4)「_check_exceptions 不拦截」）。
LLM 只负责把失败原因翻译成人话并给出建议排序，不参与放行决策。

各探针（栅格 / CSV / 元数据）都可注入，便于合成测试零依赖运行。
"""

import glob
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .result import Action, ReflectionResult

logger = logging.getLogger(__name__)

# 与现有 `_check_exceptions` 阈值一致
MIN_TRAIN_ROWS = 10000
# 云掩膜后有效像元占比下限
MIN_VALID_RATIO = 0.15
# 任一划分集合占比低于此值判为严重失衡
MIN_SPLIT_SHARE = 0.05

RULES = {
    "D1": "五个栅格文件均存在、非零字节、可被打开",
    "D2": "数据获取、预处理、地形指数三个阶段均记录为完成",
    "D3": f"训练样本数不少于 {MIN_TRAIN_ROWS}",
    "D4": "约束层行数与预测有效像元数均大于 0",
    "D5": "训练、验证、测试集行数均大于 0 且比例未严重失衡",
    "D6": "训练、验证、测试数据中存在地形热响应指数列且取值有变化",
    "D7": f"云掩膜后有效像元占比不低于 {int(MIN_VALID_RATIO * 100)}%",
}

# D1 检查的五个栅格（相对 raw 目录）
REQUIRED_RASTERS = ("landsat_lst.tif", "landsat_qa_pixel.tif",
                    "sentinel2_bands.tif", "sentinel2_scl.tif", "dem.tif")

# D2 要求已完成的阶段
REQUIRED_STAGES = ("data_acquisition", "data_pipeline", "ttri_compute")

# D6 检查的三个划分 CSV
SPLIT_CSVS = ("train.csv", "validate.csv", "test.csv")

# 每条规则不通过时的建议动作（按可行性排序，供 LLM 与气泡使用）
SUGGESTIONS = {
    "D1": ["换一组影像组合重新下载", "更换数据源后重试"],
    "D2": ["查看日志定位失败阶段", "换一组影像组合重跑"],
    "D3": ["换一组云量更低的影像组合", "换一个时间段"],
    "D4": ["换一组影像组合", "检查研究区范围是否与影像重叠"],
    "D5": ["换一组影像组合", "调整训练验证测试的划分比例"],
    "D6": ["换一组影像组合", "检查数字高程数据是否正常"],
    "D7": ["换一组云量更低的影像组合", "换一个时间段"],
}


# ── 默认探针 ───────────────────────────────────────────────────────

def default_raster_probe(path: str) -> Tuple[bool, str]:
    """默认栅格探针：存在 + 非零字节 + 可被打开。"""
    if not os.path.isfile(path):
        return False, "文件不存在"
    try:
        if os.path.getsize(path) == 0:
            return False, "文件为空"
    except OSError as e:
        return False, f"无法读取文件大小（{e}）"
    try:
        import rasterio

        with rasterio.open(path) as ds:
            if ds.width < 1 or ds.height < 1:
                return False, "栅格尺寸异常"
        return True, ""
    except ImportError:
        return True, ""
    except Exception as e:
        return False, f"无法打开栅格（{e}）"


def default_csv_probe(path: str) -> Dict[str, Any]:
    """默认 CSV 探针：返回 {exists, has_ttri, ttri_std, rows}。"""
    out = {"exists": os.path.isfile(path), "has_ttri": False, "ttri_std": None, "rows": 0}
    if not out["exists"]:
        return out
    try:
        import pandas as pd

        head = pd.read_csv(path, nrows=1)
        out["has_ttri"] = "TTRI" in head.columns
        if out["has_ttri"]:
            sample = pd.read_csv(path, usecols=["TTRI"], nrows=20000)
            series = sample["TTRI"].dropna()
            out["rows"] = int(len(sample))
            out["ttri_std"] = float(series.std()) if len(series) > 1 else 0.0
            if series.empty:
                out["ttri_std"] = None
    except Exception as e:
        logger.warning(f"[data] CSV 探针失败（按未通过处理）: {e}")
    return out


def default_meta_probe(path: str) -> Dict[str, Any]:
    """默认元数据探针：读 30m 网格的 height/width，以及有效像元占比。

    占比口径统一：优先读研究区多边形口径（region_pixels>0 时的 region_valid_ratio），
    未提供研究区时回退 bbox 全网格口径（valid_ratio）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        region_pixels = _as_int(data.get("region_pixels"))
        region_ratio = data.get("region_valid_ratio")
        if region_pixels > 0 and isinstance(region_ratio, (int, float)):
            valid_ratio = region_ratio
        else:
            valid_ratio = data.get("valid_ratio")
        return {"height": int(data.get("height") or 0), "width": int(data.get("width") or 0),
                "valid_ratio": valid_ratio}
    except Exception:
        return {"height": 0, "width": 0, "valid_ratio": None}


# ── 逐条规则 ───────────────────────────────────────────────────────

def _check_d1(raw_dir: str, probe: Callable[[str], Tuple[bool, str]]) -> List[str]:
    problems = []
    for name in REQUIRED_RASTERS:
        if raw_dir:
            # 按文件名前缀匹配实际栅格：兼容固定名 `landsat_lst.tif`
            # 与带日期后缀 `landsat_lst_20240722.tif` 两种命名
            matches = sorted(glob.glob(os.path.join(raw_dir, f"{_stem(name)}*.tif")))
            if not matches:
                problems.append(f"{_raster_label(name)}：文件不存在")
                continue
            ok, reason = probe(matches[0])
        else:
            ok, reason = probe(name)
        if not ok:
            problems.append(f"{_raster_label(name)}：{reason}")
    return problems


def _stem(name: str) -> str:
    return name[:-4] if name.endswith(".tif") else name


_RASTER_LABELS = {
    "landsat_lst.tif": "陆地卫星地表温度",
    "landsat_qa_pixel.tif": "陆地卫星质量标记",
    "sentinel2_bands.tif": "哨兵二号多光谱",
    "sentinel2_scl.tif": "哨兵二号场景分类",
    "dem.tif": "数字高程",
}


def _raster_label(name: str) -> str:
    return _RASTER_LABELS.get(name, name)


def _check_d2(manifest: Optional[dict]) -> List[str]:
    if not manifest:
        return []          # 没有 manifest 时不做此项判定（合成测试与旧项目目录）
    stages = manifest.get("stages") or {}
    problems = []
    for stage in REQUIRED_STAGES:
        status = (stages.get(stage) or {}).get("status")
        if status and status != "completed":
            problems.append(f"{_stage_label(stage)}阶段状态为{_status_label(status)}")
    return problems


_STAGE_LABELS = {"data_acquisition": "数据获取", "data_pipeline": "数据预处理",
                 "ttri_compute": "地形热响应指数计算"}
_STATUS_LABELS = {"failed": "失败", "skipped_upstream": "因上游失败被跳过",
                  "running": "未完成", "pending": "未开始"}


def _stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name, name)


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _check_d3(pipeline: dict) -> List[str]:
    rows = _as_int(pipeline.get("train_rows"))
    if rows < MIN_TRAIN_ROWS:
        return [f"有效训练样本只有 {rows:,} 个，少于 {MIN_TRAIN_ROWS:,} 个"]
    return []


def _check_d4(pipeline: dict) -> List[str]:
    problems = []
    if _as_int(pipeline.get("constraint_rows")) <= 0:
        problems.append("粗尺度约束层没有有效行")
    if _as_int(pipeline.get("predict_valid_pixels")) <= 0:
        problems.append("十米预测格网没有有效像元")
    return problems


def _check_d5(pipeline: dict) -> List[str]:
    stats = pipeline.get("split_stats") or {}
    counts = {name: _as_int((stats.get(name) or {}).get("count"))
              for name in ("train", "validate", "test")}
    labels = {"train": "训练集", "validate": "验证集", "test": "测试集"}
    problems = [f"{labels[name]}行数为 0" for name, value in counts.items() if value <= 0]
    if problems:
        return problems
    total = sum(counts.values())
    for name, value in counts.items():
        if total > 0 and value / total < MIN_SPLIT_SHARE:
            problems.append(f"{labels[name]}只占 {value / total:.1%}，划分严重失衡")
    return problems


def _check_d6(processed_dir: str, probe: Callable[[str], Dict[str, Any]]) -> List[str]:
    problems = []
    for name in SPLIT_CSVS:
        info = probe(os.path.join(processed_dir, name) if processed_dir else name)
        label = {"train.csv": "训练集", "validate.csv": "验证集", "test.csv": "测试集"}[name]
        if not info.get("exists"):
            problems.append(f"{label}数据文件缺失")
            continue
        if not info.get("has_ttri"):
            problems.append(f"{label}缺少地形热响应指数列")
            continue
        std = info.get("ttri_std")
        if std is None:
            problems.append(f"{label}的地形热响应指数全部为空")
        elif float(std) <= 0:
            problems.append(f"{label}的地形热响应指数没有变化，地形拟合可能失败")
    return problems


def _check_d7(pipeline: dict, processed_dir: str,
              probe: Callable[[str], Dict[str, Any]]) -> List[str]:
    """有效像元占比必须用**完整 30 米约束层**（`30m_constraint_grid.csv`，覆盖全部云掩膜后
    有效像元）计算，不能用 `30m_features_step2.csv` 的训练抽样行数（`step=2` 等间隔抽样，
    行数约为完整约束层的四分之一）——分子分母的抽样步长必须一致，否则会把真实占比系统性
    低估约 4 倍，导致云量正常的影像被误判为不合格（实现期修订 v1.2：修复「换了时间段仍
    反复报有效像元占比偏低」的 bug，根因就是此前误读了 `30m_features_step2_meta.json` 与
    `train_rows`）。
    """
    meta = probe(os.path.join(processed_dir, "30m_constraint_grid_meta.json")
                 if processed_dir else "30m_constraint_grid_meta.json")
    ratio: Optional[float] = None
    raw_ratio = meta.get("valid_ratio")
    if raw_ratio is not None:
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            ratio = None
    if ratio is None:
        cells = _as_int(meta.get("height")) * _as_int(meta.get("width"))
        if cells <= 0:
            return []          # 拿不到格网尺寸时不做此项判定，避免误杀
        rows = _as_int(pipeline.get("constraint_rows"))
        ratio = rows / cells
    if ratio < MIN_VALID_RATIO:
        return [f"云掩膜后有效像元只占 {ratio:.1%}，低于 {MIN_VALID_RATIO:.0%}"]
    return []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── 汇总 ───────────────────────────────────────────────────────────

def check(*, raw_dir: str = "", processed_dir: str = "", pipeline_data: Optional[dict] = None,
          manifest: Optional[dict] = None,
          raster_probe: Optional[Callable[[str], Tuple[bool, str]]] = None,
          csv_probe: Optional[Callable[[str], Dict[str, Any]]] = None,
          meta_probe: Optional[Callable[[str], Dict[str, Any]]] = None) -> ReflectionResult:
    """按 D1→D7 顺序跑完确定性规则。

    任一不通过 → `ok=False`，`action=REPLAN`（由总调度按模式决定弹窗还是自动 replan）。
    """
    pipeline = pipeline_data if isinstance(pipeline_data, dict) else {}
    raster_probe = raster_probe or default_raster_probe
    csv_probe = csv_probe or default_csv_probe
    meta_probe = meta_probe or default_meta_probe

    checks = [
        ("D1", _check_d1(raw_dir, raster_probe)),
        ("D2", _check_d2(manifest)),
        ("D3", _check_d3(pipeline)),
        ("D4", _check_d4(pipeline)),
        ("D5", _check_d5(pipeline)),
        ("D6", _check_d6(processed_dir, csv_probe)),
        ("D7", _check_d7(pipeline, processed_dir, meta_probe)),
    ]

    rule_hits: List[str] = []
    violations: List[str] = []
    suggestions: List[str] = []
    for rule_id, problems in checks:
        if not problems:
            continue
        rule_hits.append(rule_id)
        violations.extend(problems)
        for s in SUGGESTIONS.get(rule_id, []):
            if s not in suggestions:
                suggestions.append(s)

    if not rule_hits:
        return ReflectionResult.passed(note="数据检查全部通过")

    return ReflectionResult(
        ok=False, action=Action.REPLAN,
        note="数据检查未通过，禁止进入训练阶段",
        violations=violations, suggestions=suggestions, rule_hits=rule_hits,
    )
