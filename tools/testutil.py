"""Shared helpers for KiteChat test scripts."""
from __future__ import annotations

import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "kitechat.db")


def admin_token() -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT value FROM config WHERE key='admin_token'").fetchone()
    conn.close()
    return row[0]


def cleanup_users(usernames: list[str]) -> int:
    """Delete the given test users and ALL data they own:
    AI sessions, messages inside them, friend relations, codes.

    Test scripts MUST call this (in a finally block) so no test accounts
    survive a run. Returns number of users removed.
    """
    if not usernames:
        return 0
    conn = sqlite3.connect(DB_PATH)
    removed = 0
    for name in usernames:
        row = conn.execute(
            "SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if row is None:
            continue
        uid = row[0]
        # sessions created by this user (AI sessions)
        sess_ids = [r[0] for r in conn.execute(
            "SELECT id FROM sessions WHERE owner_id=?", (uid,))]
        for sid in sess_ids:
            conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM session_members WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        # any remaining memberships (e.g. direct sessions)
        conn.execute("DELETE FROM session_members WHERE user_id=?", (uid,))
        # messages sent by the user in other sessions (direct chats)
        conn.execute("DELETE FROM messages WHERE sender_id=?", (uid,))
        # friend relations
        conn.execute(
            "DELETE FROM friends WHERE user_id=? OR friend_id=?", (uid, uid))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        # drop any login tokens that pointed at this user (prevents orphan
        # tokens that previously crashed the WS auth handler)
        conn.execute("DELETE FROM config WHERE key LIKE 'token:%' AND value=?",
                     (str(uid),))
        removed += 1
    # prune orphan sessions (all members deleted, e.g. direct chats)
    orphans = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE id NOT IN "
        "(SELECT session_id FROM session_members)")]
    for sid in orphans:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    print(f"[cleanup] 已删除 {removed} 个测试用户及其数据")
    return removed
