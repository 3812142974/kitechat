"""Shared server config: paths, hashing, tokens, network helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import socket
import sys
from urllib.parse import urlparse

from . import db as dbmod

APP_NAME = "KiteChat"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录
DATA_DIR = os.path.join(ROOT, "data")
EXPORT_DIR = os.path.join(ROOT, "exports")
CLIENT_WEB_DIR = os.path.join(ROOT, "client", "web")
DESKTOP_DIR = os.path.join(ROOT, "client", "desktop")
ANDROID_DIR = os.path.join(ROOT, "client", "android")

# client version — keep in sync with client/android/app/build.gradle versionName
VERSION = "1.6.0"


def ensure_dirs() -> None:
    for d in (DATA_DIR, EXPORT_DIR):
        os.makedirs(d, exist_ok=True)


def db_path() -> str:
    return os.path.join(DATA_DIR, "kitechat.db")


# ---------------------------------------------------------------- auth
def hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 120_000
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 120_000
    ).hex()
    return hmac.compare_digest(check, digest)


def new_token() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------- network
def lan_ip() -> str:
    """Best-effort LAN IPv4 of this machine."""
    for target in ("192.255.255.255", "10.255.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return "127.0.0.1"


def public_ws_address() -> str:
    """Configured ws_address if set, otherwise auto ws://<lan_ip>:<port>."""
    cfg = dbmod.get_db().get_config("ws_address").strip()
    if cfg:
        return cfg
    port = dbmod.get_db().get_config("port") or "8920"
    return f"ws://{lan_ip()}:{port}/ws"


def normalize_ws_url(url: str) -> str:
    """Ensure the address has a scheme and path so clients can connect."""
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "ws://" + url
    p = urlparse(url)
    if not p.path or p.path == "/":
        url = url.rstrip("/") + "/ws"
    return url


def validate_ws_url(url: str) -> tuple[bool, str]:
    if not url.strip():
        return False, "WS 地址不能为空"
    p = urlparse(url if "://" in url else "ws://" + url)
    if p.scheme not in ("ws", "wss"):
        return False, "协议必须是 ws:// 或 wss://"
    if not p.hostname:
        return False, "缺少主机地址"
    if not re.fullmatch(r"[a-zA-Z0-9._\-\[\]:]+", p.hostname):
        return False, "主机地址含非法字符"
    try:
        port = p.port or (443 if p.scheme == "wss" else 80)
        if not (1 <= port <= 65535):
            return False, "端口超出范围"
    except ValueError:
        return False, "端口无效"
    return True, ""


def is_windows() -> bool:
    return sys.platform.startswith("win")


def b64e(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
