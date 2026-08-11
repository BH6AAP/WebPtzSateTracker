"""
用户认证模块: 用户存储 (users.json) + 密码哈希 + 会话管理
- 密码使用 scrypt 加盐哈希
- 会话基于 Flask session (签名 cookie)
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading

from functools import wraps

from flask import jsonify, session

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_FILE = os.path.join(BASE_DIR, "users.json")

_lock = threading.Lock()
# 内存用户表: username -> {"password_hash", "salt", "role"}
_users: dict = {}


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """scrypt 加盐哈希, 返回 (十六进制摘要, salt)"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"), n=2 ** 14, r=8, p=1
    )
    return digest.hex(), salt


def _load_users() -> None:
    global _users
    try:
        if os.path.exists(USER_FILE):
            with open(USER_FILE, "r", encoding="utf-8") as f:
                _users = json.load(f)
    except Exception:  # noqa: BLE001
        _users = {}


def _save_users() -> None:
    with open(USER_FILE, "w", encoding="utf-8") as f:
        json.dump(_users, f, ensure_ascii=False, indent=2)


def create_user(username: str, password: str, role: str = "user") -> bool:
    """创建用户, 已存在返回 False"""
    with _lock:
        if username in _users:
            return False
        digest, salt = _hash_password(password)
        _users[username] = {"password_hash": digest, "salt": salt, "role": role}
        _save_users()
        return True


def change_password(username: str, old_password: str, new_password: str) -> bool:
    """修改密码: 校验旧密码后设置新密码"""
    with _lock:
        u = _users.get(username)
        if not u:
            return False
        digest, _ = _hash_password(old_password, u["salt"])
        if not secrets.compare_digest(digest, u["password_hash"]):
            return False
        new_digest, new_salt = _hash_password(new_password)
        u["password_hash"] = new_digest
        u["salt"] = new_salt
        _save_users()
        return True


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名/密码, 成功返回用户信息, 失败返回 None"""
    with _lock:
        u = _users.get(username)
        if not u:
            return None
        digest, _ = _hash_password(password, u["salt"])
        if secrets.compare_digest(digest, u["password_hash"]):
            return {"username": username, "role": u["role"]}
        return None


def current_user() -> dict | None:
    """当前会话用户"""
    return session.get("user")


def login_required(f):
    """视图装饰器: 未登录返回 401"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        return f(*args, **kwargs)
    return wrapper


_load_users()
