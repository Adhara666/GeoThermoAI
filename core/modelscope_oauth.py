# -*- coding: utf-8 -*-
"""ModelScope（魔搭）OAuth 2.0 / OIDC 登录支持。

支持中国站（modelscope.cn）与国际站（modelscope.ai）两套 OAuth / 推理域名：
两个站点是独立的账号与 OAuth 应用体系，client_id / client_secret 不能混用。

授权码模式流程：
  1. 前端跳转 {site}/oauth/authorize?client_id=..&redirect_uri=..
     &response_type=code&scope=openid profile api-inference&state=..
  2. 授权后魔搭回调 redirect_uri?code=..&state=..
  3. 后端 POST {site}/oauth/token 用 code 换 access_token
  4. GET {site}/oauth/userinfo（OIDC）或 {site}/openapi/v1/users/me 取用户信息
  5. 建/更本地账号；access_token 直接用于魔搭推理服务（OpenAI 兼容），
     自动写入该用户的 LLM API 配置

凭据来源（优先级从高到低）：
  1. 手动注入：MODELSCOPE_OAUTH_CLIENT_ID / MODELSCOPE_OAUTH_CLIENT_SECRET
     （或 config/settings.json 的 modelscope_oauth 段）
  2. 创空间内置 OAuth：在 Studio「Settings → Adjust deployment settings」中开启
     OAuth settings 后，平台自动创建应用并注入
     OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OPENID_PROVIDER_URL /
     STUDIO_HOST / OAUTH_SCOPES，无需手动填任何凭据

站点选择（优先级从高到低）：
  MODELSCOPE_OAUTH_SITE 环境变量（cn / ai）
  → 内置 OAuth 的 OPENID_PROVIDER_URL 域名（www.modelscope.ai → ai）
  → 前端按当前页面域名传入的 site（modelscope.ai → ai）
  → config/settings.json 的 modelscope_oauth.site
  → 默认 cn
"""

import base64
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

_SITE_PROFILES = {
    "cn": {
        "authorize_url": "https://www.modelscope.cn/oauth/authorize",
        "token_url": "https://www.modelscope.cn/oauth/token",
        "userinfo_url": "https://www.modelscope.cn/oauth/userinfo",
        "openapi_me_url": "https://modelscope.cn/openapi/v1/users/me",
        "models_url": "https://api-inference.modelscope.cn/v1/models",
        "api_base_url": "https://api-inference.modelscope.cn/v1",
    },
    "ai": {
        "authorize_url": "https://www.modelscope.ai/oauth/authorize",
        "token_url": "https://www.modelscope.ai/oauth/token",
        "userinfo_url": "https://www.modelscope.ai/oauth/userinfo",
        "openapi_me_url": "https://www.modelscope.ai/openapi/v1/users/me",
        "models_url": "https://api-inference.modelscope.ai/v1/models",
        "api_base_url": "https://api-inference.modelscope.ai/v1",
    },
}

ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_PATH = ROOT / "config" / "settings.json"

# 状态防伪：state → {redirect_uri, site, ts}，5 分钟有效，一次性消费
_STATE_TTL = 300
_states = {}
_states_lock = threading.Lock()


def normalize_site(site: str) -> str:
    """把各种写法（含 URL）归一到 cn / ai；无法识别时返回 cn。"""
    v = str(site or "").strip().lower()
    if v in ("ai", "intl", "international") or "modelscope.ai" in v:
        return "ai"
    return "cn"


def _settings_segment() -> dict:
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        return data.get("modelscope_oauth") or {}
    except Exception:  # noqa: BLE001 — 配置缺失按空处理
        return {}


def oauth_config() -> dict:
    """魔搭 OAuth 应用凭据。

    手动凭据（env / 配置文件）优先；否则使用创空间内置 OAuth 注入的
    OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET（platform=True）。
    """
    cid = os.environ.get("MODELSCOPE_OAUTH_CLIENT_ID", "").strip()
    sec = os.environ.get("MODELSCOPE_OAUTH_CLIENT_SECRET", "").strip()
    platform = False
    if not (cid and sec):
        pcid = os.environ.get("OAUTH_CLIENT_ID", "").strip()
        psec = os.environ.get("OAUTH_CLIENT_SECRET", "").strip()
        if pcid and psec:
            cid, sec, platform = pcid, psec, True
    if not (cid and sec):
        seg = _settings_segment()
        cid = cid or str(seg.get("client_id") or "").strip()
        sec = sec or str(seg.get("client_secret") or "").strip()
    return {"client_id": cid, "client_secret": sec,
            "configured": bool(cid and sec),
            "platform": platform,
            "provider_url": os.environ.get("OPENID_PROVIDER_URL", "").strip(),
            "studio_host": os.environ.get("STUDIO_HOST", "").strip()}


def resolve_site(site: str = "") -> str:
    env = os.environ.get("MODELSCOPE_OAUTH_SITE", "").strip()
    if env:
        return normalize_site(env)
    cfg = oauth_config()
    if cfg["platform"] and cfg["provider_url"]:
        # 内置 OAuth：以平台注入的 OIDC provider 域名作为权威站点
        return normalize_site(cfg["provider_url"])
    if str(site or "").strip():
        return normalize_site(site)
    return normalize_site(_settings_segment().get("site") or "cn")


def site_profile(site: str = "") -> dict:
    profile = dict(_SITE_PROFILES[resolve_site(site)])
    cfg = oauth_config()
    if cfg["platform"] and cfg["provider_url"]:
        # 用平台注入的 provider 地址覆盖端点（兼容未来域名调整）。
        # 平台可能注入 http://，强制升级 https，避免令牌在 http 虚拟主机上失效。
        base = cfg["provider_url"].rstrip("/")
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        profile.update({
            "authorize_url": base + "/oauth/authorize",
            "token_url": base + "/oauth/token",
            "userinfo_url": base + "/oauth/userinfo",
            "openapi_me_url": base + "/openapi/v1/users/me",
        })
    return profile


def effective_redirect_uri(frontend_uri: str, cfg: dict = None) -> str:
    """确定回调地址。

    内置 OAuth 时平台预登记的是创空间 host 根地址
    （OAuthCallbackUrls 即 https://{STUDIO_HOST}/），必须与登记值完全一致；
    手动凭据时沿用前端计算/用户登记的回调地址。
    """
    cfg = cfg or oauth_config()
    host = str(cfg.get("studio_host") or "").strip()
    if cfg.get("platform") and host:
        return f"https://{host}/"
    return frontend_uri


def issue_state(redirect_uri: str, site: str = "") -> str:
    """签发一次性 state 并登记其回调地址与站点（防伪造回调/跨站混用）。"""
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _states_lock:
        for k in [k for k, v in _states.items() if now - v["ts"] > _STATE_TTL]:
            _states.pop(k, None)
        _states[state] = {"redirect_uri": redirect_uri,
                          "site": resolve_site(site), "ts": now}
    return state


def consume_state(state: str):
    """一次性消费 state；返回 {redirect_uri, site}（无效/过期/重放 → None）。"""
    now = time.time()
    with _states_lock:
        item = _states.pop(state or "", None)
    if not item:
        return None
    if now - item["ts"] > _STATE_TTL:
        return None
    return {"redirect_uri": item["redirect_uri"], "site": item["site"]}


def build_authorize_url(redirect_uri: str, state: str, site: str = "") -> str:
    cfg = oauth_config()
    # api-inference：授权令牌用于调用魔搭推理服务（LLM 调用必需）。
    # 创空间内置 OAuth 时以平台注入的 OAUTH_SCOPES 为准（须在 Studio
    # OAuth settings 中勾选 api-inference，否则推理调用会 401）。
    scope = "openid profile api-inference"
    if cfg["platform"]:
        scope = os.environ.get("OAUTH_SCOPES", "").strip() or scope
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return site_profile(site)["authorize_url"] + "?" + urlencode(params)


def exchange_code_tokens(code: str, redirect_uri: str, site: str = "") -> dict:
    """授权码换令牌，返回 access_token / id_token / scope。"""
    cfg = oauth_config()
    resp = requests.post(site_profile(site)["token_url"], data={
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
    # 仅记录 scope 与字段名，绝不打印令牌本身
    logger.info("[ms-oauth] token scope=%s keys=%s",
                data.get("scope"), sorted(k for k in data if k != "access_token"))
    return {"access_token": token,
            "id_token": str(data.get("id_token") or ""),
            "scope": str(data.get("scope") or "")}


def exchange_code(code: str, redirect_uri: str, site: str = "") -> str:
    """授权码换 access_token。"""
    return exchange_code_tokens(code, redirect_uri, site)["access_token"]


def _decode_id_token(id_token: str) -> dict:
    """不验签解析 id_token 载荷：令牌由 token 端点经 TLS 直接返回，仅用于取身份字段。"""
    try:
        part = str(id_token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:  # noqa: BLE001
        return {}


def fetch_userinfo(access_token: str, site: str = "", id_token: str = "") -> dict:
    """取魔搭用户信息。

    顺序：OpenAPI（字段全）→ id_token 声明（无需再请求）→ OIDC userinfo。
    内置 OAuth 若未授权 profile，userinfo 可能返回 401，此时用 id_token
    里的 sub/preferred_username 兜底，保证登录流程不中断。
    """
    profile = site_profile(site)
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(profile["openapi_me_url"], headers=headers, timeout=30)
        if resp.status_code == 200:
            data = (resp.json() or {}).get("data") or {}
            if data.get("username"):
                return {"username": str(data["username"]),
                        "nickname": str(data.get("nickname") or data["username"]),
                        "avatar": str(data.get("avatar_url") or "")}
        else:
            logger.warning("[ms-oauth] OpenAPI 用户信息返回 %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ms-oauth] OpenAPI 用户信息失败，改用 OIDC: %s", e)

    claims = _decode_id_token(id_token)
    name = str(claims.get("preferred_username") or claims.get("name")
               or claims.get("sub") or "")
    if name:
        return {"username": name,
                "nickname": str(claims.get("name") or name),
                "avatar": str(claims.get("picture") or "")}

    try:
        resp = requests.get(profile["userinfo_url"], headers=headers, timeout=30)
        if resp.status_code == 200:
            info = resp.json() or {}
            name = str(info.get("preferred_username") or info.get("name")
                       or info.get("sub") or "")
            if name:
                return {"username": name,
                        "nickname": str(info.get("name") or name),
                        "avatar": str(info.get("picture") or "")}
        logger.warning("[ms-oauth] OIDC userinfo 返回 %s（请确认已授权 openid/profile）",
                       resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ms-oauth] OIDC userinfo 请求失败: %s", e)
    raise ValueError("魔搭未返回可用的用户名（请确认 OAuth 已勾选 openid 与 profile）")


# 推理模型选择：优先 DeepSeek Flash（用户默认），再按版本降级。
# 注意国际站列表里没有 V4-Flash-0731，且 Pro 排在最前，不能只按顺序取。
_PREFERRED_HINTS = ("deepseek-v4", "deepseek-v3", "deepseek-r1")
_MODEL_FALLBACKS = ("deepseek-ai/DeepSeek-V3.2", "deepseek-ai/DeepSeek-R1-0528",
                    "deepseek-ai/DeepSeek-V3")


def _choose_model(ids: list) -> str:
    ds = [i for i in ids if i and i.lower().startswith("deepseek")]
    flash = [i for i in ds if "flash" in i.lower()]
    if flash:
        return flash[0]
    for hint in _PREFERRED_HINTS:
        for i in ds:
            if hint in i.lower():
                return i
    return ds[0] if ds else ""


def _inference_candidates(site: str = "") -> list:
    """推理服务候选地址：本站优先，另一站兜底。

    国际站文档里 api-inference 的示例仍写作 modelscope.cn，实际两个域名
    都可能可用，因此登录时用 token 探测一次，择可用者写入用户配置。
    """
    primary = site_profile(site)["api_base_url"]
    other = ("https://api-inference.modelscope.cn/v1"
             if primary.startswith("https://api-inference.modelscope.ai")
             else "https://api-inference.modelscope.ai/v1")
    return [primary, other]


def pick_inference(access_token: str, site: str = "") -> tuple:
    """返回 (model_id, api_base_url)。"""
    candidates = _inference_candidates(site)
    for base in candidates:
        try:
            resp = requests.get(base + "/models",
                                headers={"Authorization": f"Bearer {access_token}"},
                                timeout=20)
            if resp.status_code == 200:
                items = (resp.json() or {}).get("data") or []
                ids = [str(i.get("id") or "") for i in items if isinstance(i, dict)]
                return (_choose_model(ids) or _MODEL_FALLBACKS[0], base)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ms-oauth] 推理地址 %s 不可用: %s", base, e)
    return _MODEL_FALLBACKS[0], candidates[0]


def build_api_config(access_token: str, site: str = "") -> dict:
    """组装要写入用户设置的 api 段（魔搭推理服务，OpenAI 兼容）。"""
    model_id, api_base = pick_inference(access_token, site)
    return {
        "api_format": "openai",
        "api_base_url": api_base,
        "model_id": model_id,
        "api_key": access_token,
        "model_type": "modelscope",
        "display_name": f"ModelScope · {model_id}",
        "model_series": "default",
    }
