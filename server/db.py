"""SQLite storage layer (stdlib sqlite3, WAL mode, thread-confined).

Tables:
  users        account records (username/password hash/email/avatar/online)
  codes        email verification codes (register/reset)
  sessions     chat conversations (ai / private direct message)
  session_members  user <-> session membership
  messages     all messages, OneBot V11 message-segment JSON content
  friends      friend relationships + requests
  config       server settings (ws address, smtp, bot endpoint, tokens)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email TEXT NOT NULL,
    avatar_color TEXT DEFAULT '#7C6CF0',
    signature TEXT DEFAULT '',
    virtual_qq INTEGER,
    created_at REAL NOT NULL,
    last_seen REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,            -- register | reset
    target TEXT NOT NULL,             -- email or username
    code TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    expires_at REAL NOT NULL,
    used INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,              -- uuid
    kind TEXT NOT NULL,               -- ai | direct
    name TEXT DEFAULT '',
    owner_id INTEGER,                 -- creator user id (ai sessions)
    pair_key TEXT UNIQUE,             -- direct: 'minid:maxid'
    created_at REAL NOT NULL,
    last_msg_ts REAL DEFAULT 0,
    last_msg_preview TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS session_members (
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at REAL NOT NULL,
    PRIMARY KEY (session_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    sender_id INTEGER NOT NULL,       -- user id; 0 = system/bot fallback
    sender_name TEXT DEFAULT '',
    message_type TEXT NOT NULL,       -- text | image | face | record | forward | system
    content TEXT NOT NULL,            -- JSON list of OneBot V11 message segments
    raw TEXT DEFAULT '',
    onebot_message_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS friends (
    user_id INTEGER NOT NULL,
    friend_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | blocked
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, friend_id)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sent_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'register',
    sent_at REAL NOT NULL,
    bounced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sent_codes_email ON sent_codes(email);
"""

DEFAULTS: dict[str, str] = {
    "ws_address": "",            # public ws://ip:port for clients; empty = auto
    "host": "0.0.0.0",
    "port": "8920",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_ssl": "1",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_sender_name": "KiteChat",
    "tls_port": "8921",           # HTTPS/WSS twin port (camera APIs need TLS)
    "email_domain_whitelist": "",   # comma-separated domains; empty = allow all
    "imap_host": "",             # empty = derive imap.<smtp domain>
    "imap_port": "993",
    "imap_ssl": "1",
    "bot_ws_url": "",            # external OneBot V11 endpoint (e.g. NapCat ws://...)
    "app_ws_url": "",            # OneBot app reverse WS (e.g. AstrBot ws://127.0.0.1:6199/ws)
    "bot_token": "",
    "admin_token": "",
    "app_name": "KiteChat",
    "bridge_reconnect_interval": "5",   # seconds between auto reconnect attempts
    "client_reconnect_interval": "5",   # seconds between client-side auto reconnects
}

_local = threading.local()


def _now() -> float:
    return time.time()


class DB:
    """Per-thread sqlite3 connection wrapper."""

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(SCHEMA)
            for k, v in DEFAULTS.items():
                c.execute(
                    "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (k, v)
                )
            # migration: virtual numbers column (assigned #1, #2, #3, ...)
            cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
            if "virtual_qq" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN virtual_qq INTEGER")
            if "nickname" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''")
            if "avatar" not in cols:
                # avatar = filename under data/avatars/ ('' = use color initial)
                c.execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT ''")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_vqq "
                      "ON users(virtual_qq)")
            c.commit()
            self._conn = c
        return self._conn

    # ---------- config ----------
    def get_config(self, key: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM config WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            return row["value"]
        return DEFAULTS.get(key, "")

    def set_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def all_config(self) -> dict[str, str]:
        out = dict(DEFAULTS)
        for row in self.conn.execute("SELECT key, value FROM config"):
            out[row["key"]] = row["value"]
        return out

    # ---------- users ----------
    def next_virtual_qq(self) -> int:
        """Virtual numbers ("代号") start at #1 and increment per user."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(virtual_qq), 0) + 1 AS n FROM users"
        ).fetchone()
        return int(row["n"])

    def create_user(self, username: str, password_hash: str, email: str) -> int:
        vqq = self.next_virtual_qq()
        cur = self.conn.execute(
            "INSERT INTO users(username, password_hash, email, created_at, "
            "virtual_qq) VALUES(?,?,?,?,?)",
            (username, password_hash, email, _now(), vqq),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()

    def update_nickname(self, user_id: int, nickname: str) -> None:
        self.conn.execute(
            "UPDATE users SET nickname=? WHERE id=?", (nickname, user_id))
        self.conn.commit()

    def update_avatar(self, user_id: int, filename: str) -> None:
        self.conn.execute(
            "UPDATE users SET avatar=? WHERE id=?", (filename, user_id))
        self.conn.commit()

    def avatar_of(self, user_id: int) -> str:
        row = self.conn.execute(
            "SELECT avatar FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return (row["avatar"] if row else "") or ""

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()

    def get_user_by_email(self, email: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()

    def get_user_by_vqq(self, vqq: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE virtual_qq=?", (vqq,)
        ).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users ORDER BY id"
        ).fetchall()

    def delete_user(self, user_id: int) -> None:
        c = self.conn
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.execute("DELETE FROM session_members WHERE user_id=?", (user_id,))
        c.execute(
            "DELETE FROM friends WHERE user_id=? OR friend_id=?", (user_id, user_id)
        )
        c.commit()

    def touch_user(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE users SET last_seen=? WHERE id=?", (_now(), user_id)
        )
        self.conn.commit()

    # ---------- verification codes ----------
    def add_code(self, purpose: str, target: str, code: str, payload: dict,
                 ttl: int = 600) -> None:
        self.conn.execute(
            "INSERT INTO codes(purpose,target,code,payload,expires_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (purpose, target, code, json.dumps(payload, ensure_ascii=False),
             _now() + ttl, _now()),
        )
        self.conn.commit()

    def pop_code(self, purpose: str, target: str, code: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM codes WHERE purpose=? AND target=? AND code=? "
            "AND used=0 AND expires_at>? ORDER BY id DESC LIMIT 1",
            (purpose, target, code, _now()),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
        self.conn.commit()
        return json.loads(row["payload"] or "{}")

    def prune_codes(self) -> None:
        self.conn.execute(
            "DELETE FROM codes WHERE expires_at<? OR used=1", (_now() - 86400,)
        )
        self.conn.commit()

    # ---------- sessions ----------
    def create_session(self, kind: str, name: str, owner_id: int | None,
                       pair_key: str | None = None) -> str:
        sid = uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO sessions(id,kind,name,owner_id,pair_key,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (sid, kind, name, owner_id, pair_key, _now()),
        )
        if owner_id is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO session_members(session_id,user_id,joined_at)"
                " VALUES(?,?,?)",
                (sid, owner_id, _now()),
            )
        self.conn.commit()
        return sid

    def get_session(self, sid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (sid,)
        ).fetchone()

    def get_direct_session(self, pair_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE pair_key=?", (pair_key,)
        ).fetchone()

    def list_user_sessions(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.* FROM sessions s JOIN session_members m "
            "ON s.id=m.session_id WHERE m.user_id=? "
            "ORDER BY s.last_msg_ts DESC, s.created_at DESC",
            (user_id,),
        ).fetchall()

    def add_member(self, sid: str, user_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO session_members(session_id,user_id,joined_at) "
            "VALUES(?,?,?)",
            (sid, user_id, _now()),
        )
        self.conn.commit()

    def session_members(self, sid: str) -> list[int]:
        return [
            r["user_id"]
            for r in self.conn.execute(
                "SELECT user_id FROM session_members WHERE session_id=?", (sid,)
            )
        ]

    def update_session_preview(self, sid: str, preview: str, ts: float) -> None:
        self.conn.execute(
            "UPDATE sessions SET last_msg_ts=?, last_msg_preview=? WHERE id=?",
            (ts, preview[:80], sid),
        )
        self.conn.commit()

    # ---------- messages ----------
    def add_message(self, session_id: str, sender_id: int, sender_name: str,
                    message_type: str, content: list[dict], raw: str,
                    onebot_message_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(session_id,sender_id,sender_name,message_type,"
            "content,raw,onebot_message_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                session_id,
                sender_id,
                sender_name,
                message_type,
                json.dumps(content, ensure_ascii=False),
                raw,
                onebot_message_id,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_messages(self, session_id: str, limit: int = 50,
                      before_id: int | None = None) -> list[sqlite3.Row]:
        if before_id:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE session_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, before_id, limit),
            ).fetchall()
            return list(reversed(rows))
        return self.conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()[::-1]

    # ---------- friends ----------
    def friend_status(self, user_id: int, friend_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM friends WHERE user_id=? AND friend_id=?",
            (user_id, friend_id),
        ).fetchone()
        return row["status"] if row else None

    def set_friend(self, user_id: int, friend_id: int, status: str) -> None:
        self.conn.execute(
            "INSERT INTO friends(user_id,friend_id,status,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,friend_id) DO UPDATE SET status=excluded.status",
            (user_id, friend_id, status, _now()),
        )
        self.conn.commit()

    def remove_friend_pair(self, user_id: int, friend_id: int) -> None:
        c = self.conn
        c.execute(
            "DELETE FROM friends WHERE (user_id=? AND friend_id=?) "
            "OR (user_id=? AND friend_id=?)",
            (user_id, friend_id, friend_id, user_id),
        )
        c.commit()

    def list_friends(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT u.id, u.username, u.email, u.avatar_color, u.signature, "
            "u.last_seen, f.status FROM friends f JOIN users u ON u.id=f.friend_id "
            "WHERE f.user_id=? ORDER BY u.username",
            (user_id,),
        ).fetchall()

    def list_requests(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT u.id, u.username, u.avatar_color, f.created_at FROM friends f "
            "JOIN users u ON u.id=f.user_id "
            "WHERE f.friend_id=? AND f.status='pending' ORDER BY f.created_at DESC",
            (user_id,),
        ).fetchall()


_db_instance: DB | None = None
_db_lock = threading.Lock()


def init_db(path: str) -> DB:
    global _db_instance
    with _db_lock:
        _db_instance = DB(path)
        _ = _db_instance.conn  # create schema
        return _db_instance


def get_db() -> DB:
    """Return the process-wide DB handle (single event-loop thread -> safe)."""
    if _db_instance is None:
        raise RuntimeError("DB not initialised; call init_db() first")
    return _db_instance
