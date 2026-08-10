# -*- coding: utf-8 -*-
"""
气泡文案渲染层合成测试（技术方案 11.2 / 9.4 五条红线）

运行：python tests/test_presentation.py
覆盖：
- 对全部 8 个 Skill 的 message 转写结果做断言：
  不含表情符号、不含 ASCII 技能名、不含路径分隔符、不含变量名
- 阶段头、暂停、无配对等其它气泡文案同样合规
- 阶段中文名单一来源（server 与 agent 共用一份）
- 后端源码里不再有带表情符号的气泡调用
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core.agent import presentation
from core.skills.base_skill import SkillResult

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# 8 个内置 Skill 的真实 message 与 data（取自各 Skill 的实际返回格式）
SKILL_CASES = {
    "data_acquisition": (
        "数据下载完成: Landsat、Sentinel-2、DEM，耗时 128.4s",
        {"landsat_path": "/app/data/users/u1/workspace/x/raw/landsat_lst.tif",
         "output_dir": "/app/data/users/u1/workspace/x/raw"},
        ("影像下载完成",),
    ),
    "data_pipeline": (
        "预处理完成: 训练(step2) 45,678 行, 完整约束层 5,000 行, 预测 400,000 有效像素; "
        "空间块划分: 训练 27,406, 验证 9,136, 测试 9,136",
        {"train_rows": 45678, "constraint_rows": 5000, "predict_valid_pixels": 400000,
         "split_stats": {"train": {"count": 27406}, "validate": {"count": 9136},
                         "test": {"count": 9136}},
         "train_csv": "/app/x/processed/train.csv"},
        ("数据准备完成", "27,406", "9,136"),
    ),
    "ttri_compute": (
        "TTRI计算完成（仅train拟合一次）: R²=0.62, 系数 a(DEM)=0.001234；"
        "10m有效 4,231,905 行，约束层覆盖范围外 68 行",
        {"coefficients": {"r2": 0.62}, "total_valid": 4231905,
         "train_with_ttri": "/app/x/processed/train.csv"},
        ("地形热响应指数计算完成", "0.62", "4,231,905"),
    ),
    "rf_model": (
        "模型训练完成: 训练R²=0.90, 测试R²=0.87, RMSE=1.23, MB=0.12",
        {"train_metrics": {"train": {"R2": 0.90}},
         "test_metrics": {"R2": 0.87, "RMSE": 1.23, "MB": 0.12},
         "model_path": "/app/x/results/train/rf_ttri_model.pkl"},
        ("模型训练完成", "测试集决定系数 0.87", "均方根误差 1.23 K"),
    ),
    "tcr_compute": (
        "TCR计算完成（block_constant）: mean=0.0210K, std=0.4500K, 有效格=373,240, "
        "耗时 42.1s；不代表辐射或能量守恒",
        {"tcr_statistics": {"mean": 0.021, "std": 0.45, "n_valid_blocks": 373240},
         "predict_with_tcr": "/app/x/results/tcr_result.csv"},
        ("热约束残差计算完成", "373,240"),
    ),
    "lst_export": (
        "GeoTIFF导出完成: 2100×1800 像素, min=295.1, max=318.7, 有效率=92.4%, "
        "文件大小=28.51MB",
        {"image_size": {"height": 2100, "width": 1800},
         "stats": {"min": 295.1, "max": 318.7, "valid_percent": 92.4},
         "output_path": "/app/x/results/lst_10m.tif"},
        ("地表温度产品导出完成", "92.4%"),
    ),
    "accuracy_eval": (
        "粗尺度闭合评估完成: MB=0.05K, MAE=0.4K, RMSE=0.5K; 低端差=-0.45K, 高端差=-0.58K; "
        "匹配格数=373,240（不代表能量/辐射守恒，不是独立10m精度）",
        {"closure_metrics": {"closure": {"n_matched_cells": 373240,
                                         "metrics": {"MB_K": 0.05, "MAE_K": 0.40}}}},
        ("闭合校核完成", "0.05 K", "373,240"),
    ),
    "ai_assistant": (
        "AI助手 (diagnose) 完成",
        {"result": "...", "data": {}},
        ("智能分析完成",),
    ),
}

# 红线检查用的模式
_ASCII_SKILL_NAMES = tuple(SKILL_CASES)
# 路径判定复用生产 sanitize 的 _PATH_RE：合法的"8/9"（Landsat 8/9）、"row/col"等
# 表达不算路径（slash 前是字母/数字时不判为路径），真正的路径（/app/...、D:\...）才命中
_PATH_SEP_RE = presentation._PATH_RE
_VARNAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def _check_redlines(text, label):
    """红线 1/2：无表情符号、无 ASCII 技能名、无路径分隔符、无变量名。"""
    _assert(presentation.strip_emoji(text) == text, f"{label} 不含表情符号")
    hit = [n for n in _ASCII_SKILL_NAMES if n in text]
    _assert(not hit, f"{label} 不含英文技能名（命中：{hit}）")
    _assert(not _PATH_SEP_RE.search(text), f"{label} 不含路径分隔符")
    hit = _VARNAME_RE.findall(text)
    _assert(not hit, f"{label} 不含下划线变量名（命中：{hit}）")


def test_summarize_all_skills():
    print("[1] 8 个 Skill 的结果摘要全部合规")
    for skill, (message, data, expected) in SKILL_CASES.items():
        text = presentation.summarize(skill, SkillResult(True, message, data=data))
        _check_redlines(text, f"{presentation.stage_label(skill)} 摘要")
        for token in expected:
            _assert(token in text, f"{presentation.stage_label(skill)} 摘要含「{token}」")
        _assert(len(text.splitlines()) == 1, f"{presentation.stage_label(skill)} 摘要只有一行")


def test_summarize_failures_and_fallback():
    print("[2] 失败与缺数据时的摘要")
    for skill in SKILL_CASES:
        text = presentation.summarize(
            skill, SkillResult(False, "重建输入失败: /app/x/processed/train.csv 不存在"))
        _check_redlines(text, f"{presentation.stage_label(skill)} 失败摘要")
        _assert("未通过" in text, f"{presentation.stage_label(skill)} 失败时用中文「未通过」")

    # data 为空时不崩，也不泄露原始 message 里的路径
    text = presentation.summarize("data_pipeline", SkillResult(True, "预处理完成: /a/b/c.csv"))
    _check_redlines(text, "缺数据时的摘要")

    text = presentation.summarize("未知技能", SkillResult(True, "完成"))
    _check_redlines(text, "未知技能摘要")
    _assert("完成" in text, "未知技能给出兜底摘要")


def test_step_header():
    print("[3] 阶段头（红线 5：最多三行）")
    for skill in SKILL_CASES:
        text = presentation.step_header(1, 7, skill)
        _check_redlines(text, f"{presentation.stage_label(skill)} 阶段头")
        lines = [l for l in text.splitlines() if l.strip()]
        _assert(len(lines) <= 2, f"{presentation.stage_label(skill)} 阶段头不超过两行")
        _assert("第 1 步" in lines[0] and "共 7 步" in lines[0], "阶段头含步骤序号")
    _assert("／" in presentation.step_header(1, 7, "rf_model"),
            "步骤分隔用全角斜线，避免与路径分隔符混淆")


def test_other_bubbles():
    print("[4] 其它气泡文案")
    cases = {
        "已载入研究区": presentation.study_area_loaded("九江镇.geojson"),
        "开始规划": presentation.planning_started(),
        "规划重试": presentation.planning_retry(),
        "规划兜底": presentation.planning_fallback(),
        "方案就绪": presentation.plan_ready(7),
        "安全网补全": presentation.plan_completed_by_safety_net(),
        "推荐参数": presentation.tuning_params_suggesting(),
        "找到配对": presentation.pairs_found(5),
        "自动选择": presentation.pair_auto_selected(1, "云量很低、覆盖完整"),
        "开始下载": presentation.download_started(),
        "等待用户": presentation.waiting_for_user(),
        "技能缺失": presentation.skill_missing("rf_model"),
        "无配对": presentation.no_pair_reason({"landsat_count": 4, "sentinel_count": 6,
                                             "cloud_threshold": 30,
                                             "rejected_by_coverage": 2,
                                             "rejected_by_time_diff": 3}),
        "规则标注": presentation.rule_note("R3", "判定过拟合"),
    }
    for label, text in cases.items():
        _check_redlines(text, label)
    _assert(presentation.study_area_loaded("九江镇.geojson").strip()
            == "已载入研究区：九江镇", "研究区名去掉扩展名")
    _assert("共 7 步" in cases["方案就绪"], "方案就绪给出步骤数")
    _assert("Landsat 8/9 4 景" in cases["无配对"] and "Sentinel-2 6 景" in cases["无配对"],
            "无配对说清搜到了什么")
    _assert("覆盖不足被淘汰 2 组" in cases["无配对"], "无配对说清淘汰原因")
    _assert("已暂停" in cases["等待用户"], "暂停用中文状态词")


def test_number_formatting():
    print("[5] 数字格式化（红线 3）")
    _assert(presentation.fmt_count(4231905) == "4,231,905", "整数千分位")
    _assert(presentation.fmt_count("27406") == "27,406", "字符串数字可解析")
    _assert(presentation.fmt_count(None) == "未知", "缺值返回「未知」")
    _assert(presentation.fmt_count("abc") == "未知", "不可解析返回「未知」")
    _assert(presentation.fmt_num(0.8712) == "0.87", "浮点两位")
    _assert(presentation.fmt_num(None) == "未计算", "缺值返回「未计算」")
    _assert(presentation.fmt_num(None, missing="无") == "无", "缺值提示可定制")
    _assert(presentation.fmt_percent(92.44) == "92.4%", "百分比一位小数")
    _assert(presentation.fmt_percent(None) == "未知", "百分比缺值返回「未知」")


def test_sanitize():
    print("[6] 兜底清理")
    dirty = ("✅ rf_model 失败: 打开 /app/data/users/u1/results/train/rf_ttri_model.pkl "
             "时出错，train_csv 缺失 ⚠️")
    clean = presentation.sanitize(dirty)
    _check_redlines(clean, "清理后的文本")
    _assert("模型训练" in clean, "英文技能名被换成中文阶段名")
    # 只有真正的路径才替换：row/col、MB/MAE、训练/验证 这类正常写法必须原样保留
    keep = ("CSV 第 21 个批次中有 1 行 row/col 越界 (height=6612, width=5671)")
    cleaned_keep = presentation.sanitize(keep)
    _assert("row/col" in cleaned_keep,
            f"「row/col」不被当成路径吃掉（实际：{cleaned_keep}）")
    for text in ("MB/MAE 均为 0.00 K", "训练/验证/测试 划分完成", "云量 30%"):
        _assert(presentation.sanitize(text) == text, f"「{text}」原样保留")
    for path in ("/app/data/users/u1/raw/dem.tif", "./output/raw", "D:\\work\\a.tif"):
        _assert("（详见日志）" in presentation.sanitize(f"打开 {path} 失败"),
                f"真正的路径仍被替换：{path}")

    _assert(presentation.sanitize("") == "", "空串安全")
    _assert(presentation.sanitize(None) == "", "None 安全")
    _assert(presentation.strip_emoji("完成✅") == "完成", "表情符号被剥离")
    _assert(presentation.strip_emoji("九江镇 2025 年 7 月") == "九江镇 2025 年 7 月",
            "中文与数字不被误删")


def test_single_source_labels():
    print("[7] 阶段中文名单一来源")
    import server

    _assert(server._WORKFLOW_LABELS is presentation.WORKFLOW_LABELS,
            "server 的工作流标签直接引用 presentation 的字典（同一对象）")
    from core.agent import geo_thermo_agent

    _assert(geo_thermo_agent._STEP_DESCRIPTIONS
            is presentation.LEGACY_STEP_DESCRIPTIONS,
            "Agent 的阶段说明也来自 presentation")
    from core.agent import plan_schema

    for skill in plan_schema.WORKFLOW_STEPS:
        _assert(skill in presentation.STAGE_LABELS, f"{skill} 有中文阶段名")
        _assert(skill in presentation.WORKFLOW_LABELS, f"{skill} 有面板短标签")
        _assert(skill in presentation.STAGE_DESCRIPTIONS, f"{skill} 有阶段说明")
    for skill, label in presentation.STAGE_LABELS.items():
        _check_redlines(label, f"阶段名「{label}」")
    for skill, desc in presentation.STAGE_DESCRIPTIONS.items():
        _check_redlines(desc, f"阶段说明「{desc[:12]}…」")


def test_status_words():
    print("[8] 状态词用中文（红线 2）")
    expected = {"running": "开始", "completed": "完成", "failed": "未通过",
                "paused": "已暂停", "aborted": "已停止"}
    _assert(presentation.STATUS_WORDS == expected,
            "状态词表与技术方案 9.4 红线 2 一致")
    for word in expected.values():
        _check_redlines(word, f"状态词「{word}」")


def test_backend_sources_have_no_emoji_bubbles():
    print("[9] 后端源码里不再有带表情符号的气泡调用")
    targets = ["core/agent/executor.py", "core/agent/geo_thermo_agent.py",
               "core/agent/presentation.py", "core/agent/orchestrator/role_hooks.py",
               "core/agent/roles/data_agent.py", "core/agent/roles/train_agent.py",
               "core/agent/roles/eval_agent.py"]
    offenders = []
    for rel in targets:
        path = os.path.join(_ROOT, rel)
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if ("_emit(" in line or "ctx.emit(" in line) and \
                    presentation.strip_emoji(line) != line:
                offenders.append(f"{rel}:{i}")
    _assert(offenders == [], f"气泡调用无表情符号（命中：{offenders}）")

    # server 的思考链标签也已中文化
    path = os.path.join(_ROOT, "server.py")
    content = open(path, encoding="utf-8").read()
    _assert("思考过程" in content, "思考链标签已中文化")
    _assert("💭" not in content, "思考链标签不再使用表情符号")


if __name__ == "__main__":
    test_summarize_all_skills()
    test_summarize_failures_and_fallback()
    test_step_header()
    test_other_bubbles()
    test_number_formatting()
    test_sanitize()
    test_single_source_labels()
    test_status_words()
    test_backend_sources_have_no_emoji_bubbles()
    print("\n✅ 气泡文案渲染层测试全部通过")
