# -*- coding: utf-8 -*-
"""
用户账号模块：bcrypt 密码哈希 + JWT 会话 + 用户注册表

数据布局：
  data/users/index.json        ← 用户注册表 [{uid, username, nickname, password_hash, created_at}]
  data/users/{uid}/            ← 每个用户的数据根目录（对话/项目/研究区/设置/记忆）
  data/.jwt_secret             ← JWT 签名密钥（首次自动生成）

设计约定：
  - 注册只需 账号名 + 密码（昵称可选）；账号名 ASCII 安全，直接作为 uid / 目录名
  - 密码至少 6 位，允许 ASCII 33-126 可打印字符（含 * 等符号）
  - 不提供密码找回：账号仅用于多人并发隔离，遗忘由管理员重建
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

import bcrypt
import jwt

_ROOT = Path(__file__).resolve().parent.parent

_USERS_ROOT = _ROOT / "data" / "users"
_INDEX_PATH = _USERS_ROOT / "index.json"
_SECRET_PATH = _ROOT / "data" / ".jwt_secret"

# 账号名：2-32 位，仅字母/数字/_/-
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")

# 密码：至少 6 位，仅 ASCII 33-126 可打印字符
PASSWORD_MIN_LEN = 6


def _valid_password(password: str) -> bool:
    if len(password or "") < PASSWORD_MIN_LEN:
        return False
    return all(33 <= ord(ch) <= 126 for ch in password)


def _ensure_dirs():
    _USERS_ROOT.mkdir(parents=True, exist_ok=True)


def _jwt_secret() -> str:
    # 多实例部署时优先用环境变量固定 secret（各实例一致，避免 A 签发 B 验证失败）
    env_secret = os.environ.get("GTAI_JWT_SECRET", "").strip()
    if env_secret:
        return env_secret
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_text(encoding="utf-8").strip()
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SECRET_PATH.write_text(secret, encoding="utf-8")
    try:
        os.chmod(_SECRET_PATH, 0o600)
    except Exception:
        pass
    return secret


# ── 密码 ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


# ── JWT ──────────────────────────────────────────────────────────

TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 天


def create_token(uid: str, username: str) -> str:
    payload = {
        "sub": uid,
        "username": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_token(token: str):
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except Exception:
        return None


# ── 用户注册表 ───────────────────────────────────────────────────

def load_users() -> list:
    _ensure_dirs()
    if not _INDEX_PATH.exists():
        return []
    try:
        return json.loads(_INDEX_PATH.read_text(encoding="utf-8")).get("users", [])
    except Exception:
        return []


def _save_users(users: list):
    _ensure_dirs()
    _INDEX_PATH.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")


def find_by_username(username: str):
    for u in load_users():
        if u["username"] == username:
            return u
    return None


def find_by_uid(uid: str):
    for u in load_users():
        if u["uid"] == uid:
            return u
    return None


def public_user(user: dict) -> dict:
    """去掉 password_hash 的用户信息"""
    return {
        "uid": user["uid"],
        "username": user["username"],
        "nickname": user.get("nickname", "") or user["username"],
    }


# ── 注册 / 登录 ──────────────────────────────────────────────────

def register_user(username: str, password: str, nickname: str = "") -> dict:
    """注册用户；返回 {ok, message, user?}"""
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        return {"ok": False, "message": "账号名仅允许 2-32 位字母/数字/_/-"}
    if not _valid_password(password or ""):
        return {"ok": False, "message": f"密码至少 {PASSWORD_MIN_LEN} 位，仅允许英文字母/数字/符号"}
    if find_by_username(username):
        return {"ok": False, "message": "账号名已存在"}
    users = load_users()
    user = {
        "uid": username,  # 账号名 ASCII 安全，直接作为 uid / 目录名
        "username": username,
        "nickname": (nickname or "").strip() or username,
        "password_hash": hash_password(password),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    users.append(user)
    _save_users(users)
    (_USERS_ROOT / user["uid"]).mkdir(parents=True, exist_ok=True)
    return {"ok": True, "message": f"账号「{username}」注册成功", "user": public_user(user)}


def authenticate(username: str, password: str):
    user = find_by_username((username or "").strip())
    if not user:
        return None
    if not verify_password(password or "", user.get("password_hash", "")):
        return None
    return user
