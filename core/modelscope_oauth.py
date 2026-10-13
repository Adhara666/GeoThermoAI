# -*- coding: utf-8 -*-
"""ModelScope（魔搭）OAuth 2.0 / OIDC 登录支持。

授权码模式流程：
  1. 前端跳转 https://www.modelscope.cn/oauth/authorize?client_id=..&redirect_uri=..
     &response_type=code&scope=openid profile&state=..
  2. 授权后魔搭回调 redirect_uri?code=..&state=..
  3. 后端 POST /oauth/token 用 code 换 access_token
  4. GET /oauth/userinfo（OIDC）或 /openapi/v1/users/me 取用户信息
  5. 建/更本地账号；access_token 直接用于魔搭推理服务（OpenAI 兼容），
     自动写入该用户的 LLM API 配置

配置（优先环境变量，便于创空间部署）：
  MODELSCOPE_OAUTH_CLIENT_ID / MODELSCOPE_OAUTH_CLIENT_SECRET
  或 config/settings.json 的 modelscope_oauth 段
"""

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.modelscope.cn/oauth/authorize"
TOKEN_URL = "https://www.modelscope.cn/oauth/token"
USERINFO_URL = "https://www.modelscope.cn/oauth/userinfo"
OPENAPI_ME_URL = "https://modelscope.cn/openapi/v1/users/me"
MODELS_URL = "https://api-inference.modelscope.cn/v1/models"

ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_PATH = ROOT / "config" / "settings.json"

# 状态防伪：state → (redirect_uri, 生成时间)，5 分钟有效，一次性消费
_STATE_TTL = 300
_states = {}
_states_lock = threading.Lock()


def oauth_config() -> dict:
    """魔搭 OAuth 应用凭据（env 优先，其次 config/settings.json）。"""
    cid = os.environ.get("MODELSCOPE_OAUTH_CLIENT_ID", "").strip()
    sec = os.environ.get("MODELSCOPE_OAUTH_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
            seg = data.get("modelscope_oauth") or {}
            cid = cid or str(seg.get("client_id") or "").strip()
            sec = sec or str(seg.get("client_secret") or "").strip()
        except Exception:  # noqa: BLE001 — 配置缺失按未配置处理
            pass
    return {"client_id": cid, "client_secret": sec,
            "configured": bool(cid and sec)}


def issue_state(redirect_uri: str) -> str:
    """签发一次性 state 并登记其回调地址（防伪造回调）。"""
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _states_lock:
        for k in [k for k, v in _states.items() if now - v[1] > _STATE_TTL]:
            _states.pop(k, None)
        _states[state] = (redirect_uri, now)
    return state


def consume_state(state: str):
    """一次性消费 state；返回登记的 redirect_uri（无效/过期/重放 → None）。"""
    now = time.time()
    with _states_lock:
        item = _states.pop(state or "", None)
    if not item:
        return None
    uri, ts = item
    if now - ts > _STATE_TTL:
        return None
    return uri


def build_authorize_url(redirect_uri: str, state: str) -> str:
    cfg = oauth_config()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # api-inference：授权令牌用于调用魔搭推理服务（LLM 调用必需）
        "scope": "openid profile api-inference",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urlencode(params)


def exchange_code(code: str, redirect_uri: str) -> str:
    """授权码换 access_token。"""
    cfg = oauth_config()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise ValueError(f"未返回 access_token：{str(data)[:200]}")
    return token


def fetch_userinfo(access_token: str) -> dict:
    """取魔搭用户信息：优先 OpenAPI（字段更全），降级 OIDC userinfo。"""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(OPENAPI_ME_URL, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = (resp.json() or {}).get("data") or {}
            if data.get("username"):
                return {"username": str(data["username"]),
                        "nickname": str(data.get("nickname") or data["username"]),
                        "avatar": str(data.get("avatar_url") or "")}
    except Exception as e:  # noqa: BLE001
        logger.warning("[ms-oauth] OpenAPI 用户信息失败，改用 OIDC: %s", e)
    resp = requests.get(USERINFO_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    info = resp.json() or {}
    name = str(info.get("preferred_username") or info.get("name") or info.get("sub") or "")
    if not name:
        raise ValueError("魔搭未返回可用的用户名")
    return {"username": name,
            "nickname": str(info.get("name") or name),
            "avatar": str(info.get("picture") or "")}


# 推理模型选择：优先查询魔搭可用列表并挑 DeepSeek 系；失败用固定候选链
_PREFERRED_HINTS = ("deepseek-v4", "deepseek-v3", "deepseek-r1")
_MODEL_FALLBACKS = ("deepseek-ai/DeepSeek-V3.2", "deepseek-ai/DeepSeek-R1-0528",
                    "deepseek-ai/DeepSeek-V3")


def pick_inference_model(access_token: str) -> str:
    try:
        resp = requests.get(MODELS_URL,
                            headers={"Authorization": f"Bearer {access_token}"},
                            timeout=20)
        if resp.status_code == 200:
            items = (resp.json() or {}).get("data") or []
            ids = [str(i.get("id") or "") for i in items if isinstance(i, dict)]
            ds = [i for i in ids if i.lower().startswith("deepseek")]
            for hint in _PREFERRED_HINTS:
                for i in ds:
                    if hint in i.lower():
                        return i
            if ds:
                return ds[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("[ms-oauth] 模型列表获取失败，使用兜底候选: %s", e)
    return _MODEL_FALLBACKS[0]


def build_api_config(access_token: str) -> dict:
    """组装要写入用户设置的 api 段（魔搭推理服务，OpenAI 兼容）。"""
    model_id = pick_inference_model(access_token)
    return {
        "api_format": "openai",
        "api_base_url": "https://api-inference.modelscope.cn/v1",
        "model_id": model_id,
        "api_key": access_token,
        "model_type": "modelscope",
        "display_name": f"ModelScope · {model_id}",
        "model_series": "default",
    }
