"""
数据下载与预处理 Agent（技术方案第 5 章）

覆盖 `data_acquisition` + `data_pipeline` + `ttri_compute` 三个 Skill。

两条硬约束：
1. 影像质量评分是**确定性**的（可复现、可解释、可测试），不用 LLM 打分；
2. 数据轻反思由 D1–D7 规则决定放行，LLM 只把失败原因翻译成人话。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from ..reflection import data_rules
from ..reflection.result import Action, ReflectionResult
from .base_role import RoleAgent
from .prompts import data as data_prompts

# 配对质量评分权重（技术方案 5.2）
PAIR_WEIGHTS = {"cloud": 0.45, "coverage": 0.30, "time_diff": 0.15, "scene_count": 0.10}

# 配对规则上限就是 2 天（领域知识 K13）
MAX_TIME_DIFF_DAYS = 2.0


def _num(value: Any, default: float) -> float:
    """把数据源里可能是 '?'/None 的字段安全转成浮点数。"""
    try:
        if value is None or value == "" or value == "?":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _reasons(cloud: float, coverage: float, dt: float, scenes: float) -> List[str]:
    """生成中文短语，最终拼成 recommend_reason（气泡红线：中文、无表情符号）。"""
    items: List[str] = []
    if cloud <= 10:
        items.append(f"云量很低（最高 {cloud:.0f}%）")
    elif cloud <= 30:
        items.append(f"云量可接受（最高 {cloud:.0f}%）")
    else:
        items.append(f"云量偏高（最高 {cloud:.0f}%）")

    if coverage >= 95:
        items.append(f"研究区覆盖完整（{coverage:.0f}%）")
    elif coverage >= 70:
        items.append(f"研究区覆盖较好（{coverage:.0f}%）")
    else:
        items.append(f"研究区覆盖不足（{coverage:.0f}%）")

    if dt <= 0:
        items.append("两颗卫星同一天成像")
    elif dt <= 1:
        items.append(f"两星成像只差 {dt:.0f} 天")
    else:
        items.append(f"两星成像相差 {dt:.0f} 天")

    if scenes <= 2:
        items.append("无需多景拼接")
    else:
        items.append(f"需要拼接 {scenes:.0f} 景")
    return items


def score_pair(pair: dict) -> Tuple[float, List[str]]:
    """返回 (0~1 的质量得分, 人话理由列表)。确定性函数，不调用 LLM。"""
    cloud = max(_num(pair.get("landsat_cloud_cover"), 100),
                _num(pair.get("sentinel2_cloud_cover"), 100))
    cloud_score = max(0.0, 1.0 - cloud / 100.0)

    coverage = min(_num(pair.get("landsat_coverage"), 0),
                   _num(pair.get("sentinel2_coverage"), 0))
    coverage_score = max(0.0, min(1.0, coverage / 100.0))

    dt = _num(pair.get("time_diff_days"), MAX_TIME_DIFF_DAYS)
    dt_score = max(0.0, 1.0 - dt / MAX_TIME_DIFF_DAYS)

    scenes = _num(pair.get("landsat_count"), 1) + _num(pair.get("sentinel2_count"), 1)
    scene_score = 1.0 if scenes <= 2 else max(0.0, 1.0 - (scenes - 2) * 0.15)

    score = (PAIR_WEIGHTS["cloud"] * cloud_score
             + PAIR_WEIGHTS["coverage"] * coverage_score
             + PAIR_WEIGHTS["time_diff"] * dt_score
             + PAIR_WEIGHTS["scene_count"] * scene_score)
    return round(score, 4), _reasons(cloud, coverage, dt, scenes)


def pair_key(pair: dict) -> str:
    """配对唯一标识：Landsat 日期_Sentinel 日期（YYYYMMDD_YYYYMMDD，升级点 1/12）。"""
    if not isinstance(pair, dict):
        return ""
    l = str(pair.get("landsat_date") or "").replace("-", "")[:8]
    s = str(pair.get("sentinel2_date") or "").replace("-", "")[:8]
    return f"{l}_{s}" if (l and s) else ""


def rank_pairs(pairs: List[dict], used_pairs=None) -> List[dict]:
    """按质量得分降序排列，并在最优的一组上打推荐标记。

    返回**新列表 + 新字典**，不修改入参（不可变约定）。
    只加 `quality_score` / `quality_reasons` / `recommended` / `recommend_reason`
    四个字段，其余字段原样保留，前端旧组件继续可用。

    升级点 1/12：`used_pairs` 为该项目历史已尝试过的配对 key 集合；
    已尝试的对打 `tried=True` 且**不再参与推荐**（推荐标记只落在未尝试的最高分上）；
    若全部已尝试，则不给任何推荐标记（Agent 不应再提示换对）。
    """
    used = {str(k) for k in (used_pairs or [])} if used_pairs else set()

    scored: List[Tuple[float, List[str], dict]] = []
    for pair in pairs or []:
        score, reasons = score_pair(pair)
        scored.append((score, reasons, pair))
    scored.sort(key=lambda item: item[0], reverse=True)

    out: List[dict] = []
    recommended_assigned = False
    for i, (score, reasons, pair) in enumerate(scored):
        key = pair_key(pair)
        tried = bool(used) and key in used
        item = {**pair, "quality_score": score, "quality_reasons": reasons,
                "pair_key": key}
        if tried:
            item["tried"] = True
            item["recommended"] = False
            item["recommend_reason"] = "这组影像组合已尝试过"
        elif not recommended_assigned:
            item["recommended"] = True
            item["recommend_reason"] = "、".join(reasons[:3])
            recommended_assigned = True
        else:
            item["recommended"] = False
        out.append(item)

    # 全部已尝试（升级点 12）：不再推荐任何一组
    if out and not recommended_assigned:
        for item in out:
            item["recommended"] = False
            item["recommend_reason"] = "当前可选的影像组合都已尝试过"
        out[0]["all_tried"] = True
    return out


def best_pair(pairs: List[dict], used_pairs=None) -> Optional[dict]:
    ranked = rank_pairs(pairs, used_pairs=used_pairs)
    return ranked[0] if ranked else None


class DataAgent(RoleAgent):
    """数据质量评估 + 推荐配对 + 数据轻反思。"""

    role = "data"
    role_name = "数据"

    # ── 影像配对 ───────────────────────────────────────────────────

    def rank(self, pairs: List[dict], used_pairs=None) -> List[dict]:
        ranked = rank_pairs(pairs, used_pairs=used_pairs)
        if ranked:
            self.log(f"配对质量排序完成，最高得分 {ranked[0]['quality_score']}")
        return ranked

    def choose(self, pairs: List[dict], used_pairs=None) -> Optional[dict]:
        """完全执行模式下自动选质量得分最高且未尝试过的一组。"""
        return best_pair(pairs, used_pairs=used_pairs)

    @staticmethod
    def auto_select_note(pair: dict, index: int) -> str:
        """自动选择时的气泡说明（中文、含理由）。"""
        reason = pair.get("recommend_reason") or "综合质量最好"
        return f"已自动选择第 {index} 组：{reason}"

    # ── 无合格配对 ─────────────────────────────────────────────────

    def no_pair_summary(self, detail: dict) -> str:
        """说清「搜到了什么、为什么都不合格」（技术方案 5.2）。"""
        from .. import presentation

        return presentation.no_pair_reason(detail).strip()

    # ── 数据轻反思 ─────────────────────────────────────────────────

    def reflect(self, *, raw_dir: str = "", processed_dir: str = "",
                pipeline_data: Optional[dict] = None, manifest: Optional[dict] = None,
                raster_probe=None, csv_probe=None, meta_probe=None) -> ReflectionResult:
        """先跑 D1–D7 规则，再让 LLM 把失败原因翻译成人话。

        规则结论决定放行；LLM 只丰富 note/suggestions，**不会把不通过改成通过**。
        """
        result = data_rules.check(
            raw_dir=raw_dir, processed_dir=processed_dir, pipeline_data=pipeline_data,
            manifest=manifest, raster_probe=raster_probe, csv_probe=csv_probe,
            meta_probe=meta_probe,
        )
        if result.ok:
            return result

        explained = self._explain(result)
        return ReflectionResult(
            ok=False, action=Action.REPLAN,
            note=explained.get("cause") or result.note,
            violations=list(result.violations),
            suggestions=explained.get("suggestions") or list(result.suggestions),
            rule_hits=list(result.rule_hits),
            data={"rule_note": result.note},
        )

    def _explain(self, result: ReflectionResult) -> Dict[str, Any]:
        findings = "\n".join(f"- 不合格：{v}" for v in result.violations) or "- 无"
        candidates = "\n".join(f"- {s}" for s in result.suggestions) or "- 无"
        parsed = self.call_json(
            data_prompts.reflect_prompt(findings, candidates),
            # 实现期修订 v1.2：retry_once=False 只有一次机会，预算给足避免被截断
            "请给出原因与建议。", temperature=0.0, max_tokens=1024, retry_once=False,
        )
        if not isinstance(parsed, dict):
            return {}
        suggestions = [str(s).strip() for s in (parsed.get("suggestions") or [])
                       if str(s).strip()]
        return {"cause": str(parsed.get("cause") or "").strip(),
                "suggestions": suggestions[:3]}

    # ── 记忆联动（读） ─────────────────────────────────────────────

    def region_history(self, region: str) -> str:
        """读同区域历史用过哪组影像、失败过什么（技术方案 5.4）。"""
        return self.memory_block(f"{region} 影像配对 云量 数据源 失败原因")

    # ── 写入实验记录草稿（不单独落库） ─────────────────────────────

    @staticmethod
    def pair_candidates_digest(ranked: List[dict], limit: int = 5) -> List[dict]:
        """候选配对与得分的精简记录，供总调度并入实验记录（技术方案 5.4）。"""
        digest = []
        for pair in (ranked or [])[:limit]:
            digest.append({
                "landsat_date": str(pair.get("landsat_date", "")),
                "sentinel2_date": str(pair.get("sentinel2_date", "")),
                "time_diff_days": pair.get("time_diff_days"),
                "landsat_cloud_cover": pair.get("landsat_cloud_cover"),
                "sentinel2_cloud_cover": pair.get("sentinel2_cloud_cover"),
                "score": pair.get("quality_score"),
                "recommended": bool(pair.get("recommended")),
            })
        return digest
