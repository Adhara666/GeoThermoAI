# -*- coding: utf-8 -*-
"""可复现的「脚本化模型」：一个最小 OpenAI 兼容网关。

用途：让阶段 2 的端到端验收可以在**没有真实模型凭据**的机器上原样复跑，
同时仍然走完整的 HTTP 链路（server → GeoThermoAI_Assistant → requests →
网关 → 候选理解器 → 台账），而不是在进程内打桩。

它按用户消息里的关键词挑一份预置候选 JSON 返回；返回内容会被端到端脚本
原样记录为「模型原始输出」。换成真实模型时，把 api_base_url 指回真实网关
即可用同一份脚本复跑，无需改动被测代码。

独立运行（供人工排查）：
    python3 tests/stub_model_server.py --port 18000
"""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# ── 场景脚本：(消息匹配正则, 候选 JSON) ───────────────────────────
# 这些 JSON 就是「模型允许输出的候选操作」格式（docs/候选操作格式说明.md）


def _ops(operations: List[dict], shared: Optional[List[dict]] = None,
         note: str = "") -> str:
    return json.dumps({"operations": operations,
                       "shared_modifiers": shared or [],
                       "note": note}, ensure_ascii=False)


SCRIPT: List[Tuple[str, str]] = [
    # 验收① 一句话两个任务，共享年份显式写出作用范围
    (r"武汉.*7\s*月.*南京.*8\s*月", _ops(
        [
            {"op": "create", "label": "A", "target_ref": "武汉",
             "capability": "full_lst",
             "patches": {
                 "region": {"action": "set", "value": "武汉",
                            "evidence": "武汉 7 月"},
                 "time": {"action": "set", "value": "7 月",
                          "evidence": "武汉 7 月"},
                 "product_mode": {"action": "set", "value": "monthly",
                                  "evidence": "月度产品"}}},
            {"op": "create", "label": "B", "target_ref": "南京",
             "capability": "full_lst",
             "patches": {
                 "region": {"action": "set", "value": "南京",
                            "evidence": "南京 8 月"},
                 "time": {"action": "set", "value": "8 月",
                          "evidence": "南京 8 月"},
                 "product_mode": {"action": "set", "value": "monthly",
                                  "evidence": "月度产品"}}},
        ],
        shared=[{"patches": {"time": {"action": "set", "value": "2025 年"}},
                 "applies_to": ["A", "B"], "evidence": "都是 2025 年"}],
        note="两个地区两个月份，年份共享")),

    # 验收② 否定
    (r"不是武汉", _ops(
        [{"op": "clear", "target_ref": "武汉",
          "patches": {"region": {"action": "clear", "value": "武汉",
                                 "evidence": "不是武汉"}}}],
        note="用户否定了武汉")),

    # 否定之后接着说别的（不再提地区）
    (r"那就.*8\s*月", _ops(
        [{"op": "set",
          "patches": {"time": {"action": "set", "value": "2025 年 8 月",
                               "evidence": "2025 年 8 月"}}}],
        note="只补时间，没有提到地区")),

    # 验收③ 相对年份
    (r"去年\s*7\s*月|去年七月", _ops(
        [{"op": "create", "label": "A", "target_ref": "武汉",
          "capability": "full_lst",
          "patches": {
              "region": {"action": "set", "value": "武汉", "evidence": "武汉"},
              "time": {"action": "set", "value": "去年 7 月",
                       "evidence": "去年 7 月"},
              "product_mode": {"action": "set", "value": "pair",
                               "evidence": "配对模式"}}}],
        note="相对年份原样交给程序按锚点解析")),

    # 验收④ 只下载
    (r"只下载", _ops(
        [{"op": "create", "label": "A", "capability": "download_subset",
          "patches": {
              "region": {"action": "set", "value": "武汉", "evidence": "武汉"},
              "time": {"action": "set", "value": "2025 年 7 月",
                       "evidence": "2025 年 7 月"},
              "datasets": {"action": "set", "value": ["sentinel2"],
                           "evidence": "只下载 Sentinel-2"}}}],
        note="只要下载 Sentinel-2，不训练")),

    # 明确年月的完整生产（验收②的第一轮：先建起一个武汉任务再否定它）
    (r"(武汉|南京).*\d{4}\s*年\s*\d{1,2}\s*月.*(LST|地表温度)", _ops(
        [{"op": "create", "label": "A", "target_ref": "武汉",
          "capability": "full_lst",
          "patches": {
              "region": {"action": "set", "value": "武汉", "evidence": "武汉"},
              "time": {"action": "set", "value": "2025 年 7 月",
                       "evidence": "2025 年 7 月"},
              "product_mode": {"action": "set", "value": "pair",
                               "evidence": "配对模式"}}}],
        note="明确年月的完整生产")),

    # 验收⑤ 纯知识问答
    (r"什么是地表温度|地表温度是什么", _ops(
        [{"op": "reply_only", "evidence": "什么是地表温度"}],
        note="知识问答，不建任务")),

    # 追问场景：只说地区不说时间
    (r"^(?!.*(月|日|年)).*(武汉|南京).*(地表温度|温度图|LST)", _ops(
        [{"op": "create", "label": "A", "capability": "full_lst",
          "patches": {"region": {"action": "set", "value": "武汉",
                                 "evidence": "武汉"}}}],
        note="只给了地区，时间缺失")),

    # 回答追问
    (r"^\s*2025\s*年\s*7\s*月\s*$", _ops(
        [{"op": "answer", "answer_text": "2025 年 7 月"}],
        note="回答时间")),
    (r"^\s*配对模式\s*$", _ops(
        [{"op": "answer", "answer_text": "配对模式"}],
        note="回答产品方式")),
]

_FALLBACK = _ops([{"op": "reply_only"}], note="没有匹配到脚本，按只回答处理")


def pick_reply(messages: List[Dict[str, Any]]) -> str:
    user = ""
    for item in reversed(messages or []):
        if item.get("role") == "user":
            user = str(item.get("content") or "")
            break
    for pattern, body in SCRIPT:
        if re.search(pattern, user):
            return body
    return _FALLBACK


_RECORD_PATH = ""
_RECORD_LOCK = threading.Lock()


def _record(user_message: str, content: str) -> None:
    """把真实经 HTTP 返回的模型输出逐条留档（验收交付物：模型原始输出）。"""
    if not _RECORD_PATH:
        return
    line = json.dumps({"user_message": user_message, "model_output": content},
                      ensure_ascii=False)
    with _RECORD_LOCK:
        with open(_RECORD_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):   # 静音访问日志
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        content = pick_reply(messages)
        _record(next((str(m.get("content") or "") for m in reversed(messages)
                      if m.get("role") == "user"), ""), content)
        body = json.dumps({
            "id": "scripted", "object": "chat.completion",
            "model": payload.get("model") or "scripted",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int, record_path: str = "") -> ThreadingHTTPServer:
    global _RECORD_PATH
    _RECORD_PATH = record_path
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="stub-model").start()
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--record", default="",
                        help="把每次请求与返回的模型原始输出追加到该 JSONL 文件")
    args = parser.parse_args()
    server = serve(args.port, args.record)
    print(f"脚本化模型网关已启动：http://127.0.0.1:{args.port}/chat/completions")
    try:
        threading.Event().wait()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
