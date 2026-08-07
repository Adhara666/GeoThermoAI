# -*- coding: utf-8 -*-
"""
规划 Agent 合成测试（技术方案 11.2）

用 FakeAssistant 返回预设 JSON，零网络依赖。
覆盖：
① 纯聊天不产生 steps
② 领域问答不产生 steps
③ 缺时间 → 反问
④ 地名匹配不到研究区 → 反问并列出候选
⑤ 多轮场景（九江镇 → 生成啊 → 25年 → 7月）四轮后槽位齐全并出合法 plan
⑥ replan 次数达上限后不再自动 replan
⑦ LLM 不可用时退回关键词兜底，且绝不出现「默认武汉」
⑧ 角色提示词四段结构齐全
⑨ SessionState 读写与级联删除
⑩ 异常年份（如「125年」）反问年份本身，不盲目复述反问月份（v1.2）
⑪ extract_json 对尾随逗号与推理前言夹带花括号的兜底（v1.2）
"""

import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import plan_schema
from core.agent.roles.planner_agent import PlannerAgent, PlannerContext, PlannerOutcome
from core.agent.roles.prompts import planner as planner_prompts
from core.agent.roles.base_role import REQUIRED_PROMPT_SECTIONS, extract_json
from core.memory.session_state import SessionState

TODAY = datetime.date(2026, 8, 7)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None

    def get_tool_descriptions_for_llm(self):
        return "可调用技能：\n- data_acquisition: 下载数据"


REGISTRY = _FakeRegistry(list(plan_schema.WORKFLOW_STEPS) + ["ai_assistant"])


class _FakeAssistant:
    """按调用顺序返回预设响应；不够时返回最后一条。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _call_api(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.responses:
            return "API调用失败: 没有更多预设响应"
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _intent(intent, region=None, time_expr=None, confidence=0.9, missing=None, question=None):
    return json.dumps({
        "intent": intent, "intent_confidence": confidence, "reason": "测试",
        "slots": {"region_name": region, "time_expression": time_expr,
                  "product": "lst_10m", "model": None},
        "missing": missing or [], "question": question,
    }, ensure_ascii=False)


def _reflect(action="proceed", question=""):
    return json.dumps({"ok": action == "proceed", "action": action,
                       "question": question, "note": "测试"}, ensure_ascii=False)


def _plan_json(region_file, start="2025-07-01", end="2025-07-31"):
    return json.dumps({
        "goal": "生成九江镇 2025 年 7 月的十米地表温度产品",
        "constraints": {"cloud_threshold": 30, "dem_source": "copernicus", "model": "rf"},
        "steps": [{"skill": s, "params": ({"region": region_file, "start_date": start,
                                           "end_date": end} if s == "data_acquisition" else {}),
                   "reason": "测试"} for s in plan_schema.WORKFLOW_STEPS],
        "memory_refs": ["K13"],
    }, ensure_ascii=False)


def _study_dir(tmp, names=("九江镇.geojson",)):
    d = os.path.join(tmp, "study_areas")
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "w", encoding="utf-8") as f:
            f.write('{"type":"FeatureCollection","features":[]}')
    return d


def _ctx(tmp, user_input, session=None, names=("九江镇.geojson",), replan_count=0,
         previous_plan=None, replan_reason=""):
    d = _study_dir(tmp, names)
    return PlannerContext(
        user_input=user_input,
        prior_messages=[],
        session_state=session or {},
        study_areas=list(names),
        study_areas_dir=d,
        project_dir=os.path.join(tmp, "project"),
        settings={"data": {"cloud_threshold": 30, "dem_source": "copernicus"}},
        skill_catalog=REGISTRY.get_tool_descriptions_for_llm(),
        replan_count=replan_count,
        previous_plan=previous_plan,
        replan_reason=replan_reason,
        today=TODAY,
    )


def test_chat_and_qa_no_steps():
    print("[1] 纯聊天与领域问答不产生步骤")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        for intent, label in (("chat", "纯聊天"), ("qa", "领域问答")):
            planner = PlannerAgent(_FakeAssistant([_intent(intent)]), REGISTRY)
            out = planner.run(_ctx(tmp, "你好" if intent == "chat" else "TTRI 是解决什么问题的"))
            _assert(out.action == PlannerOutcome.CHAT, f"{label} → 转流式对话")
            _assert(out.plan is None, f"{label} 不产生任何执行计划")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_time_asks():
    print("[2] 缺时间范围 → 反问")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        planner = PlannerAgent(_FakeAssistant([_intent("task", region="九江镇")]), REGISTRY)
        out = planner.run(_ctx(tmp, "生成九江镇的地表温度"))
        _assert(out.action == PlannerOutcome.ASK, "缺时间 → 反问")
        _assert("月份" in out.question or "2025 年 7 月" in out.question,
                "反问里给出月份示例")
        _assert(out.plan is None, "反问时不生成计划，绝不放行执行")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_region_not_matched_asks():
    print("[3] 地名匹配不到研究区 → 反问并列出候选")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", region="武汉", time_expr="2025年7月")]), REGISTRY)
        out = planner.run(_ctx(tmp, "生成武汉 2025 年 7 月的产品",
                               names=("九江镇.geojson", "南海区.geojson")))
        _assert(out.action == PlannerOutcome.ASK, "匹配不到 → 反问")
        _assert("九江镇" in out.question and "南海区" in out.question,
                "反问里列出已上传的研究区")
        _assert("武汉" in out.question, "反问里点明用户说的地名没找到")

        # 多个候选歧义
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", region="南海", time_expr="2025年7月")]), REGISTRY)
        out = planner.run(_ctx(tmp, "生成南海 2025 年 7 月的产品",
                               names=("南海区.geojson", "佛山市南海区.geojson")))
        _assert(out.action == PlannerOutcome.ASK and "多个" in out.question,
                "多个候选 → 反问让用户确认是哪一个")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_study_area_uploaded():
    print("[4] 未上传研究区 → 对话方式引导（拍板结论 3）")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        d = os.path.join(tmp, "empty_study")
        os.makedirs(d, exist_ok=True)
        ctx = PlannerContext(user_input="跑一下全流程", study_areas=[], study_areas_dir=d,
                             skill_catalog="", today=TODAY)
        planner = PlannerAgent(_FakeAssistant([_intent("task")]), REGISTRY)
        out = planner.run(ctx)
        _assert(out.action == PlannerOutcome.ASK, "没有研究区 → 反问，不硬拦截")
        _assert("上传" in out.question, "提示用户先上传研究区")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_study_area_compat():
    print("[5] 兼容性保护：只有一个研究区且未提地名时沿用现状")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        d = _study_dir(tmp, ("九江镇.geojson",))
        region_file = os.path.join(d, "九江镇.geojson")
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", time_expr="2025年7月"),
                            _plan_json(region_file), _reflect()]), REGISTRY)
        out = planner.run(_ctx(tmp, "跑一下 2025 年 7 月的全流程"))
        _assert(out.action == PlannerOutcome.PLAN, "单研究区用户体验不变，直接出计划")
        _assert(out.plan["region"]["name"] == "九江镇", "自动选中唯一的研究区")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_jiujiang_four_turns():
    print("[6] 九江镇四轮场景（技术方案 4.7 验收用例）")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        d = _study_dir(tmp, ("九江镇.geojson", "南海区.geojson"))
        region_file = os.path.join(d, "九江镇.geojson")
        session_path = os.path.join(tmp, "memory", "sessions", "conv1.json")
        session = SessionState(session_path, conv_id="conv1", project_id="p1")

        def turn(user_input, responses, expect_action):
            planner = PlannerAgent(_FakeAssistant(responses), REGISTRY)
            ctx = _ctx(tmp, user_input, session=session.load(),
                       names=("九江镇.geojson", "南海区.geojson"))
            out = planner.run(ctx)
            slots = dict(out.slots)
            if out.action == PlannerOutcome.CHAT:
                region = slots.get("region_name")
                if isinstance(region, dict) and region.get("source") == "user":
                    slots["region_name"] = {**region, "source": "mentioned"}
            session.set_slots(slots, intent=out.intent,
                              pending_question=out.question if out.action == PlannerOutcome.ASK else "")
            _assert(out.action == expect_action,
                    f"第 {turn.n} 轮「{user_input}」→ {expect_action}")
            turn.n += 1
            return out

        turn.n = 1

        # 第 1 轮：你认识九江镇吗？→ qa，只把九江镇记进会话
        turn("你认识九江镇吗？", [_intent("qa", region="九江镇", confidence=0.9)],
             PlannerOutcome.CHAT)
        stored = session.get_slot("region_name")
        _assert(stored.get("value") == "九江镇" and stored.get("source") == "mentioned",
                "第 1 轮把「九江镇」记入会话槽位（来源 mentioned）")

        # 第 2 轮：生成啊 → task，region 命中上文，缺时间 → 反问
        out = turn("生成啊", [_intent("task", region="九江镇", confidence=0.8)],
                   PlannerOutcome.ASK)
        _assert("时间" in out.question or "月份" in out.question, "第 2 轮反问时间范围")
        _assert(session.load()["pending_question"], "反问被记入 pending_question")

        # 第 3 轮：25 年 → 只到年，继续反问月份
        out = turn("25 年", [_intent("task", time_expr="25年", confidence=0.8)],
                   PlannerOutcome.ASK)
        _assert("2025" in out.question and "月" in out.question,
                "第 3 轮识别出 2025 年但要求确认月份")

        # 第 4 轮：7 月 → 槽位齐全，出合法 plan
        out = turn("7 月", [_intent("task", time_expr="7月", confidence=0.85),
                            _plan_json(region_file), _reflect()],
                   PlannerOutcome.PLAN)
        plan = out.plan
        _assert(plan["region"]["study_area_file"] == os.path.abspath(region_file),
                "第 4 轮研究区解析为九江镇的绝对路径")
        _assert(plan["time_range"] == {"start": "2025-07-01", "end": "2025-07-31"},
                "第 4 轮时间范围补全为 2025 年 7 月")
        _assert(plan_schema.is_full_workflow(plan), "出的是合法的 7 步全流程计划")
        _assert("武汉" not in json.dumps(plan, ensure_ascii=False),
                "任何一轮都不出现「默认武汉」")
        _assert("南海" not in json.dumps(plan, ensure_ascii=False),
                "不会误取另一个研究区（不再是「取最新上传」）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_implausible_year_asks_about_year_not_month():
    print("[6.1] 「125年」等异常年份：反问年份本身，不盲目复述反问月份（v1.2 修复）")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", region="九江镇", time_expr="125年")]), REGISTRY)
        out = planner.run(_ctx(tmp, "要125年的影像"))
        _assert(out.action == PlannerOutcome.ASK, "年份异常 → 反问，不放行")
        _assert("125" in out.question, "反问里点名是「125」这个年份有问题")
        _assert("年份" in out.question, "反问的是年份本身，而不是盲目要月份")
        _assert("请确认具体月份，例如 125 年 7 月" not in out.question,
                "不能原样把 125 塞进示例句子里，那是在不加甄别地复述异常输入")

        # 年月一次说全但年份异常（更危险：差一点就被判定为可执行直接放行下载）
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", region="九江镇", time_expr="125年7月")]), REGISTRY)
        out = planner.run(_ctx(tmp, "要125年7月的影像"))
        _assert(out.action == PlannerOutcome.ASK, "年月都有但年份异常 → 仍必须被拦下反问")
        _assert(out.plan is None, "绝不会带着 0125-07-01 这种日期生成执行计划")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_replan_budget():
    print("[7] replan 次数达上限后不再自动 replan")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        d = _study_dir(tmp)
        region_file = os.path.join(d, "九江镇.geojson")
        planner = PlannerAgent(
            _FakeAssistant([_intent("task", region="九江镇", time_expr="2025年7月"),
                            _plan_json(region_file), _reflect()]),
            REGISTRY, replan_max=3)
        ctx = _ctx(tmp, "换个时间再跑", replan_count=4, replan_reason="数据检查未通过")
        out = planner.run(ctx)
        _assert(out.action == PlannerOutcome.ASK, "超出 replan 上限 → 转人工询问")
        _assert("重新规划" in out.question, "说明已自动重试多次")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_llm_unavailable_fallback():
    print("[8] LLM 不可用时的关键词兜底（绝不默认武汉）")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        planner = PlannerAgent(_FakeAssistant(["API调用失败: 网络不可达"]), REGISTRY)
        out = planner.run(_ctx(tmp, "你好呀"))
        _assert(out.action == PlannerOutcome.CHAT, "无关键词 → 判为聊天，不误拉进流程")

        planner = PlannerAgent(_FakeAssistant(["API调用失败: 网络不可达"]), REGISTRY)
        out = planner.run(_ctx(tmp, "生成一下"))
        _assert(out.action == PlannerOutcome.ASK, "有关键词但信息不全 → 反问，不硬跑")
        _assert("武汉" not in out.question, "反问里不出现硬编码的武汉")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prompt_sections():
    print("[9] 角色提示词四段结构齐全（附录 A 检查清单）")
    identity = planner_prompts.PLANNER_IDENTITY
    for section in REQUIRED_PROMPT_SECTIONS:
        _assert(section in identity, f"规划角色提示词包含「{section}」段")
    for name, prompt in (("意图分类", planner_prompts.intent_prompt("", "")),
                         ("出计划", planner_prompts.plan_prompt("九江镇", "2025 年 7 月",
                                                              "lst_10m", "{}", "", "")),
                         ("轻反思", planner_prompts.reflect_prompt("生成啊", "task", 0.8,
                                                                "{}", "", ""))):
        _assert(identity.splitlines()[0] in prompt, f"{name}提示词内嵌了角色身份")
        _assert("JSON" in prompt or "json" in prompt, f"{name}提示词声明了 JSON 输出格式")


def test_session_state():
    print("[10] SessionState 读写与优先级")
    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        path = os.path.join(tmp, "memory", "sessions", "c1.json")
        s = SessionState(path, conv_id="c1", project_id="p1")
        _assert(s.load()["slots"] == {}, "首次读取得到空状态（文件不存在不报错）")

        s.set_slots({"region_name": {"value": "九江镇", "source": "user"}}, intent="task")
        _assert(os.path.isfile(path), "写入后文件落盘")
        _assert(s.get_slot("region_name")["value"] == "九江镇", "槽位可读回")

        s.set_slots({"region_name": {"value": "南海区", "source": "inferred"}})
        _assert(s.get_slot("region_name")["value"] == "九江镇",
                "user 来源的槽位不会被 inferred 覆盖")
        s.set_slots({"region_name": {"value": "南海区", "source": "user"}})
        _assert(s.get_slot("region_name")["value"] == "南海区",
                "user 来源可以被新的 user 输入覆盖")

        s.update(pending_question="要哪个月份？")
        _assert(s.load()["pending_question"] == "要哪个月份？", "待补问题可写入")
        s.clear_pending_question()
        _assert(s.load()["pending_question"] == "", "待补问题可清空")

        _assert(s.note_replan("数据不合格") == 1 and s.note_replan("再来") == 2,
                "replan 计数自增")
        s.reset_replan()
        _assert(s.load()["replan_count"] == 0, "replan 计数可重置")

        s.delete()
        _assert(not os.path.isfile(path), "会话状态可删除（供级联删除调用）")
        s.delete()
        _assert(True, "重复删除不报错")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_cascade_delete():
    print("[11] 会话状态的级联删除")
    from core.memory import MemoryManager

    tmp = tempfile.mkdtemp(prefix="planner_agent_")
    try:
        mm = MemoryManager(memory_root=os.path.join(tmp, "memory"))
        mm.session_state("cA", "pX").set_slots({"region_name": {"value": "A", "source": "user"}})
        mm.session_state("cB", "pX").set_slots({"region_name": {"value": "B", "source": "user"}})
        mm.session_state("cC", "pY").set_slots({"region_name": {"value": "C", "source": "user"}})

        mm.delete_conversation("pX", "cA")
        _assert(mm.session_state("cA", "pX").load()["slots"] == {},
                "删除对话时级联删除该对话的会话状态")
        _assert(mm.session_state("cB", "pX").load()["slots"] != {}, "同项目其它对话不受影响")

        mm.delete_project("pX")
        _assert(mm.session_state("cB", "pX").load()["slots"] == {},
                "删除项目时级联删除该项目下所有对话的会话状态")
        _assert(mm.session_state("cC", "pY").load()["slots"] != {}, "其它项目不受影响")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extract_json_single_source():
    print("[12] JSON 三级解析的单一来源")
    _assert(extract_json('{"a":1}') == {"a": 1}, "直接解析")
    _assert(extract_json('```json\n{"a":2}\n```') == {"a": 2}, "代码块提取")
    _assert(extract_json('解释文字 {"a":3} 结尾') == {"a": 3}, "首尾大括号兜底")
    _assert(extract_json("不是 JSON") is None, "无法解析返回 None")
    _assert(extract_json("[1,2]") is None, "数组不算合法计划对象")
    _assert(extract_json("") is None, "空串返回 None")

    # v1.2 新增两级兜底：应对模型输出的「近似 JSON」（实现期修订，见修订记录 v1.2 第 ⑮ 条）
    _assert(extract_json('{"a": 1,}') == {"a": 1}, "尾随逗号被去除后可解析")
    _assert(extract_json('```json\n{"a": [1, 2,], "b": 3,}\n```') == {"a": [1, 2], "b": 3},
            "代码块内的尾随逗号也被去除")
    _assert(extract_json('先举个例子：{"example": {"nested": 1}}，'
                         '正式回答是 {"a": 4}') == {"a": 4},
            "推理前言里夹带的花括号不会带偏，命中离结尾最近的合法对象")
    _assert(extract_json('参考格式 {"steps": []} 大概是这样，我的答案：\n'
                         '{"steps": [{"skill": "data_acquisition"}],}')
            == {"steps": [{"skill": "data_acquisition"}]},
            "前面提到的示例 JSON 不会抢先被截取，且尾随逗号也能容错")

    from core.agent.geo_thermo_agent import GeoThermoAgent
    from core.ai_assistant import GeoThermoAI_Assistant
    from core.skills.skill_registry import SkillRegistry

    agent = GeoThermoAgent(GeoThermoAI_Assistant("", "", "", "", "openai"), SkillRegistry())
    _assert(agent._parse_plan('```json\n{"steps":[]}\n```') == {"steps": []},
            "GeoThermoAgent._parse_plan 委托到同一实现")


if __name__ == "__main__":
    test_chat_and_qa_no_steps()
    test_missing_time_asks()
    test_region_not_matched_asks()
    test_no_study_area_uploaded()
    test_single_study_area_compat()
    test_jiujiang_four_turns()
    test_implausible_year_asks_about_year_not_month()
    test_replan_budget()
    test_llm_unavailable_fallback()
    test_prompt_sections()
    test_session_state()
    test_session_cascade_delete()
    test_extract_json_single_source()
    print("\n✅ 规划 Agent 合成测试全部通过")
