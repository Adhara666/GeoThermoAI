# -*- coding: utf-8 -*-
"""第一阶段（状态内核）服务器级端到端验收脚本（在容器内运行）。

用法：
    python tests/e2e_stage1_kernel.py --phase send
        注册/登录 → 建项目/对话 → 同一 request_id 连发两次 chat/start
        （第二次必须命中幂等：duplicate=True）→ 打印台账命令数
    python tests/e2e_stage1_kernel.py --phase verify
        重启服务后运行：登录 → 查台账 → 断言上一阶段的命令/消息/事件仍在

退出码 0 = 该阶段验收通过；非 0 = 失败（打印原因）。

注意：REQUEST_ID 每轮完整复测前换新编号——同一编号登记过之后，
"第一次发送"会命中幂等（这是期望行为，但会令 send 阶段的
"第一次发送非重复"断言失真）。
"""

import argparse
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:7860"
USERNAME = "stage1_e2e"
PASSWORD = "Stage1-E2E-2026"
REQUEST_ID = "e2e-req-003"
PROJECT = "阶段一验收"
CONV_TITLE = "kernel-e2e"
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append((name, detail))


def call(method, path, payload=None, token=""):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login():
    r = call("POST", "/api/auth/login", {"username": USERNAME, "password": PASSWORD})
    if not r.get("ok"):
        r = call("POST", "/api/auth/register",
                 {"username": USERNAME, "password": PASSWORD, "nickname": "阶段一验收"})
        check("注册验收用户", r.get("ok"), str(r))
        r = call("POST", "/api/auth/login", {"username": USERNAME, "password": PASSWORD})
    check("登录", r.get("ok"), str(r))
    return r["token"]


def phase_send():
    print("端到端 phase=send：命令接收 + 去重")
    token = login()
    projects = call("GET", "/api/bootstrap", token=token)["projects"]
    project = next((p["project"] for p in projects if p["project"] == PROJECT), None)
    if not project:
        r = call("POST", "/api/projects", {"name": PROJECT}, token=token)
        check("创建项目", r.get("ok"), str(r))
    convs = call("GET", "/api/bootstrap", token=token)["projects"]
    conv_id = None
    for p in convs:
        if p["project"] == PROJECT:
            for c in p["conversations"]:
                if c["title"] == CONV_TITLE:
                    conv_id = c["id"]
    if not conv_id:
        r = call("POST", "/api/conversations", {"project": PROJECT, "title": CONV_TITLE},
                 token=token)
        check("创建对话", r.get("ok"), str(r))
        conv_id = r.get("conv_id") or r.get("id")
        if not conv_id:  # 从 bootstrap 兜底拿
            for p in call("GET", "/api/bootstrap", token=token)["projects"]:
                if p["project"] == PROJECT:
                    conv_id = p["conversations"][-1]["id"]
    print(f"  project={PROJECT!r} conv={conv_id!r}")

    body = {"project": PROJECT, "conv": conv_id,
            "message": "状态内核端到端验收消息", "exec_mode": "approval",
            "chat_mode": "work", "request_id": REQUEST_ID}
    r1 = call("POST", "/api/chat/start", body, token=token)
    check("第一次发送 ok", r1.get("ok"), str(r1)[:200])
    check("第一次发送非重复", not r1.get("duplicate"), str(r1)[:200])

    r2 = call("POST", "/api/chat/start", body, token=token)
    check("第二次发送 ok", r2.get("ok"), str(r2)[:200])
    check("第二次发送命中幂等（duplicate=True，不再执行）",
          r2.get("duplicate") is True, str(r2)[:200])

    ledger = call("GET", "/api/kernel/ledger?limit=100", token=token)
    cmds = [c for c in ledger["commands"] if c["dedup_key"] == REQUEST_ID]
    check("台账中该请求编号只记一条命令", len(cmds) == 1, str(len(cmds)))
    evs = [e for e in ledger["events"] if e["type"] == "command.received"]
    check("台账中 command.received 事件已落库", len(evs) >= 1, str(len(evs)))
    # 按 message_id 精确关联本轮命令对应的用户消息（避免旧轮次同文本干扰）
    msg_ids = {c.get("message_id") for c in cmds}
    msgs = [m for m in ledger["messages"]
            if m["id"] in msg_ids and "端到端验收" in (m["content"] or "")]
    check("用户消息已先持久化", len(msgs) == 1, str(len(msgs)))

    print(f"\nphase=send 结果：{len(FAILS)} 项失败")
    return 0 if not FAILS else 1


def phase_verify():
    print("端到端 phase=verify：服务重启后台账持久化")
    token = login()
    ledger = call("GET", "/api/kernel/ledger?limit=200", token=token)
    cmds = [c for c in ledger["commands"] if c["dedup_key"] == REQUEST_ID]
    check("重启后命令仍在", len(cmds) == 1, str(len(cmds)))
    if cmds:
        check("重启后命令终态为 processed", cmds[0]["status"] == "processed",
              str(cmds[0]))
    msgs = [m for m in ledger["messages"]
            if m["id"] in {c.get("message_id") for c in cmds}
            and "端到端验收" in (m["content"] or "")]
    check("重启后用户消息仍在", len(msgs) == 1, str(len(msgs)))
    evs = [e for e in ledger["events"] if e["type"] == "command.received"]
    check("重启后事件仍在", len(evs) >= 1, str(len(evs)))

    # 重启后同键同载荷仍幂等（跨重启去重）
    # 需要找到原对话编号：从 bootstrap 找标题对应对话
    conv_id = None
    for p in call("GET", "/api/bootstrap", token=token)["projects"]:
        if p["project"] == PROJECT:
            for c in p["conversations"]:
                if c["title"] == CONV_TITLE:
                    conv_id = c["id"]
    body = {"project": PROJECT, "conv": conv_id,
            "message": "状态内核端到端验收消息", "exec_mode": "approval",
            "chat_mode": "work", "request_id": REQUEST_ID}
    r = call("POST", "/api/chat/start", body, token=token)
    check("重启后同键同载荷仍被去重", r.get("ok") and r.get("duplicate") is True,
          str(r)[:200])

    print(f"\nphase=verify 结果：{len(FAILS)} 项失败")
    return 0 if not FAILS else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["send", "verify"], required=True)
    args = parser.parse_args()
    code = phase_send() if args.phase == "send" else phase_verify()
    sys.exit(code)


if __name__ == "__main__":
    main()
