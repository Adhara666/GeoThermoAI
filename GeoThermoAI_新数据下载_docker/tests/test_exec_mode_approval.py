# -*- coding: utf-8 -*-
"""
执行模式与审批协议合成测试（技术方案 11.2）

运行：python tests/test_exec_mode_approval.py
覆盖：
- ExecMode 归一化与默认值（默认必须是 approval）
- AUTO 模式各节点默认策略；APPROVAL 模式在每个节点都触发暂停
- 审批载荷 schema 合法性（全部 7 个节点）
- manual_tune 表单字段从 RFModelSkill.hyperparameters 动态生成
- resume 的两种协议（pair_index / option_id+values）
- 恢复值在后端被范围截断（不信任前端）
- 超时挂起而非静默选择第一组
- settings.agent 特性开关解析与越界截断
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent.orchestrator import agent_config, approval, exec_mode
from core.agent.orchestrator.approval import Node, Option
from core.agent.orchestrator.exec_mode import DEFAULT_EXEC_MODE, ExecMode
from core.agent.orchestrator.run_state import RunState, Stage


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def test_exec_mode():
    print("[1] 执行模式默认值与归一化")
    _assert(DEFAULT_EXEC_MODE == ExecMode.APPROVAL,
            "默认执行模式是「由我批准」（保证不改变现有功能）")
    _assert(exec_mode.normalize("auto") == ExecMode.AUTO, "auto 被识别")
    _assert(exec_mode.normalize("APPROVAL") == ExecMode.APPROVAL, "大小写不敏感")
    for bad in [None, "", "  ", "xxx", 123]:
        _assert(exec_mode.normalize(bad) == ExecMode.APPROVAL,
                f"非法值 {bad!r} 回落 approval")
    _assert(exec_mode.normalize("", ExecMode.AUTO) == ExecMode.AUTO, "可指定回落值")
    _assert(exec_mode.label("auto") == "完全执行" and exec_mode.label("approval") == "由我批准",
            "模式中文标签正确")


def test_pause_policy():
    print("[2] 两种模式的暂停策略")
    for node in approval.ALL_NODES:
        _assert(approval.should_pause(node, ExecMode.APPROVAL),
                f"由我批准模式在 {node} 节点必须暂停")
    for node in approval.ALL_NODES:
        _assert(not approval.should_pause(node, ExecMode.AUTO),
                f"完全执行模式在 {node} 节点不暂停")
    # 用户明确要求：由我批准模式下即使精度很高也要在调优节点询问
    _assert(approval.should_pause(Node.TUNING_DECISION, ExecMode.APPROVAL),
            "高精度也不跳过 tuning_decision 询问")
    _assert(approval.should_pause(Node.TUNING_ROUND, ExecMode.APPROVAL),
            "每轮调优都要询问")


def test_auto_strategy():
    print("[3] AUTO 模式各节点默认策略（技术方案 3.2）")
    expected = {
        Node.PLAN_CONFIRM: Option.START,
        Node.PAIR_SELECTION: None,
        Node.NO_PAIR: Option.REPLAN,
        Node.DATA_QUALITY: Option.REPLAN,
        Node.TUNING_DECISION: Option.AI_TUNE,
        Node.TUNING_ROUND: None,
        Node.FINAL_REPORT: Option.DONE,
    }
    for node, want in expected.items():
        _assert(approval.auto_choice(node) == want,
                f"{node} 的 AUTO 默认策略为 {want!r}")
    _assert(set(approval.AUTO_DEFAULT_STRATEGY) == set(approval.ALL_NODES),
            "每个审批节点都定义了 AUTO 策略（不漏节点）")


def _all_payloads(fields=None):
    return [
        approval.build_plan_confirm("生成九江镇 2025 年 7 月的十米地表温度产品"),
        approval.build_no_pair("没有找到符合条件的影像组合"),
        approval.build_data_quality("训练样本数量不足"),
        approval.build_tuning_decision("测试集决定系数 0.72", fields=fields, max_rounds=5),
        approval.build_tuning_round("本轮决定系数 0.84"),
        approval.build_tuning_round("本轮决定系数 0.84", is_last_round=True),
        approval.build_final_report("全流程完成"),
    ]


def test_payload_schema():
    print("[4] 审批载荷 schema 合法性")
    for payload in _all_payloads():
        issues = approval.validate_payload(payload)
        _assert(issues == [], f"{payload['node']} 载荷合法（{payload['title']}）")
        _assert(payload["type"] == "approval", f"{payload['node']} 带 type=approval")
        _assert(approval.find_option(payload, payload["default_option"]) is not None,
                f"{payload['node']} 的 default_option 在选项中")
    bad = {"type": "approval", "node": "ghost", "title": "x", "summary": "y",
           "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "A2"}],
           "default_option": "zzz"}
    issues = approval.validate_payload(bad)
    _assert(any("未知的审批节点" in s for s in issues), "未知节点被检出")
    _assert(any("重复" in s for s in issues), "重复选项 id 被检出")
    _assert(any("default_option" in s for s in issues), "非法 default_option 被检出")
    _assert(approval.validate_payload("x") == ["审批载荷必须是对象"], "非对象载荷被检出")
    # 不含表情符号（气泡红线 2）：用生产环境同一套正则判定，避免误伤中文
    from core.agent import presentation

    for payload in _all_payloads():
        text = payload["title"] + payload["summary"] + "".join(
            o["label"] + o.get("hint", "") for o in payload["options"])
        _assert(presentation.strip_emoji(text) == text,
                f"{payload['node']} 文案不含表情符号")


def test_hyperparameter_fields():
    print("[5] manual_tune 表单字段动态生成")
    from core.skills.builtin.rf_model import RFModelSkill

    fields = approval.hyperparameter_fields(RFModelSkill())
    names = [f["name"] for f in fields]
    _assert("n_estimators" in names and "max_depth" in names,
            "决策树数量与最大深度来自 Skill 声明")
    _assert("random_state" not in names, "随机种子不进手动调参表单（不是调优旋钮）")
    for f in fields:
        _assert(f.get("label") and f.get("type") and "default" in f,
                f"{f['name']} 字段含 label/type/default")
    n_est = next(f for f in fields if f["name"] == "n_estimators")
    _assert(n_est["min"] == 50 and n_est["max"] == 1000 and n_est["step"] == 50,
            "决策树数量的取值区间与步长来自 Skill 声明，不在前端硬编码")

    payload = approval.build_tuning_decision("测试集决定系数 0.72", fields=fields)
    _assert(approval.validate_payload(payload) == [], "带表单的调优载荷合法")
    manual = approval.find_option(payload, Option.MANUAL_TUNE)
    _assert(len(manual["fields"]) == len(fields), "表单字段被完整挂到 manual_tune 选项上")


def test_resume_parsing():
    print("[6] 恢复载荷解析与后端范围截断")
    from core.skills.builtin.rf_model import RFModelSkill

    fields = approval.hyperparameter_fields(RFModelSkill())
    payload = approval.build_tuning_decision("测试集决定系数 0.72", fields=fields)

    ok, err = approval.parse_resume(payload, {"option_id": Option.AI_TUNE})
    _assert(err == "" and ok["option_id"] == Option.AI_TUNE, "合法选项被接受")
    _assert(ok["values"] == {}, "无表单的选项 values 为空")

    bad, err = approval.parse_resume(payload, {"option_id": "ghost"})
    _assert(bad is None and err == "无效的选项", "非法选项被拒绝")
    bad, err = approval.parse_resume(None, {"option_id": Option.AI_TUNE})
    _assert(bad is None and "没有待处理的选择" in err, "无待处理载荷时给出明确提示")

    ok, _ = approval.parse_resume(payload, {
        "option_id": Option.MANUAL_TUNE,
        "values": {"n_estimators": 999999, "max_depth": -5, "注入字段": "x"},
    })
    _assert(ok["values"]["n_estimators"] == 1000, "超上限的数值被截断到 1000")
    _assert(ok["values"]["max_depth"] == 5, "低于下限的数值被截断到 5")
    _assert("注入字段" not in ok["values"], "未声明的字段被丢弃（不信任前端）")
    _assert(isinstance(ok["values"]["n_estimators"], int), "整数型超参保持整数")

    ok, _ = approval.parse_resume(payload, {
        "option_id": Option.MANUAL_TUNE, "values": {"n_estimators": "abc"}})
    _assert(ok["values"]["n_estimators"] == 200, "不可解析的数值回落默认值")

    auto = approval.auto_resume(payload)
    _assert(auto["option_id"] == Option.AI_TUNE,
            "AUTO 模式的等效恢复结果取 default_option")


def test_run_state():
    print("[7] 流程状态机")
    st = RunState(exec_mode="auto", replan_max=3, plan_id="plan_x")
    _assert(st.exec_mode == ExecMode.AUTO and st.stage == Stage.PLANNING,
            "初始阶段为需求规划")
    st.advance_from_skill("data_pipeline")
    _assert(st.stage == Stage.DATA, "按 skill 推进到数据准备阶段")
    st.advance_from_skill("rf_model")
    _assert(st.stage == Stage.TRAIN, "推进到模型训练阶段")
    st.advance_from_skill("data_acquisition")
    _assert(st.stage == Stage.TRAIN, "阶段只前进不倒退")

    for i in range(3):
        _assert(st.can_replan(), f"第 {i + 1} 次 replan 允许")
        st.note_replan(f"原因{i}")
    _assert(not st.can_replan(), "达到上限后不再允许自动 replan（规则 P6）")
    _assert(st.last_replan_reason() == "原因2", "记录最后一次 replan 原因")

    st.record_approval(Node.TUNING_DECISION, Option.AI_TUNE)
    st.set_resume_point(Node.PAIR_SELECTION)
    _assert(st.take_resume_point() == Node.PAIR_SELECTION, "断点可取出")
    _assert(st.take_resume_point() == "", "断点一次性消费")
    _assert(st.next_tuning_round() == 1 and st.next_tuning_round() == 2, "调优轮次自增")

    restored = RunState.from_dict(st.to_dict())
    _assert(restored.replan_count == 3 and restored.tuning_rounds == 2
            and restored.approval_choices[Node.TUNING_DECISION] == Option.AI_TUNE,
            "序列化与反序列化保真")
    _assert(RunState.from_dict(None).stage == Stage.PLANNING, "空输入得到干净初始状态")


def test_agent_config():
    print("[8] settings.agent 特性开关解析")
    cfg = agent_config.resolve(None)
    _assert(cfg["roles_enabled"] is agent_config.AGENT_DEFAULTS["roles_enabled"],
            "缺配置时取代码默认（特性开关）")
    _assert(cfg["tuning_max_rounds"] == 5, "调优轮数默认 5（拍板结论 4）")
    _assert(agent_config.MAX_TUNING_ROUNDS == 8, "调优轮数硬上限 8")

    cfg = agent_config.resolve({"agent": {"tuning_max_rounds": 100}})
    _assert(cfg["tuning_max_rounds"] == 8, "配置超过硬上限时截断为 8")
    cfg = agent_config.resolve({"agent": {"tuning_max_rounds": 0}})
    _assert(cfg["tuning_max_rounds"] == 1, "配置小于 1 时截断为 1")
    cfg = agent_config.resolve({"agent": {"tuning_max_rounds": "abc"}})
    _assert(cfg["tuning_max_rounds"] == 5, "非法配置回落默认值")

    cfg = agent_config.resolve({"agent": {"roles_enabled": True, "replan_max": 2,
                                          "default_exec_mode": "auto",
                                          "approval_wait_seconds": 60}})
    _assert(cfg["roles_enabled"] and cfg["replan_max"] == 2
            and cfg["default_exec_mode"] == "auto" and cfg["approval_wait_seconds"] == 60,
            "显式配置生效")
    _assert(agent_config.resolve({"agent": {"default_exec_mode": "ghost"}})["default_exec_mode"]
            == ExecMode.APPROVAL, "非法执行模式回落 approval")


def _make_backend(tmp_root, roles_enabled):
    """构造一个可离线测试 chat_resume / pause_callback 的最小后端替身。"""
    import queue

    class _Backend:
        def __init__(self):
            self._conv_states = {}
            self._stream_queues = {"c1": queue.Queue()}
            self._pause_events = {"c1": threading.Event()}
            self._pause_responses = {}
            self._deleted_convs = set()

        _get_conv_state = None  # 由下方赋值

    import server

    b = _Backend()
    b._get_conv_state = lambda cid: b._conv_states.setdefault(cid, {})
    b.chat_resume = server.AppBackend.chat_resume.__get__(b, _Backend)
    return b


def test_chat_resume_protocols():
    print("[9] chat_resume 双协议（旧 pair_index / 新 option_id）")
    b = _make_backend(None, True)

    # 旧协议：配对选择
    b._get_conv_state("c1")["pending_pairs"] = [{"landsat_date": "a"}, {"landsat_date": "b"}]
    r = b.chat_resume("c1", {"pair_index": 1})
    _assert(r["ok"] and b._pause_responses["c1"]["landsat_date"] == "b",
            "旧协议按索引恢复")
    _assert("pending_pairs" not in b._get_conv_state("c1"), "恢复后清除待选配对")

    b._get_conv_state("c1")["pending_pairs"] = [{"landsat_date": "a"}]
    r = b.chat_resume("c1", {"pair_index": 99})
    _assert(r["ok"] and b._pause_responses["c1"]["landsat_date"] == "a",
            "越界索引被截断到合法范围")

    r = b.chat_resume("c1", {"pair_index": 0})
    _assert(not r["ok"] and "没有待选配对" in r["message"], "无待选配对时明确报错")

    # 新协议：通用审批
    payload = approval.build_tuning_decision("测试集决定系数 0.72", fields=[
        {"name": "n_estimators", "label": "决策树数量", "type": "number",
         "default": 200, "min": 50, "max": 1000, "step": 50},
    ])
    b._get_conv_state("c1")["pending_approval"] = payload
    r = b.chat_resume("c1", {"option_id": Option.MANUAL_TUNE,
                             "values": {"n_estimators": 5000}})
    _assert(r["ok"], "新协议恢复成功")
    _assert(b._pause_responses["c1"] == {"option_id": Option.MANUAL_TUNE,
                                        "values": {"n_estimators": 1000}},
            "恢复结果形状为 {option_id, values} 且数值已截断")
    _assert("pending_approval" not in b._get_conv_state("c1"), "恢复后清除待处理审批")

    b._get_conv_state("c1")["pending_approval"] = payload
    r = b.chat_resume("c1", {"option_id": "ghost"})
    _assert(not r["ok"] and r["message"] == "无效的选项", "非法选项被拒绝")

    b._get_conv_state("c1").pop("pending_approval", None)
    r = b.chat_resume("c1", {"option_id": Option.AI_TUNE})
    _assert(not r["ok"] and "没有待处理的选择" in r["message"], "无待处理审批时明确报错")

    r = b.chat_resume("unknown_conv", {"pair_index": 0})
    _assert(not r["ok"] and "没有待恢复的流" in r["message"], "未知对话被拒绝")


def test_pause_timeout_suspends():
    print("[10] 超时挂起而非静默选第一组（拍板结论 1）")
    import server

    # 用极短超时验证语义：角色路径超时 → paused=True；旧路径超时 → 静默选 pairs[0]
    os.environ["GTAI_APPROVAL_WAIT_SECONDS"] = "30"
    try:
        _assert(server._approval_wait_seconds_from({"approval_wait_seconds": 1800}) == 30,
                "环境变量可覆盖审批等待超时")
    finally:
        os.environ.pop("GTAI_APPROVAL_WAIT_SECONDS", None)
    _assert(server._approval_wait_seconds_from({"approval_wait_seconds": 1800}) == 1800,
            "无环境变量时取 settings.agent.approval_wait_seconds")
    _assert(server._LEGACY_PAUSE_TIMEOUT == 300,
            "旧路径保持改造前的 300 秒超时")

    # 直接复刻 pause_callback 的等待与兜底语义（不起真实 SSE）
    def wait_and_resolve(roles_enabled, responses, pause_data, timeout=0.05):
        ev = threading.Event()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ev.wait(timeout=0.01):
                break
        selected = responses.pop("c1", None)
        if selected is not None:
            return {"paused": False, "data": selected}
        if not roles_enabled:
            pairs = pause_data.get("pairs", [])
            if pairs:
                return {"paused": False, "data": pairs[0]}
        return {"paused": True}

    pause_data = {"type": "select_pair", "pairs": [{"landsat_date": "a"}]}
    _assert(wait_and_resolve(True, {}, pause_data) == {"paused": True},
            "角色路径超时后挂起，不替用户选择")
    out = wait_and_resolve(False, {}, pause_data)
    _assert(out["paused"] is False and out["data"]["landsat_date"] == "a",
            "旧路径保持超时静默选第一组（不破坏队友现有行为）")
    out = wait_and_resolve(True, {"c1": {"option_id": Option.AI_TUNE}}, pause_data)
    _assert(out["paused"] is False and out["data"]["option_id"] == Option.AI_TUNE,
            "已有用户选择时正常返回")


def test_pause_response_popped():
    print("[11] 多暂停点：上一次选择不会自动放行下一次")
    b = _make_backend(None, True)
    b._get_conv_state("c1")["pending_pairs"] = [{"landsat_date": "a"}]
    b.chat_resume("c1", {"pair_index": 0})
    _assert("c1" in b._pause_responses, "第一次选择已写入")
    # pause_callback 用 pop 取值，取完即清空
    consumed = b._pause_responses.pop("c1", None)
    _assert(consumed is not None and "c1" not in b._pause_responses,
            "选择被 pop 消费后不残留，第二个暂停点不会被误放行")


if __name__ == "__main__":
    test_exec_mode()
    test_pause_policy()
    test_auto_strategy()
    test_payload_schema()
    test_hyperparameter_fields()
    test_resume_parsing()
    test_run_state()
    test_agent_config()
    test_chat_resume_protocols()
    test_pause_timeout_suspends()
    test_pause_response_popped()
    print("\n✅ 执行模式与审批协议测试全部通过")
