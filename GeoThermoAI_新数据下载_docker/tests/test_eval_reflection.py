# -*- coding: utf-8 -*-
"""
评估轻反思 E-R1 – E-R7 合成测试

运行：python tests/test_eval_reflection.py
覆盖：
- E-R1 ~ E-R7 规则单元测试（作为已测试的工具库保留）
- 结果解读：报告的数字/评级/闭合口径句由系统确定性渲染，
  LLM 只写「关键特征与局限」的 1~3 句定性说明
  - 定性短句通过轻校验 → 原文进入报告
  - 出现数字 / 禁用表述 / 半截句 → 固定兜底句，报告仍完整（永不降级）
  - 大模型不可用 → 兜底句，报告仍给出真实指标
- 有效像元数取自 lst_export 顶层
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent.reflection import eval_rules
from core.agent.reflection.result import Action
from core.agent.roles.eval_agent import EvalAgent

BUNDLE = {
    "region": "九江镇",
    "time_range": {"start": "2025-07-01", "end": "2025-07-31"},
    "test_metrics": {"R2": 0.87, "RMSE": 1.23, "MAE": 0.91, "MB": 0.12},
    "train_metrics": {"R2": 0.90, "RMSE": 1.50},
    "closure": {"n_matched_cells": 373240, "coverage_ratio": 0.98,
                "metrics": {"MB_K": 0.05, "MAE_K": 0.40, "RMSE_K": 0.50, "R2": 0.995},
                "value_range": {"low_end_difference_K": -0.45,
                                "high_end_difference_K": -0.58}},
    "lst_stats": {"total_valid": 4231905, "valid_percent": 92.4},
    "image_size": {"height": 2100, "width": 1800},
    "feature_importance": [{"feature": "NDVI", "importance": 0.28},
                           {"feature": "TTRI", "importance": 0.20}],
    "params": {"n_estimators": 400, "max_depth": 30},
}

GOOD_TEXT = """产品概况
本次生成九江镇 2025 年 7 月的十米地表温度产品，有效像元 4,231,905 个，影像 2100 行 1800 列。

模型精度
测试集决定系数 0.87，均方根误差 1.23 开尔文，平均绝对误差 0.91 开尔文，属于优秀。

闭合情况
闭合平均偏差 0.05 开尔文，平均绝对误差 0.40 开尔文，共比对 373240 个格网。
这是十米结果回聚合到三十米格网的算术均值闭合，不是十米精度，也不代表能量守恒之外的任何含义。

关键特征与局限
贡献最大的是植被指数与地形热响应指数。十米产品的最大值更高、最小值更低，
这是分辨率提升带来的正常表现。局限在于云掩膜后的空洞区域没有结果。
"""


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def _check(text, bundle=None, expected=None):
    return eval_rules.check(text, bundle=bundle or BUNDLE, expected_grade=expected)


def test_good_text_passes():
    print("[1] 合格文本不被误杀")
    res = _check(GOOD_TEXT)
    _assert(res.ok, f"合格文本通过全部检查（违规项：{res.violations}）")
    _assert(res.action == Action.PROCEED, "动作为放行")
    _assert("不是十米精度" in GOOD_TEXT and "不代表能量守恒" in GOOD_TEXT,
            "口径说明（否定语境）被允许写出")


def test_er1_numbers():
    print("[2] E-R1 数字必须有出处")
    res = _check("测试集决定系数 0.95，均方根误差 1.23 开尔文。")
    _assert(not res.ok and "E-R1" in res.rule_hits, "编造的决定系数被打回")
    _assert(any("决定系数 0.95" in v for v in res.violations), "违规项点明是哪个数字")

    res = _check("测试集决定系数 0.87，均方根误差 9.99 开尔文。")
    _assert("E-R1" in res.rule_hits, "编造的均方根误差被打回")

    res = _check("测试集决定系数 0.87，均方根误差 1.23 开尔文。")
    _assert("E-R1" not in res.rule_hits, "真实数值通过核对")

    res = _check("测试集决定系数 0.870，均方根误差 1.230 开尔文。")
    _assert("E-R1" not in res.rule_hits, "多写一位小数仍算一致（按位数容差）")

    res = _check("共比对 373,240 个格网，有效像元 4,231,905 个。")
    _assert("E-R1" not in res.rule_hits, "带千分位的计数能核对")

    res = _check("本次生成 2025 年 7 月的十米产品，分辨率 10 米。")
    _assert("E-R1" not in res.rule_hits, "日期与分辨率等非指标数字不参与核对")

    res = _check("均方根误差 1.23 开尔文。", bundle={"test_metrics": {}})
    _assert("E-R1" in res.rule_hits, "结果文件里没有该指标时也被打回")
    _assert(any("没有该指标" in v for v in res.violations), "违规项说明结果文件里没有")

    # 规则编号「E-R2」含子串「R2」，不能被当成决定系数关键词
    res = _check("本次未通过的检查项包括 E-R1、E-R2、E-R5、E-R6、E-R7。")
    _assert("E-R1" not in res.rule_hits,
            "规则编号里的数字不被误判为指标数值")
    _assert(eval_rules._group_for("检查项 E-R2", len("检查项 E-R")) is None,
            "「E-R2」里的 R2 不算指标关键词")
    normal = "测试集 R2 为 0.87"
    _assert(eval_rules._group_for(normal, normal.index("0.87")) == "r2",
            "正常写法「R2 为 0.87」仍被识别为决定系数")
    _assert("E-R1" not in _check(normal).rule_hits, "正常写法且数值真实 → 通过")


def test_er2_disallowed():
    print("[3] E-R2 禁用表述")
    for word in ("能量守恒", "辐射守恒", "10 米精度", "完全准确", "零误差"):
        res = _check(f"本次闭合结果说明该产品达到{word}。")
        _assert(not res.ok and "E-R2" in res.rule_hits, f"禁用词「{word}」被打回")

    res = _check("闭合平均偏差 0.05 开尔文，但产品不可用。")
    _assert("E-R2" in res.rule_hits, "闭合正常却说产品不可用 → 打回")

    bad_closure = {**BUNDLE, "closure": {**BUNDLE["closure"],
                                         "metrics": {"MB_K": 5.0, "MAE_K": 6.0}}}
    res = _check("闭合平均偏差 5.00 开尔文，产品不可用。", bundle=bad_closure)
    _assert(not any("产品不可用" in v for v in res.violations),
            "闭合指标异常时允许指出产品不可用（不是硬禁）")


def test_er3_null_metric():
    print("[4] E-R3 指标为空不得编造")
    bundle = {**BUNDLE, "test_metrics": {"RMSE": 1.23},
              "r2_null_reason": "测试集方差过小，决定系数无定义"}
    res = _check("测试集决定系数 0.87，效果不错。", bundle=bundle, expected="未知")
    _assert(not res.ok and "E-R3" in res.rule_hits, "决定系数为空却给了数值 → 打回")
    _assert(any("仍给出了具体数值" in v for v in res.violations), "违规项说明原因")

    res = _check("本次未能计算决定系数。", bundle=bundle, expected="未知")
    _assert("E-R3" in res.rule_hits, "没有复述为空的原因 → 打回")

    res = _check("本次未能计算，原因是测试集方差过小，决定系数无定义。",
                 bundle=bundle, expected="未知")
    _assert("E-R3" not in res.rule_hits, "复述了原因且未编造数值 → 通过")


def test_er4_extreme_negativity():
    print("[5] E-R4 极值差不得作为负面结论依据")
    res = _check("十米产品的最大值明显偏高，说明产品有问题。")
    _assert(not res.ok and "E-R4" in res.rule_hits, "因极值差下负面结论 → 打回")
    _assert(any("极值差" in v for v in res.violations), "违规项点明原因")

    res = _check("十米产品的最大值更高、最小值更低，这是分辨率提升的正常表现。")
    _assert("E-R4" not in res.rule_hits, "按 E03 正确解释极值差 → 通过")

    # 「温差」「差异」是合法领域词，不是负面判断；裸「差」会误杀它们
    res = _check("极值端温差达 -11.74 K，是分辨率提升的预期表现。")
    _assert("E-R4" not in res.rule_hits, "「温差」不被误判为负面判断词")
    res = _check("十米与三十米结果的最大值存在差异，这是尺度效应的正常表现。")
    _assert("E-R4" not in res.rule_hits, "「差异」不被误判为负面判断词")
    res = _check("极值端偏差较大，说明产品有问题。")
    _assert("E-R4" in res.rule_hits, "「偏差较大」等复合负面词仍被识别")

    res = _check("云掩膜后的空洞区域没有结果，这是本次产品的局限。")
    _assert("E-R4" not in res.rule_hits, "不涉及极值的局限性说明不被误杀")

    bad_closure = {**BUNDLE, "closure": {**BUNDLE["closure"],
                                         "metrics": {"MB_K": 5.0, "MAE_K": 6.0}}}
    res = _check("十米产品的最大值明显偏高，说明产品有问题。", bundle=bad_closure)
    _assert("E-R4" not in res.rule_hits, "闭合指标异常时不再拦这条（问题确实存在）")


def test_er5_grade_consistency():
    print("[6] E-R5 评级必须与分档一致")
    res = _check("测试集决定系数 0.87，属于合格水平。")
    _assert(not res.ok and "E-R5" in res.rule_hits, "0.87 应为优秀，写成合格 → 打回")
    _assert(any("优秀" in v for v in res.violations), "违规项给出应有的评级")

    res = _check("测试集决定系数 0.87，属于优秀水平。")
    _assert("E-R5" not in res.rule_hits, "评级一致 → 通过")

    res = _check("测试集决定系数 0.87。")
    _assert("E-R5" not in res.rule_hits, "不写评级词也不算违规")

    low = {**BUNDLE, "test_metrics": {"R2": 0.70, "RMSE": 1.23}}
    res = _check("测试集决定系数 0.70，属于优秀水平。", bundle=low)
    _assert("E-R5" in res.rule_hits, "0.70 应为偏低，写成优秀 → 打回")


def test_er6_closure_wording():
    print("[7] E-R6 口径不得混用")
    res = _check("本次闭合精度达到 0.05 开尔文。")
    _assert(not res.ok and "E-R6" in res.rule_hits, "把闭合说成精度 → 打回")

    res = _check("这是均值闭合校核，不是十米独立精度。")
    _assert("E-R6" not in res.rule_hits, "显式否定的口径说明不被误杀")

    res = _check("闭合平均偏差 0.05 开尔文；模型精度见测试集评估。")
    _assert("E-R6" not in res.rule_hits, "两者分句陈述、间隔足够时不误杀")


def test_multiple_violations():
    print("[8] 多条同时违规时全部上报")
    res = _check("闭合精度达到能量守恒，测试集决定系数 0.66，属于合格。")
    for rule in ("E-R1", "E-R2", "E-R5", "E-R6"):
        _assert(rule in res.rule_hits, f"{rule} 被记录")
    _assert(res.action == Action.REWRITE, "动作为打回重写")
    _assert(len(res.violations) >= 4, "违规项逐条列出，作为重写的修改要求")


def test_closure_normality():
    print("[9] 闭合指标正常性判据（E04）")
    _assert(eval_rules.closure_is_normal({"MB_K": 0.05, "MAE_K": 0.40}), "接近 0 判为正常")
    _assert(not eval_rules.closure_is_normal({"MB_K": 2.0, "MAE_K": 0.40}),
            "平均偏差明显偏离 0 判为异常")
    _assert(not eval_rules.closure_is_normal({}), "拿不到指标时按未知处理（不放宽检查）")
    _assert(not eval_rules.closure_is_normal(None), "None 同上")


class _BadLLM:
    """通用假 LLM：返回违规文本。不跑 build_report 的测试用它当占位。"""

    def __init__(self):
        self.calls = 0

    def _call_api(self, messages, **kwargs):
        self.calls += 1
        return "闭合精度达到能量守恒，测试集决定系数 0.66，属于合格，产品不可用。"


class _BadQualitativeLLM:
    """定性说明违规（含数字 + 禁用表述），用于验证固定兜底句。"""

    def __init__(self):
        self.calls = 0

    def _call_api(self, messages, **kwargs):
        self.calls += 1
        return "本次产品决定系数 0.66，产品不可用。"


class _GoodQualitativeLLM:
    """定性说明合规：无数字、无禁用表述、无极值负面判断，以句号收尾。"""

    def __init__(self):
        self.calls = 0

    def _call_api(self, messages, **kwargs):
        self.calls += 1
        return ("贡献最大的是植被指数与地形热响应指数，十米产品的细节更丰富；"
                "云掩膜后的空洞区域没有结果。")


class _Ctx:
    def __init__(self):
        self.results_dir = ""
        self.exp_state = {"rf_data": {"test_metrics": BUNDLE["test_metrics"],
                                     "train_metrics": {"train": BUNDLE["train_metrics"]},
                                     "feature_importance": BUNDLE["feature_importance"],
                                     "params": BUNDLE["params"]}}
        self.plan = {"region": {"name": "九江镇"},
                     "time_range": {"start": "2025-07-01", "end": "2025-07-31"}}
        self.conv_id = "c1"
        self.emitted = []

    def emit(self, text, to_log=False):
        self.emitted.append(text)


def _agent_with(llm):
    agent = EvalAgent(llm)
    agent.bundle = dict(BUNDLE)
    return agent


def test_qualitative_rejected_uses_fallback():
    print("[10] 定性说明违规 → 固定兜底句，报告仍完整（永不降级）")
    llm = _BadQualitativeLLM()
    agent = _agent_with(llm)
    report = agent.build_report(_Ctx())
    _assert(llm.calls == 1, "只调用一次 LLM（不再有重写循环）")
    _assert(agent.note_reason, f"记录了未采用原因：{agent.note_reason}")
    _assert("数字" in agent.note_reason or "禁用表述" in agent.note_reason,
            "原因点明是哪项校验不过")
    _assert("0.66" not in report, "违规原文里的数字不进报告")
    _assert("产品不可用" not in report, "禁用表述不进报告")
    _assert("结果说明" in report and "产品概况" in report, "报告标题与结构完整")
    _assert("决定系数" in report, "真实指标仍由系统渲染进报告")
    _assert("需结合研究区、天气与云掩膜情况理解" in report, "使用了固定兜底句")
    last = [l for l in report.splitlines() if l.strip()][-1]
    _assert(not last.rstrip().endswith("。"), "报告段落结尾不带句号（用户要求）")


def test_qualitative_note_included():
    print("[11] 定性说明通过轻校验 → 原文进入报告，无需兜底")
    llm = _GoodQualitativeLLM()
    agent = _agent_with(llm)
    report = agent.build_report(_Ctx())
    _assert(llm.calls == 1, "只调用一次 LLM")
    _assert(not agent.note_reason, "采用了 LLM 原文（note_reason 为空）")
    _assert("植被指数与地形热响应指数" in report, "LLM 定性说明进入报告")
    _assert("优秀" in report, "系统渲染的评级正确")


def test_llm_unavailable_uses_fallback():
    print("[12] 大模型不可用 → 固定兜底句，报告仍完整")

    class _NoLLM:
        def _call_api(self, messages, **kwargs):
            return "API调用失败: 网络不可达"

    agent = _agent_with(_NoLLM())
    report = agent.build_report(_Ctx())
    _assert(agent.note_reason and "接口调用失败" in agent.note_reason,
            "记录了接口不可用原因")
    _assert("决定系数" in report and "0.87" in report, "报告仍给出真实指标")
    _assert("需结合研究区、天气与云掩膜情况理解" in report, "使用固定兜底句")
    _assert(not report.rstrip().endswith("。"), "报告段落结尾不带句号（用户要求）")


def test_lst_total_valid_from_top_level():
    print("[13] 有效像元数取自 lst_export 结果顶层")
    from core.skills.base_skill import SkillResult as _SR

    agent = EvalAgent(_BadLLM())
    # lst_export 的 total_valid 在 result_data 顶层，不在 stats 里
    agent.on_eval_step("lst_export", _SR(True, "导出完成", data={
        "stats": {"min": 295.1, "max": 318.7, "valid_percent": 16.4},
        "total_valid": 6152331,
        "image_size": {"height": 6612, "width": 5671},
    }), _Ctx())
    _assert(agent.bundle["lst_stats"]["total_valid"] == 6152331,
            "顶层 total_valid 被合并进统计字典")
    agent.bundle.update(BUNDLE, lst_stats=agent.bundle["lst_stats"])
    facts = agent.facts_text()
    _assert("有效像元数：6,152,331 个" in facts, "报告里给出真实有效像元数，不再是「未知」")


def test_assemble_report_complete():
    print("[15] 组装报告始终完整：数字/评级/口径由系统渲染，句末收尾")
    agent = _agent_with(_BadQualitativeLLM())
    report = agent.assemble_report("优秀", "")
    last = [l for l in report.splitlines() if l.strip()][-1]
    _assert(not last.rstrip().endswith("。"),
            f"最后一行不带句号（实际结尾：{last[-16:]!r}）")
    for section in ("产品概况", "模型精度", "闭合情况", "关键特征与局限"):
        _assert(section in report, f"小节「{section}」齐全")
    _assert("优秀" in report, "传入的评级被渲染进报告")
    # 系统确定性报告永不截断，不需要 E-R7 的收尾句号判定
    res = eval_rules.check(report, bundle=BUNDLE, require_structure=False)
    _assert(res.ok, f"组装报告通过 E-R1~E-R6（违规项：{res.violations}）")


def test_er7_structure_and_truncation():
    print("[16] E-R7 报告结构完整、不得停在半句话")
    # 生成预算耗尽的典型形态：写到一半就没了，且没有任何标记
    cut = ("产品概况：九江镇 2025 年 7 月十米地表温度产品。\n"
           "模型精度：测试集决定系数 0.87，属于优秀。\n"
           "闭合情况：闭合平均偏差 0.05 开尔文，这是算术均值闭合，不是十米精度。\n"
           "关键特征与局限：植被指数贡献最大，十米产品的极值范围更宽，"
           "这是分辨率提升的正常表现，局限在于云")
    res = _check(cut)
    _assert("E-R7" not in res.rule_hits, "默认不跑 E-R7（单条规则单测不受结构检查干扰）")
    res = eval_rules.check(cut, bundle=BUNDLE, require_structure=True)
    _assert(not res.ok and "E-R7" in res.rule_hits, "正式路径检出「停在半句话」")
    _assert(any("半句话" in v for v in res.violations), "违规项点明是被切断")

    res = eval_rules.check(GOOD_TEXT, bundle=BUNDLE, require_structure=True)
    _assert(res.ok, f"完整报告通过 E-R7（违规项：{res.violations}）")

    res = eval_rules.check("测试集决定系数 0.87。", bundle=BUNDLE, require_structure=True)
    _assert("E-R7" in res.rule_hits, "过短的正文被判为没写完")
    _assert(any("没有写完" in v for v in res.violations), "过短时直接判定没写完")

    # 长度够但四个小节一个都没有（刻意不含 产品概况/模型精度/闭合/局限 这些词）
    no_section = ("测试集决定系数 0.87，属于优秀。均方根误差 1.23 开尔文。"
                  "共比对 373,240 个格网，有效像元 4,231,905 个，覆盖完整。"
                  "整体结果可以使用。")
    res = eval_rules.check(no_section, bundle=BUNDLE, require_structure=True)
    _assert("E-R7" in res.rule_hits and any("必备小节" in v for v in res.violations),
            "缺少必备小节被检出（对应领域知识 E09）")

    _assert(eval_rules.check_structure(""), "空文本被判为没写完")
    _assert(eval_rules.check_structure(GOOD_TEXT) == [], "完整报告结构检查无问题")


def test_truncated_qualitative_uses_fallback():
    print("[17] 被切断/半截的定性说明 → 固定兜底句，不给用户看半截话")

    class _TruncatedLLM:
        def __init__(self):
            self.calls = 0

        def _call_api(self, messages, **kwargs):
            self.calls += 1
            return ("植被指数贡献最大，十米产品的极值范围更宽，"
                    "这是分辨率提升的正常表现，局限在于云")

    llm = _TruncatedLLM()
    agent = _agent_with(llm)
    report = agent.build_report(_Ctx())
    _assert(llm.calls == 1, "只调用一次 LLM（不再重写）")
    _assert(agent.note_reason and "收尾" in agent.note_reason,
            f"半截句被识别为未收尾：{agent.note_reason}")
    _assert("局限在于云" not in report, "半截话不进报告")
    last = [l for l in report.splitlines() if l.strip()][-1]
    _assert(not last.rstrip().endswith("。"),
            "最终输出段落结尾不带句号（半截话不会显示给用户）")
    _assert(eval_rules.check(report, bundle=BUNDLE, require_structure=False).ok,
            "兜底后的报告仍通过表述检查")


def test_tcr_statistics_surfaced():
    print("[18] 热约束残差统计进入事实清单与允许值表")
    agent = _agent_with(_BadLLM())
    agent.bundle["tcr_statistics"] = {"mean": 0.27, "std": 0.45, "n_valid_blocks": 732571}
    facts = agent.facts_text()
    _assert("热约束残差平均值：0.27 K" in facts, "事实清单给出残差平均值")
    _assert("有效格网：732,571 个" in facts, "事实清单给出有效格网数")

    bundle = {**BUNDLE, "tcr_statistics": agent.bundle["tcr_statistics"]}
    res = eval_rules.check("热约束残差平均 0.27 开尔文，有效格网 732,571 个。",
                           bundle=bundle, expected_grade="优秀")
    _assert("E-R1" not in res.rule_hits, "写对的残差统计不再被误判为编造")


def test_facts_text_only_real_numbers():
    print("[19] 事实清单只含真实数值")
    agent = _agent_with(_BadLLM())
    facts = agent.facts_text()
    # 只含测试集与闭合等仍展示的真实值（独立预测已彻底移除）
    for token in ("0.87", "1.23", "0.05", "0.40", "373,240", "4,231,905"):
        _assert(token in facts, f"事实清单包含真实值 {token}")
    from core.agent import presentation
    _assert(presentation.strip_emoji(facts) == facts, "事实清单不含表情符号")
    _assert("九江镇" in facts, "含研究区中文名")


def test_repair_extreme_negativity():
    print("[20] E-R4 确定性修复：极值差负面句被替换为 E03 中性表述")
    # 闭合正常（MB/MAE ≈ 0），文本以极值差下负面结论 → repair 后不再命中 E-R4
    closure_ok = eval_rules.closure_is_normal({"MB_K": 0.0, "MAE_K": 0.0})
    _assert(closure_ok, "闭合正常判据生效")
    draft = "关键特征与局限\n\n低端温差 -11.74 K 表明极值端偏差较大。"
    repaired = eval_rules.repair_extreme_negativity(draft, closure_ok)
    _assert("偏差较大" not in repaired, "负面判断词被替换")
    _assert("分辨率提升的预期表现" in repaired, "替换为 E03 中性表述")
    res = eval_rules.check(repaired, bundle=BUNDLE, expected_grade="优秀")
    _assert("E-R4" not in res.rule_hits, "修复后 E-R4 不再命中")
    # 非负面句（仅陈述极值）不应被误替换
    ok = "极值范围更宽是分辨率提升的正常表现。"
    _assert(eval_rules.repair_extreme_negativity(ok, closure_ok) == ok,
            "合法极值陈述保持原样")
    # 闭合异常时不替换（问题确实存在，规则放行）
    _assert(eval_rules.repair_extreme_negativity(draft, False) == draft,
            "闭合异常时不做 E-R4 替换")


def test_group_for_sentence_boundary():
    print("[21] E-R1 关键词窗口按句内截断，不跨句误归组")
    _assert(eval_rules._group_for("占比 82.0%。影像 15,477 行", 12) is None,
            "跨句的「占比」不误归到影像行数上")
    _assert(eval_rules._group_for("测试集决定系数 0.87，", 8) == "r2",
            "同句决定系数正常识别")
    _assert(eval_rules._group_for("共比对 7,761,388 个格网。", 4) is None,
            "「格网」在数字后、数字前无关键词 → 不归组")
    # 中文逗号也是分句符：上一分句的「个格网」不能归到下一分句的「30 米」上，
    # 否则合规的「…个格网，回聚合到 30 米格网」会被 E-R1 误判成
    # 「计数 30 与结果文件不一致」而连续重写失败。
    res = eval_rules.check("共比对 373,240 个格网，回聚合到 30 米格网，"
                           "闭合平均偏差 0.05 开尔文。", bundle=BUNDLE)
    _assert("E-R1" not in res.rule_hits, "合规口径句（含 30 米/0.05 K）不误判 E-R1")
    _assert(eval_rules._group_for("共比对 7,761,388 个格网，闭合平均偏差 0.00 K", 25) == "mb",
            "逗号后同分句的指标关键词仍正常识别")


def test_data_time_line_pair_and_monthly():
    print("[22] 数据时间一行：配对写卫星与日期，月度写月份")
    agent = _agent_with(_BadLLM())
    # 配对模式：写明实际卫星与日期
    agent.bundle["pair"] = {"landsat_date": "2024-07-17",
                            "landsat_satellite": "L9",
                            "sentinel2_date": "2024-07-22"}
    facts = agent.facts_text()
    _assert("- 数据时间：Landsat 9 2024-07-17 与 Sentinel-2 2024-07-22" in facts,
            "配对模式写明卫星与日期")
    # 月度合成模式：写月份
    agent.bundle["pair"] = {"landsat_date": "2024-07-31",
                            "sentinel2_date": "2024-07-31",
                            "composite": "monthly"}
    facts = agent.facts_text()
    _assert("- 数据时间：2024 年 7 月" in facts, "月度合成写月份")
    # 无配对信息：回退时间范围
    agent.bundle["pair"] = {}
    facts = agent.facts_text()
    _assert("- 数据时间：2025-07-01 至 2025-07-31" in facts, "无配对回退时间范围")


if __name__ == "__main__":
    test_good_text_passes()
    test_er1_numbers()
    test_er2_disallowed()
    test_er3_null_metric()
    test_er4_extreme_negativity()
    test_er5_grade_consistency()
    test_er6_closure_wording()
    test_multiple_violations()
    test_closure_normality()
    test_qualitative_rejected_uses_fallback()
    test_qualitative_note_included()
    test_llm_unavailable_uses_fallback()
    test_lst_total_valid_from_top_level()
    test_assemble_report_complete()
    test_er7_structure_and_truncation()
    test_truncated_qualitative_uses_fallback()
    test_tcr_statistics_surfaced()
    test_facts_text_only_real_numbers()
    test_repair_extreme_negativity()
    test_group_for_sentence_boundary()
    test_data_time_line_pair_and_monthly()
    print("\n✅ 评估轻反思 E-R1 – E-R7 测试全部通过")
