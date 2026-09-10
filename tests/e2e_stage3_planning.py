# -*- coding: utf-8 -*-
"""第三阶段（计划编译）服务器级端到端验收脚本（在容器内运行）。

对应 docs/升级方案_通俗解读版.md 9.3 验收条款：
  1. 提交任务 → 编译排队 → 排队期间去设置界面改参数 → 查该任务的
     冻结参数快照：**必须还是提交（编译）时的值**（9.3 验收第 1 条）。
  2. 任务详情可见节点拆分与依赖顺序（先下载后预处理）（9.3 验收第 2 条）。

任务来源：复用第二阶段 stub 模型网关（tests/stub_model_server.py），
经理解层创建确认任务；编译与快照查询走第三阶段 API
（POST /api/planning/compile、GET /api/planning/run/{run_id}）。

容器内运行步骤：
    python3 tests/stub_model_server.py --port 18000 &   # 先起 stub 网关
    python3 tests/e2e_stage3_planning.py                # 再跑本脚本
退出码 0 = 验收通过。
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

import e2e_stage2_understanding as s2  # noqa: E402

# 本地验收账号：用从未注册过的专用用户名 + 持久卷状态文件，
# 保证容器重建后登录稳定（密码留在卷内状态文件中）
s2.os.environ["GTAI_E2E_USERNAME"] = "stage3_e2e"
s2._DEFAULT_STATE = "/app/data/state_kernel/.stage3_e2e_state.json"
s2.PROJECT = "阶段三验收"   # 复用第二阶段辅助函数，但换成阶段三的项目/对话
# 对话标题带时间戳：脚本可重复执行，避免同句消息命中命令去重
CONV_TITLE = f"planning-e2e-{int(time.time())}"
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append((name, detail))


def call(method, path, payload=None, token="", timeout=180):
    req = urllib.request.Request(
        s2.BASE + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_ready_task(token, conv_id, timeout_s=120):
    """轮询会话快照，直到出现已确认（可编译）的完整 LST 武汉任务。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = s2.session(token, conv_id)
        for t in snap.get("tasks") or []:
            if str(t.get("region")).startswith("武汉") and t.get("time_start") \
                    and "完整" in str(t.get("capability")) \
                    and "月度" in str(t.get("product_mode")):
                return t
        time.sleep(3)
    return None


def main() -> int:
    print("端到端（第三阶段）：计划编译 + 快照冻结验收")
    token = s2.login()
    s2.configure_model(token, "http://127.0.0.1:18000", "stub-key",
                       "scripted-candidate-model")
    s2.ensure_project(token)
    s2.upload_study_areas(token, ["武汉市_市", "南京市_市"])

    conv_id = s2.make_conversation(token, CONV_TITLE)
    # 与第二阶段验收①同一句：stub 网关脚本化返回两个 full_lst 任务
    s2.send(token, conv_id, "武汉 7 月、南京 8 月，都是 2025 年，月度产品")

    task = wait_ready_task(token, conv_id)
    check("理解层产出已确认的完整 LST 任务（武汉月度）", task is not None)
    if not task:
        return 1
    task_id = task["task_id"]
    version = int(task.get("version") or 1)
    print(f"  task_id={task_id} version={version}")

    # ── 验收 1：编译 → 排队期间改设置 → 快照不变 ──
    r = call("POST", "/api/planning/compile",
             {"task_id": task_id, "expected_version": version}, token=token)
    check("编译被接受", r.get("ok"), str(r)[:300])
    if not r.get("ok"):
        return 1
    run_id = r["run_id"]
    check("模板版本登记", r.get("template_version") == "plan-catalog-v1")

    detail1 = call("GET", f"/api/planning/run/{run_id}", token=token)
    check("可查冻结参数快照", detail1.get("ok")
          and bool(detail1.get("snapshot")), str(detail1)[:200])
    snap1 = detail1["snapshot"]
    cloud_before = (snap1["params"].get("cloud_threshold") or {}).get("value")
    check("快照云量为编译时设置值（默认 30）", cloud_before == 30,
          f"实际 {cloud_before}")
    check("快照逐项含来源（region=user，cloud=default）",
          (snap1["params"].get("region") or {}).get("source") == "user"
          and (snap1["params"].get("cloud_threshold") or {}).get("source")
          == "default",
          str(snap1["params"].get("cloud_threshold"))[:120])

    # 排队期间改设置（模型参数改为不同的值；走真实设置端点）。
    # 不假设编译时具体值：记录编译值 rf_before，改为 rf_before + 300，
    # 断言快照仍是编译时的 rf_before（与持久卷遗留值无关，可重复执行）。
    rf_before = (((snap1["params"].get("rf_params") or {}).get("value") or {})
                 .get("n_estimators"))
    rf_after_change = (rf_before or 200) + 300
    r_set = call("POST", "/api/model-params",
                 {"n_estimators": rf_after_change, "max_depth": 25,
                  "min_samples_split": 16, "min_samples_leaf": 8},
                 token=token)
    check("排队期间修改设置被接受", _settings_ok(r_set), str(r_set)[:200])

    detail2 = call("GET", f"/api/planning/run/{run_id}", token=token)
    rf_after = (((detail2.get("snapshot") or {}).get("params") or {})
                .get("rf_params", {}).get("value", {})).get("n_estimators")
    check("改设置后冻结快照仍是编译时的值（快照不可变）",
          rf_after == rf_before, f"编译时 {rf_before}，实际 {rf_after}")

    # ── 验收 2：节点清单与依赖顺序 ──
    nodes = detail1.get("nodes") or []
    types = [n["type"] for n in nodes]
    check(f"节点数 = 主链模板数（{len(nodes)}）", len(nodes) == 15,
          f"实际 {types}")
    check("先下载后预处理（acquire 在 preprocess 之前）",
          "acquire_asset" in types and "preprocess_split" in types
          and types.index("acquire_asset") < types.index("preprocess_split"))
    check("主链末端为导出 → 闭合", types[-2:] == ["export", "closure_eval"])
    check("全部节点排队态（本阶段不派发执行）",
          all(n["status"] == "pending" for n in nodes))

    print(f"\n第三阶段端到端结果：{len(FAILS)} 项失败")
    return 0 if not FAILS else 1


def _settings_ok(r) -> bool:
    if isinstance(r, dict):
        return r.get("ok") is not False
    return True


if __name__ == "__main__":
    sys.exit(main())
