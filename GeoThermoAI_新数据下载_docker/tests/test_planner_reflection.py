# -*- coding: utf-8 -*-
"""
规划轻反思规则 P1–P7 合成测试（技术方案 11.2）

运行：python tests/test_planner_reflection.py
覆盖：
- P1 研究区必须是当前用户目录下真实存在的文件
- P2 时间范围必须精确到月、start<=end、不晚于今天
- P3 chat/qa 意图强制丢弃 steps
- P4 非法 skill 剔除；剔完为空则反问
- P5 全流程 7 步顺序修正与缺失补齐
- P6 replan 次数上限
- P7 replan 无实质差异判为无效
- 规则结论覆盖 LLM 结论
- 中文时间表达解析（含「25年」「7月」两轮补全）
"""

import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import plan_schema
from core.agent.reflection import planner_rules
from core.agent.reflection.result import Action
from core.agent.roles import slots as slot_utils


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


REGISTRY = _FakeRegistry(list(plan_schema.WORKFLOW_STEPS) + ["ai_assistant"])


def _tmp_study_area(tmp, name="九江镇.geojson"):
    d = os.path.join(tmp, "study_areas")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"FeatureCollection","features":[]}')
    return d, p


def _plan(study_area_file="", start="2025-07-01", end="2025-07-31", intent="task",
          steps=None):
    return plan_schema.parse({
        "intent": intent,
        "goal": "测试",
        "region": {"name": "九江镇", "study_area_file": study_area_file},
        "time_range": {"start": start, "end": end},
        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus"},
        "steps": steps if steps is not None else [
            {"skill": s, "params": {}} for s in plan_schema.WORKFLOW_STEPS],
    })


def test_p1_region():
    print("[1] P1 研究区必须真实存在且属于当前用户")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)

        plan, res = planner_rules.check(_plan(study_area_file=path), registry=REGISTRY,
                                        study_areas_dir=d, study_areas=["九江镇.geojson"])
        _assert(res.action == Action.PROCEED, "研究区存在且在用户目录内 → 放行")

        plan, res = planner_rules.check(_plan(study_area_file=""), registry=REGISTRY,
                                        study_areas_dir=d, study_areas=["九江镇.geojson"])
        _assert(res.action == Action.ASK and "P1" in res.rule_hits, "研究区为空 → P1 反问")
        _assert("九江镇" in res.question, "反问里列出已上传的研究区")

        plan, res = planner_rules.check(_plan(study_area_file=os.path.join(d, "不存在.geojson")),
                                        registry=REGISTRY, study_areas_dir=d,
                                        study_areas=["九江镇.geojson"])
        _assert(res.action == Action.ASK and "P1" in res.rule_hits, "文件不存在 → P1 反问")

        # 越权：指向别的目录
        outside = os.path.join(tmp, "other")
        os.makedirs(outside, exist_ok=True)
        other = os.path.join(outside, "别人的.geojson")
        with open(other, "w", encoding="utf-8") as f:
            f.write("{}")
        plan, res = planner_rules.check(_plan(study_area_file=other), registry=REGISTRY,
                                        study_areas_dir=d, study_areas=["九江镇.geojson"])
        _assert(res.action == Action.ASK and "P1" in res.rule_hits,
                "研究区文件不在用户目录内 → P1 反问（堵越权）")

        plan, res = planner_rules.check(_plan(study_area_file=""), registry=REGISTRY,
                                        study_areas_dir=d, study_areas=[])
        _assert("上传研究区" in res.question, "没有任何研究区时提示先上传")

        d2, p2 = _tmp_study_area(tmp, "南海区.geojson")
        plan, res = planner_rules.check(_plan(study_area_file=""), registry=REGISTRY,
                                        study_areas_dir=d,
                                        study_areas=["九江镇.geojson", "南海区.geojson"])
        _assert("九江镇" in res.question and "南海区" in res.question,
                "多个研究区时全部列出让用户确认")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p2_time_range():
    print("[2] P2 时间范围校验")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)
        common = dict(registry=REGISTRY, study_areas_dir=d, study_areas=["九江镇.geojson"])

        _, res = planner_rules.check(_plan(path, start="", end=""), **common)
        _assert(res.action == Action.ASK and "P2" in res.rule_hits, "时间为空 → P2 反问")
        _assert("月份" in res.question or "2025 年 7 月" in res.question, "反问带月份示例")

        _, res = planner_rules.check(_plan(path, start="2025-07-31", end="2025-07-01"), **common)
        _assert("P2" in res.rule_hits and "晚于" in res.note, "start>end → P2 反问")

        future = (datetime.date.today() + datetime.timedelta(days=400)).isoformat()
        _, res = planner_rules.check(_plan(path, start=future, end=future), **common)
        _assert("P2" in res.rule_hits, "开始时间晚于今天 → P2 反问")

        _, res = planner_rules.check(_plan(path, start="不是日期", end="x"), **common)
        _assert("P2" in res.rule_hits, "无法解析的时间 → P2 反问")

        _, res = planner_rules.check(_plan(path), **common)
        _assert(res.action == Action.PROCEED, "合法时间范围放行")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p3_chat_intent():
    print("[3] P3 聊天/问答意图强制丢弃步骤")
    for intent in ("chat", "qa"):
        plan, res = planner_rules.check(
            _plan("", intent=intent), registry=REGISTRY, study_areas_dir="", study_areas=[])
        _assert(res.action == Action.CHAT_ONLY, f"intent={intent} → 转聊天路径")
        _assert(plan["steps"] == [], f"intent={intent} 的 steps 被清空")
        _assert("P3" in res.rule_hits, f"intent={intent} 命中 P3")


def test_p4_unknown_skills():
    print("[4] P4 非法 skill 剔除")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)
        steps = [{"skill": "data_acquisition", "params": {}}, {"skill": "none", "params": {}}]
        plan, res = planner_rules.check(
            _plan(path, steps=steps), registry=REGISTRY, study_areas_dir=d,
            study_areas=["九江镇.geojson"], wants_full_workflow=False)
        _assert("P4" in res.rule_hits, "非法 skill 命中 P4")
        _assert(plan_schema.skill_names(plan) == ["data_acquisition"], "非法 skill 被剔除")

        plan, res = planner_rules.check(
            _plan(path, steps=[{"skill": "none", "params": {}}]), registry=REGISTRY,
            study_areas_dir=d, study_areas=["九江镇.geojson"], wants_full_workflow=False)
        _assert(res.action == Action.ASK and "P4" in res.rule_hits,
                "剔完为空 → 反问而不是硬跑")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p5_workflow_order():
    print("[5] P5 全流程顺序修正与缺失补齐")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)
        common = dict(registry=REGISTRY, study_areas_dir=d, study_areas=["九江镇.geojson"])

        # 只给了两步 → 补齐为 7 步且顺序正确
        partial = [{"skill": "data_acquisition",
                    "params": {"region": path, "start_date": "2025-07-01",
                               "end_date": "2025-07-31"}},
                   {"skill": "rf_model", "params": {}}]
        plan, res = planner_rules.check(_plan(path, steps=partial), **common)
        _assert("P5" in res.rule_hits, "步骤不完整命中 P5")
        _assert(plan_schema.is_full_workflow(plan), "补齐为完整 7 步且顺序正确")
        acq = plan["steps"][0]["params"]
        _assert(acq["region"] == path and acq["start_date"] == "2025-07-01",
                "补齐时保留原有的研究区与时间参数")

        # 乱序 → 重排
        shuffled = [{"skill": s, "params": {}} for s in reversed(plan_schema.WORKFLOW_STEPS)]
        plan, res = planner_rules.check(_plan(path, steps=shuffled), **common)
        _assert(plan_schema.is_full_workflow(plan), "乱序被重排为正确顺序")

        # 已经正确 → 不命中 P5
        plan, res = planner_rules.check(_plan(path), **common)
        _assert("P5" not in res.rule_hits, "已经正确的 7 步不触发 P5 修正")

        # 非全流程任务不强制补齐
        plan, res = planner_rules.check(
            _plan(path, steps=[{"skill": "accuracy_eval", "params": {}}]),
            wants_full_workflow=False, **common)
        _assert(plan_schema.skill_names(plan) == ["accuracy_eval"],
                "非全流程任务保持用户要求的单步")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p6_replan_budget():
    print("[6] P6 replan 次数上限")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)
        common = dict(registry=REGISTRY, study_areas_dir=d, study_areas=["九江镇.geojson"])
        for n in (0, 1, 2, 3):
            _, res = planner_rules.check(_plan(path), replan_count=n, replan_max=3, **common)
            _assert(res.action == Action.PROCEED, f"replan_count={n} 未超上限 → 放行")
        _, res = planner_rules.check(_plan(path), replan_count=4, replan_max=3, **common)
        _assert(res.action == Action.ASK and "P6" in res.rule_hits,
                "replan_count 超上限 → 停止自动 replan 转人工询问")
        _assert("重新规划" in res.question, "反问里说明已重试多次")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_p7_replan_difference():
    print("[7] P7 replan 必须有实质差异")
    tmp = tempfile.mkdtemp(prefix="planner_")
    try:
        d, path = _tmp_study_area(tmp)
        common = dict(registry=REGISTRY, study_areas_dir=d, study_areas=["九江镇.geojson"])
        old = _plan(path)

        same = _plan(path)
        _, res = planner_rules.check(same, previous_plan=old, **common)
        _assert(res.action == Action.ASK and "P7" in res.rule_hits,
                "与上一版完全相同 → 判为无效 replan")

        widened = _plan(path, start="2025-06-01", end="2025-07-31")
        _, res = planner_rules.check(widened, previous_plan=old, **common)
        _assert(res.action == Action.PROCEED, "扩大时间窗算实质调整 → 放行")

        relaxed = plan_schema.parse({**_plan(path),
                                     "constraints": {"cloud_threshold": 50,
                                                     "dem_source": "copernicus"}})
        _, res = planner_rules.check(relaxed, previous_plan=old, **common)
        _assert(res.action == Action.PROCEED, "放宽云量算实质调整 → 放行")

        _, res = planner_rules.check(_plan(path), previous_plan=None, **common)
        _assert(res.action == Action.PROCEED, "首次规划不做 P7 判定")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_time_expression_parsing():
    print("[8] 中文时间表达解析")
    today = datetime.date(2026, 8, 7)
    cases = {
        "2025年7月": ("2025-07-01", "2025-07-31", slot_utils.PRECISION_MONTH),
        "25年7月": ("2025-07-01", "2025-07-31", slot_utils.PRECISION_MONTH),
        "2025-07": ("2025-07-01", "2025-07-31", slot_utils.PRECISION_MONTH),
        "2024年2月": ("2024-02-01", "2024-02-29", slot_utils.PRECISION_MONTH),
    }
    for raw, (start, end, precision) in cases.items():
        got = slot_utils.parse_time_expression(raw, today=today)
        _assert(got["start"] == start and got["end"] == end and got["precision"] == precision,
                f"「{raw}」解析为 {start} ~ {end}")

    for raw in ("25年", "2025年", "2025"):
        got = slot_utils.parse_time_expression(raw, today=today)
        _assert(got["precision"] == slot_utils.PRECISION_YEAR and got["year"] == 2025,
                f"「{raw}」只到年，必须反问月份")
        _assert(not slot_utils.is_executable(got["precision"]), f"「{raw}」不可放行执行")

    # 中文数字月份（用户实际输入「我要这区域24年七月」时反复被追问月份的那个 bug）
    cn_cases = {
        "我要这区域24年七月": (2024, 7),
        "24年七月": (2024, 7),
        "2024年七月": (2024, 7),
        "二〇二四年七月": (2024, 7),
        "二零二五年十二月": (2025, 12),
        "去年七月": (2025, 7),
        "2024年十一月": (2024, 11),
        "2024年十月": (2024, 10),
    }
    for raw, (year, month) in cn_cases.items():
        got = slot_utils.parse_time_expression(raw, today=today)
        _assert(got["year"] == year and got["month"] == month
                and got["precision"] == slot_utils.PRECISION_MONTH,
                f"「{raw}」解析为 {year} 年 {month} 月")
        _assert(slot_utils.is_executable(got["precision"]), f"「{raw}」可直接放行执行")

    got = slot_utils.parse_time_expression("十月", today=today)
    _assert(got["month"] == 10 and not slot_utils.is_executable(got["precision"]),
            "「十月」识别出月份但缺年份，仍需反问")
    _assert(slot_utils.normalize_cn_numerals("二〇二四年十二月") == "2024年12月",
            "中文数字归一化只作用于年月，不影响其它文字")
    _assert(slot_utils.normalize_cn_numerals("第二年的一些数据") == "第二年的一些数据",
            "非年月语境的中文数字不被改写")

    got = slot_utils.parse_time_expression("去年夏天", today=today)
    _assert(got["year"] == 2025 and got["precision"] == slot_utils.PRECISION_SEASON,
            "「去年夏天」解析出年份但判为模糊，需反问")
    _assert(not slot_utils.is_executable(got["precision"]), "季节表达不可放行执行")

    got = slot_utils.parse_time_expression("7月", today=today)
    _assert(got["month"] == 7 and not slot_utils.is_executable(got["precision"]),
            "只有月份时缺年份，不可放行")

    merged = slot_utils.merge_time_parts(2025, 7)
    _assert(merged["start"] == "2025-07-01" and slot_utils.is_executable(merged["precision"]),
            "年、月分两轮给出后可合并为可执行范围（25年 → 7月）")
    _assert(not slot_utils.is_executable(slot_utils.merge_time_parts(2025, None)["precision"]),
            "只有年份的合并结果仍不可执行")

    got = slot_utils.parse_time_expression("2025-07-05 到 2025-07-20", today=today)
    _assert(got["precision"] == slot_utils.PRECISION_DAY and got["end"] == "2025-07-20",
            "明确起止日期被识别")
    _assert(slot_utils.parse_time_expression("", today=today)["precision"]
            == slot_utils.PRECISION_NONE, "空表达返回 none")
    _assert(slot_utils.describe_range("2025-07-01", "2025-07-31") == "2025 年 7 月",
            "时间范围中文描述正确")


def test_year_plausibility():
    """v1.2 新增：「125年」这类明显不合理的年份必须能被识别出来（修订记录 v1.2 第 ⑯ 条）。"""
    print("[8.1] 年份合理性校验")
    today = datetime.date(2026, 8, 7)

    _assert(slot_utils.MIN_DATA_YEAR == 2015, "系统数据最早年份为 2015（Sentinel-2A 发射年）")
    _assert(slot_utils.year_plausible(2025, today=today), "2025 是合理年份")
    _assert(slot_utils.year_plausible(2015, today=today), "下界 2015 本身合理")
    _assert(slot_utils.year_plausible(2026, today=today), "今年合理")
    _assert(not slot_utils.year_plausible(2027, today=today), "明年不合理（还没有未来影像）")
    _assert(not slot_utils.year_plausible(2014, today=today), "早于 2015 不合理")
    _assert(not slot_utils.year_plausible(125, today=today), "「125」这种明显异常年份不合理")
    _assert(not slot_utils.year_plausible(0, today=today), "0 不合理")
    _assert(not slot_utils.year_plausible(None, today=today), "空值不合理")

    # 「125年」解析出年份是字面值 125（解析本身仍是机械的，合理性判断是另一层），
    # 但合理性校验能识别出它有问题，供上层决定怎么反问。
    got = slot_utils.parse_time_expression("125年", today=today)
    _assert(got["year"] == 125, "「125年」按字面解析出 125（解析层不做语义判断）")
    _assert(not slot_utils.year_plausible(got["year"], today=today),
            "解析出的年份被判定为不合理，交给上层反问年份本身")

    # 更危险的分支：年月一次说全但年份是异常值，不能被判为「可执行」而直接放行下载
    reason = slot_utils.time_range_valid("0125-07-01", "0125-07-31", today=today)
    _assert(reason and "2015" in reason, "起始时间早于 2015 年时 P2 必须拒绝，不能放行执行")
    combined = slot_utils.merge_time_parts(125, 7)
    _assert(slot_utils.is_executable(combined["precision"]),
            "merge_time_parts 本身只管年月是否都有，不做合理性判断（合理性判断在 time_range_valid）")
    reason2 = slot_utils.time_range_valid(combined["start"], combined["end"], today=today)
    _assert(reason2, "「125年7月」这种年月都有但年份异常的组合，最终会在 P2 校验被拦下")


def test_study_area_matching():
    print("[9] 研究区名称匹配")
    import pathlib

    paths = [pathlib.Path("九江镇.geojson"), pathlib.Path("南海区.geojson"),
             pathlib.Path("佛山市南海区.geojson")]
    _assert(slot_utils.match_study_area(paths, "九江镇").stem == "九江镇", "精确匹配")
    _assert(slot_utils.match_study_area(paths, "九江").stem == "九江镇", "包含匹配")
    _assert(slot_utils.match_study_area(paths, "南海区").stem == "南海区",
            "精确匹配优先于包含匹配（南海区 不会被 佛山市南海区 抢走）")
    _assert(slot_utils.match_study_area(paths, "南海") is None,
            "多个包含候选时不擅自选择（交给反问）")
    _assert(len(slot_utils.match_candidates(paths, "南海")) == 2, "歧义候选可被列出")
    _assert(slot_utils.match_study_area(paths, "武汉") is None, "匹配不到返回 None")
    _assert(slot_utils.match_study_area(paths, "") is None, "空名称返回 None")


if __name__ == "__main__":
    test_p1_region()
    test_p2_time_range()
    test_p3_chat_intent()
    test_p4_unknown_skills()
    test_p5_workflow_order()
    test_p6_replan_budget()
    test_p7_replan_difference()
    test_time_expression_parsing()
    test_year_plausibility()
    test_study_area_matching()
    print("\n✅ 规划轻反思规则测试全部通过")
