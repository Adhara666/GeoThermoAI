# -*- coding: utf-8 -*-
"""验证：调优决定节点的运行时模式优先（完全执行不弹卡/由我批准仍弹卡）。"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/app")

from core.agent.orchestrator.hooks import StepDecision  # noqa: E402
from core.scheduling import adapters  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s" % name)
    else:
        fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


captured = {}


class FakeTrainAgent:
    """替身 TrainAgent：记录 hooks.exec_mode，按场景返回 continue / pause。"""

    def __init__(self, assistant, registry, on_log=None, max_rounds=5):
        self.rounds = []
        self.selected = {"round": 0, "test_r2": 0.8}
        self.tuning_started = False
        self.decided_no_tuning = False
        self.manual_params = {}
        self.ai_rounds = 0
        self._round_kind = "ai"

    def on_trained(self, result, ctx, hooks):
        captured["exec_mode"] = getattr(hooks, "exec_mode", "")
        self.rounds.append({"round": 0})
        if captured.get("expect_pause"):
            return StepDecision.pause(
                {"type": "approval", "node": "tuning_decision",
                 "title": "t", "summary": "s",
                 "options": [{"id": "ai_tune", "label": "x", "recommended": True}],
                 "default_option": "ai_tune"},
                reason="等待用户选择是否调优")
        return StepDecision.cont()


import core.agent.roles.train_agent as ta  # noqa: E402

ta.TrainAgent = FakeTrainAgent

tmp = Path(tempfile.mkdtemp())
settings = tmp / "settings.json"
settings.write_text(json.dumps({"api": {}}), encoding="utf-8")


def make_spec(frozen_mode, runtime_mode):
    return {
        "runtime_exec_mode": runtime_mode,
        "snapshot": {
            # 冻结快照的 params 是带 provenance 包装的结构（value/source/evidence）
            "params": {
                "exec_mode": {"value": frozen_mode, "source": "test",
                               "evidence": ""},
                "tuning_max_rounds": {"value": 5, "source": "test",
                                      "evidence": ""},
            },
            "execution": {"settings_path": str(settings)},
        },
        "node": {"params": json.dumps({})},
    }


context = {"workspace": str(tmp), "rf_data": {}, "rf_directory": str(tmp),
           "rounds": [], "train_state": {}}

# ① 冻结 approval + 运行时 auto → 不弹卡（决策直接继续）
captured.clear()
captured["expect_pause"] = False
out = adapters._train_decision(make_spec("approval", "auto"), dict(context),
                               report=lambda *a, **k: None)
check("① 运行时可覆盖冻结值：返回 decision", "decision" in out, str(list(out)))
check("① hooks.exec_mode == auto", captured.get("exec_mode") == "auto",
      str(captured.get("exec_mode")))

# ② 运行时 approval → 仍弹卡（未切模式时行为不变）
captured.clear()
captured["expect_pause"] = True
out2 = adapters._train_decision(make_spec("auto", "approval"), dict(context),
                                report=lambda *a, **k: None)
check("② 运行时 approval 仍弹卡（含候选）",
      "question" in out2 and bool(out2["question"].get("candidates")))
check("② hooks.exec_mode == approval", captured.get("exec_mode") == "approval")

# ③ 运行时为空 → 回退冻结值（保守行为不变）
captured.clear()
captured["expect_pause"] = True
out3 = adapters._train_decision(make_spec("approval", ""), dict(context),
                                report=lambda *a, **k: None)
check("③ 运行时为空回退冻结 approval 弹卡", "question" in out3)
check("③ hooks.exec_mode == approval（回退）",
      captured.get("exec_mode") == "approval")

print("\n结果：%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
