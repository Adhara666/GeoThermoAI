# -*- coding: utf-8 -*-
"""第二阶段（理解层）服务器级端到端验收脚本（在容器内运行）。

逐条覆盖 docs/升级方案_通俗解读版.md 9.2 的验收表：

| 你说 | 必须看到 |
|---|---|
| 武汉 7 月、南京 8 月，都是 2025 年，月度产品 | 两个任务，各自绑定正确月份，不串 |
| 不是武汉（随后接着说别的） | 武汉被清除，后续不出现武汉下载 |
| 武汉去年 7 月做 LST | 追问年份或用明确的锚点日期，不拿当前年冒充 |
| 只下载 Sentinel-2 | 只下载，不自动开始训练 |
| Chat 模式问「什么是地表温度」 | 只回答，不建任何任务 |

外加**重启加分项**：追问出现后重启服务，问题还在；回答后任务继续。

用法：
    python3 tests/e2e_stage2_understanding.py --phase send
        建账号/项目/对话 → 上传研究区 → 依次发 5 条消息 → 断言台账状态
        → 制造一个待答问题留给 verify 阶段
    python3 tests/e2e_stage2_understanding.py --phase verify
        重启服务后运行：断言待答问题仍在、回答后任务转为就绪

模型：默认指向 tests/stub_model_server.py 起的**脚本化网关**，
让本脚本在没有真实模型凭据的机器上也能原样复跑，且模型返回的原始 JSON
会被完整记录下来。要用真实模型复跑，加 `--model-base-url/--model-key/
--model-id` 指向真实网关即可，被测代码一行不改。
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BASE = "http://127.0.0.1:7860"
PROJECT = "阶段二验收"
CONV_TITLE = "understanding-e2e"
STUB_PORT = 18000
_DEFAULT_STATE = "/tmp/stage2_e2e_state.json"

FAILS = []
RECORDS = []   # 模型原始输出记录


def _state_path() -> Path:
    return Path(os.environ.get("GTAI_E2E_STAGE2_STATE", _DEFAULT_STATE))


def load_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(update: dict) -> dict:
    state = load_state()
    state.update(update)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def resolve_credentials():
    """本地端到端账号：环境变量优先，否则运行时生成并写入 send/verify 状态文件。

    不是外部服务或生产凭据。默认用户名 stage2_e2e 只是本地注册名，密码不进仓库。
    """
    user = os.environ.get("GTAI_E2E_USERNAME", "").strip() or "stage2_e2e"
    env_password = os.environ.get("GTAI_E2E_PASSWORD", "").strip()
    if env_password:
        return user, env_password
    state = load_state()
    if state.get("username") and state.get("password"):
        return state["username"], state["password"]
    password = secrets.token_urlsafe(18)
    save_state({"username": user, "password": password})
    return user, password


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append((name, detail))


def call(method, path, payload=None, token="", timeout=180):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login():
    username, password = resolve_credentials()
    try:
        r = call("POST", "/api/auth/login",
                 {"username": username, "password": password})
    except urllib.error.HTTPError:
        r = {"ok": False}
    if not r.get("ok"):
        call("POST", "/api/auth/register",
             {"username": username, "password": password, "nickname": "阶段二验收"})
        r = call("POST", "/api/auth/login",
                 {"username": username, "password": password})
    check("登录", r.get("ok"), str(r))
    return r["token"]


def ensure_project(token):
    tree = call("GET", "/api/bootstrap", token=token)["projects"]
    if not any(p["project"] == PROJECT for p in tree):
        r = call("POST", "/api/projects", {"name": PROJECT}, token=token)
        check("创建项目", r.get("ok"), str(r))


def find_conversation(token, title):
    for p in call("GET", "/api/bootstrap", token=token)["projects"]:
        if p["project"] == PROJECT:
            for c in p.get("conversations", []):
                if c["title"] == title:
                    return c["id"]
    return ""


def make_conversation(token, title):
    existing = find_conversation(token, title)
    if existing:
        return existing
    r = call("POST", "/api/conversations",
             {"project": PROJECT, "title": title}, token=token)
    check(f"创建对话 {title}", r.get("ok"), str(r))
    return r.get("conv_id") or find_conversation(token, title)


def upload_study_areas(token, names):
    """用 multipart 上传研究区 GeoJSON（真实上传路径，不直接写盘）。"""
    import io
    import uuid as _uuid

    boundary = "----gtai" + _uuid.uuid4().hex
    body = io.BytesIO()
    for name in names:
        payload = json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"name": name},
                          "geometry": {"type": "Polygon", "coordinates": [[
                              [114.0, 30.4], [114.6, 30.4], [114.6, 30.8],
                              [114.0, 30.8], [114.0, 30.4]]]}}],
        }, ensure_ascii=False).encode("utf-8")
        body.write(f"--{boundary}\r\n".encode())
        body.write(('Content-Disposition: form-data; name="files"; '
                    f'filename="{name}.geojson"\r\n').encode("utf-8"))
        body.write(b"Content-Type: application/geo+json\r\n\r\n")
        body.write(payload)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "/api/study-area", data=body.getvalue(), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def configure_model(token, base_url, key, model_id):
    r = call("POST", "/api/settings", {
        "api_format": "openai", "base_url": base_url,
        "api_key": key, "model_id": model_id,
        "display_name": model_id, "model_type": "deepseek",
    }, token=token)
    check("配置模型网关", r.get("ok", True) is not False, str(r))


def send(token, conv_id, message, chat_mode="work", request_id="",
         expect_ledger=True, timeout=90):
    """发一条消息，等到「理解层已落台账」或本轮流结束为止。

    理解层在把任务交给执行链**之前**就把任务卡与问题写进了台账，所以
    等台账出现变化即可，不必等整条生产链跑完（那属于阶段 4 的调度范畴）。
    """
    before = session(token, conv_id) if expect_ledger else {}
    signature = _ledger_signature(before)

    # 阶段 2 还没有调度器：同一对话同时只跑一轮，先等它让出来再发下一条。
    # 每次重试都用新的请求编号——沿用旧编号会命中去重，等于什么都没发。
    r = {}
    for attempt in range(30):
        if call("GET", "/api/chat/streaming?" + _query(conv=conv_id),
                token=token).get("active"):
            time.sleep(2)
            continue
        r = call("POST", "/api/chat/start", {
            "project": PROJECT, "conv": conv_id, "message": message,
            "exec_mode": "approval", "chat_mode": chat_mode,
            "request_id": request_id or f"e2e-{int(time.time() * 1000)}-{attempt}",
        }, token=token)
        if r.get("ok") or "已有任务在执行中" not in str(r.get("message") or ""):
            break
        time.sleep(2)
    check(f"发送「{message[:18]}…」被接收", r.get("ok"), str(r)[:200])

    deadline = time.time() + timeout
    while time.time() < deadline:
        if expect_ledger and _ledger_signature(session(token, conv_id)) != signature:
            return r
        if not call("GET", "/api/chat/streaming?" + _query(conv=conv_id),
                    token=token).get("active"):
            return r
        time.sleep(1)
    return r


def _ledger_signature(snapshot):
    """任务卡 + 待答问题的指纹：理解层写完台账就会变。"""
    tasks = [(t.get("task_id"), t.get("version"), t.get("summary_status"))
             for t in (snapshot.get("tasks") or [])]
    questions = [q.get("id") for q in (snapshot.get("questions") or [])]
    return json.dumps([tasks, questions], ensure_ascii=False, sort_keys=True)


def _query(**params):
    return urllib.parse.urlencode(params)


def session(token, conv_id):
    return call("GET", "/api/kernel/session?"
                + _query(project=PROJECT, conv=conv_id), token=token)


def last_bubble(token, conv_id):
    msgs = call("GET", "/api/messages?" + _query(project=PROJECT, conv=conv_id),
                token=token).get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            return str(m.get("content") or "")
    return ""


# ── 验收场景 ─────────────────────────────────────────────────────


def scenario_two_cities(token, conv_id):
    print("\n验收①：武汉 7 月、南京 8 月，都是 2025 年，月度产品")
    send(token, conv_id, "武汉 7 月、南京 8 月，都是 2025 年，月度产品")
    snap = session(token, conv_id)
    tasks = snap.get("tasks") or []
    check("登记了两个任务", len(tasks) == 2, str([t["label"] for t in tasks]))
    by_region = {t.get("region"): t for t in tasks}
    wuhan = next((t for r, t in by_region.items() if r and "武汉" in r), None)
    nanjing = next((t for r, t in by_region.items() if r and "南京" in r), None)
    check("分别绑定到武汉与南京", wuhan and nanjing, str(list(by_region)))
    if wuhan and nanjing:
        check("武汉绑定 2025 年 7 月",
              wuhan["time_start"] == "2025-07-01"
              and wuhan["time_end"] == "2025-07-31",
              f"{wuhan['time_start']}~{wuhan['time_end']}")
        check("南京绑定 2025 年 8 月（月份不串）",
              nanjing["time_start"] == "2025-08-01"
              and nanjing["time_end"] == "2025-08-31",
              f"{nanjing['time_start']}~{nanjing['time_end']}")
        check("两个任务都是月度合成",
              wuhan["product_mode"] == "月度合成模式"
              and nanjing["product_mode"] == "月度合成模式",
              f"{wuhan['product_mode']}/{nanjing['product_mode']}")
    check("信息齐全时不追问", not (snap.get("questions") or []),
          str(snap.get("questions")))
    return tasks


def scenario_negation(token, conv_id_factory):
    print("\n验收②：不是武汉（随后接着说别的）")
    # 第一轮故意只给地区不给时间：任务登记但不就绪，不会真的开跑生产链
    #（阶段 2 还没有调度器，执行中的对话不接受新消息，那是阶段 4 的事）
    conv_id = conv_id_factory("negation")
    send(token, conv_id, "给武汉做个地表温度")
    before = session(token, conv_id).get("tasks") or []
    check("否定之前确实有一个武汉任务",
          any((t.get("region") or "").startswith("武汉") for t in before),
          str([t.get("region") for t in before]))

    send(token, conv_id, "不是武汉")
    snap = session(token, conv_id)
    tasks = snap.get("tasks") or []
    check("武汉已被清除",
          all(not (t.get("region") or "").startswith("武汉") for t in tasks),
          str([t.get("region") for t in tasks]))
    negated = [n for t in tasks for n in (t.get("negations") or [])]
    check("否定记录已落台账",
          any("武汉" in str(n.get("value")) for n in negated), str(negated))

    send(token, conv_id, "那就按 2025 年 8 月来吧")
    snap2 = session(token, conv_id)
    tasks2 = snap2.get("tasks") or []
    check("后续一轮不把武汉填回来",
          all(not (t.get("region") or "").startswith("武汉") for t in tasks2),
          str([t.get("region") for t in tasks2]))
    check("没有任何武汉任务进入就绪（不会出现武汉下载）",
          all(t.get("summary_status") != "ready"
              or not (t.get("region") or "").startswith("武汉") for t in tasks2),
          str([(t.get("region"), t.get("summary_status")) for t in tasks2]))


def scenario_relative_year(token, conv_id_factory):
    print("\n验收③：武汉去年 7 月做 LST")
    conv_id = conv_id_factory("relyear")
    send(token, conv_id, "武汉去年 7 月做 LST，配对模式")
    snap = session(token, conv_id)
    tasks = snap.get("tasks") or []
    check("登记了一个任务", len(tasks) == 1, str(tasks))
    if tasks:
        start = tasks[0].get("time_start") or ""
        this_year = time.strftime("%Y")
        check("不拿当前年冒充",
              start[:4] != this_year and start.endswith("-07-01"),
              f"实际 {start}（今年 {this_year}）")
        check("解析成明确的锚点年份",
              start[:4].isdigit() and int(start[:4]) == int(this_year) - 1,
              f"实际 {start}")


def scenario_download_only(token, conv_id_factory):
    print("\n验收④：只下载 Sentinel-2")
    conv_id = conv_id_factory("download")
    send(token, conv_id, "武汉 2025 年 7 月，只下载 Sentinel-2，不要训练")
    snap = session(token, conv_id)
    tasks = snap.get("tasks") or []
    check("登记了一个下载任务", len(tasks) == 1, str(tasks))
    if tasks:
        check("能力是下载指定数据",
              tasks[0].get("capability") == "下载指定数据",
              str(tasks[0].get("capability")))
        check("数据集合只有 Sentinel-2",
              tasks[0].get("datasets") == ["sentinel2"],
              str(tasks[0].get("datasets")))
    bubble = last_bubble(token, conv_id)
    check("气泡里没有出现训练阶段",
          "训练" not in bubble or "不" in bubble, bubble[:200])


def scenario_chat_mode(token, conv_id_factory):
    print("\n验收⑤：Chat 模式问「什么是地表温度」")
    conv_id = conv_id_factory("chatmode")
    send(token, conv_id, "什么是地表温度", chat_mode="chat",
         expect_ledger=False, timeout=45)
    snap = session(token, conv_id)
    check("Chat 模式没有建任何任务", not (snap.get("tasks") or []),
          str(snap.get("tasks")))
    check("Chat 模式没有产生任何问题", not (snap.get("questions") or []),
          str(snap.get("questions")))


def scenario_pending_question(token, conv_id_factory):
    """给 verify 阶段留一个待答问题（重启加分项）。"""
    print("\n重启加分项（send 半场）：制造一个待答问题")
    conv_id = conv_id_factory("restart")
    send(token, conv_id, "给武汉做个地表温度")
    snap = session(token, conv_id)
    questions = snap.get("questions") or []
    check("追问已落台账", len(questions) == 1, str(questions))
    if questions:
        check("问题绑定了任务与版本",
              bool(questions[0].get("targets")),
              str(questions[0].get("targets")))
    check("任务处于等待信息状态",
          any(t.get("summary_status") == "awaiting_info"
              for t in (snap.get("tasks") or [])),
          str([t.get("summary_status") for t in (snap.get("tasks") or [])]))
    return conv_id


def scenario_dedup(token, conv_id_factory):
    print("\n旧功能不坏：命令去重仍然生效（阶段 1 能力）")
    conv_id = conv_id_factory("dedup")
    rid = "stage2-dedup-001"
    send(token, conv_id, "什么是地表温度", chat_mode="chat", request_id=rid,
         expect_ledger=False, timeout=45)
    r2 = call("POST", "/api/chat/start",
              {"project": PROJECT, "conv": conv_id, "message": "什么是地表温度",
               "exec_mode": "approval", "chat_mode": "chat", "request_id": rid},
              token=token)
    check("同一请求编号第二次命中幂等", r2.get("duplicate") is True, str(r2)[:200])


def scenario_command_closure(token, busy_conv_id):
    """占线被拒的命令也要有终态，不能永久停在「已接收」（§3.4 处理闭环）。"""
    print("\n处理闭环：对话占线时被拒的命令写入拒绝终态")
    if not call("GET", "/api/chat/streaming?" + _query(conv=busy_conv_id),
                token=token).get("active"):
        print("  [SKIP] 该对话当前没有在执行的任务，跳过占线拒绝检查")
        return
    rid = f"stage2-busy-{int(time.time() * 1000)}"
    r = call("POST", "/api/chat/start",
             {"project": PROJECT, "conv": busy_conv_id, "message": "再加一个南京",
              "exec_mode": "approval", "chat_mode": "work", "request_id": rid},
             token=token)
    check("对话占线时明确拒绝新命令",
          r.get("ok") is False and "已有任务在执行中" in str(r.get("message")),
          str(r)[:200])
    ledger = call("GET", "/api/kernel/ledger?limit=200", token=token)
    row = next((c for c in ledger.get("commands") or []
                if c.get("dedup_key") == rid), None)
    check("被拒命令在台账里是拒绝终态，不停在已接收",
          row is not None and row.get("status") == "rejected", str(row))


# ── 阶段入口 ─────────────────────────────────────────────────────


def phase_send(args):
    print("端到端 phase=send：理解层 5 条验收场景")
    token = login()
    configure_model(token, args.model_base_url, args.model_key, args.model_id)
    ensure_project(token)
    upload_study_areas(token, ["武汉市_市", "南京市_市"])

    created = {}

    def conv_factory(tag):
        if tag not in created:
            created[tag] = make_conversation(token, f"{CONV_TITLE}-{tag}")
        return created[tag]

    scenario_two_cities(token, conv_factory("two_cities"))
    scenario_negation(token, conv_factory)
    scenario_relative_year(token, conv_factory)
    scenario_download_only(token, conv_factory)
    scenario_chat_mode(token, conv_factory)
    restart_conv = scenario_pending_question(token, conv_factory)
    scenario_dedup(token, conv_factory)
    scenario_command_closure(token, conv_factory("two_cities"))

    save_state({"restart_conv": restart_conv})
    print(f"\nphase=send 结果：{len(FAILS)} 项失败")
    return 0 if not FAILS else 1


def phase_verify(args):
    print("端到端 phase=verify：重启后待答问题仍在、回答后任务继续")
    token = login()
    state = load_state()
    if not state.get("restart_conv"):
        print("  缺少 send 阶段留下的状态文件，先跑 --phase send")
        return 1
    conv_id = state["restart_conv"]

    snap = session(token, conv_id)
    questions = snap.get("questions") or []
    check("重启后待答问题仍在", len(questions) == 1, str(questions))
    if not questions:
        return 1
    question = questions[0]
    check("问题正文与候选完整保留", bool(question.get("prompt")),
          str(question))

    r = call("POST", "/api/kernel/answer",
             {"project": PROJECT, "conv": conv_id,
              "question_id": question["id"], "answer": "2025 年 7 月"},
             token=token)
    check("卡片式回答被接受", r.get("ok"), str(r)[:200])

    snap2 = session(token, conv_id)
    tasks = snap2.get("tasks") or []
    check("答案已写入任务",
          any(t.get("time_start") == "2025-07-01" for t in tasks),
          str([t.get("time_start") for t in tasks]))
    follow = snap2.get("questions") or []
    check("只继续问真正还缺的信息（产品方式）",
          len(follow) == 1 and "整月" in str(follow[0].get("prompt")),
          str([q.get("prompt") for q in follow]))

    if follow:
        r2 = call("POST", "/api/kernel/answer",
                  {"project": PROJECT, "conv": conv_id,
                   "question_id": follow[0]["id"], "answer": "配对模式"},
                  token=token)
        check("补齐后回答被接受", r2.get("ok"), str(r2)[:200])
        # 过期卡片再点一次：必须提示已失效，而不是错误执行
        r3 = call("POST", "/api/kernel/answer",
                  {"project": PROJECT, "conv": conv_id,
                   "question_id": follow[0]["id"], "answer": "月度合成模式"},
                  token=token)
        check("过期卡片再点提示已失效",
              r3.get("ok") is False and r3.get("expired") is True, str(r3)[:200])

    snap3 = session(token, conv_id)
    # 第四阶段起调度器在线：任务补齐信息后可能已被编译推进（ready→queued→…），
    # 这里接受“已不再缺信息”的全部后续状态
    check("补齐后任务进入就绪或已被调度",
          any(t.get("summary_status") in
              ("ready", "queued", "running", "completed")
              for t in (snap3.get("tasks") or [])),
          str([(t.get("label"), t.get("summary_status"))
               for t in (snap3.get("tasks") or [])]))
    check("补齐后不再有待答问题", not (snap3.get("questions") or []),
          str(snap3.get("questions")))

    print(f"\nphase=verify 结果：{len(FAILS)} 项失败")
    return 0 if not FAILS else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["send", "verify"], required=True)
    parser.add_argument("--model-base-url",
                        default=f"http://127.0.0.1:{STUB_PORT}")
    parser.add_argument("--model-key", default="stub-key")
    parser.add_argument("--model-id", default="scripted-candidate-model")
    args = parser.parse_args()
    sys.exit(phase_send(args) if args.phase == "send" else phase_verify(args))


if __name__ == "__main__":
    main()
