"""KiteChat core hub: presence, routing, friend logic, built-in AI fallback.

All client traffic flows through here. Message payloads are OneBot V11
message-segment lists end to end.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from . import db as dbmod
from . import onebot as ob
from .config import APP_NAME, DATA_DIR

log = logging.getLogger("kitechat.hub")


def avatar_url(user_row) -> str:
    """Public URL of a user's custom avatar ('' = colored initial).
    Shared by web.py and hub.py response dicts."""
    import os
    try:
        fn = user_row["avatar"]
    except (IndexError, KeyError):
        fn = ""
    if not fn:
        return ""
    uid = user_row["id"]
    try:
        ver = int(os.path.getmtime(os.path.join(DATA_DIR, "avatars", f"{uid}.jpg")))
    except OSError:
        ver = 0
    return f"/avatar/{uid}.jpg?v={ver}"

# Virtual numbers ("代号") are stored per user in users.virtual_qq and are
# assigned starting from #1 (first user = 1, second = 2, ...).
TYPING_TIMEOUT = 12.0


class Hub:
    def __init__(self) -> None:
        self.conns: dict[int, set] = {}          # user_id -> set of ClientConn
        self.bridge = None                        # BotBridge (set by web layer)
        self._typing: dict[str, float] = {}      # session_id -> ts of bot typing
        self._pending_bot: dict[int, asyncio.Future] = {}
        self.seq = 0

    # ------------------------------------------------------------- presence
    def online(self, user_id: int) -> bool:
        return bool(self.conns.get(user_id))

    def register(self, user_id: int, conn) -> bool:
        """Returns True if this is the user's first connection (came online)."""
        s = self.conns.setdefault(user_id, set())
        first = not s
        s.add(conn)
        dbmod.get_db().touch_user(user_id)
        return first

    def unregister(self, user_id: int, conn) -> bool:
        """Returns True if the user went fully offline."""
        s = self.conns.get(user_id)
        if not s:
            return False
        s.discard(conn)
        if not s:
            self.conns.pop(user_id, None)
            dbmod.get_db().touch_user(user_id)
            return True
        return False

    async def send_to_user(self, user_id: int, frame: dict) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        for conn in list(self.conns.get(user_id, ())):
            try:
                await conn.send_raw(data)
            except Exception:  # noqa: BLE001
                log.debug("send_to_user %s failed", user_id, exc_info=True)

    async def broadcast(self, frame: dict, to_users: list[int] | None = None) -> None:
        if to_users is None:
            to_users = list(self.conns.keys())
        for uid in to_users:
            await self.send_to_user(uid, frame)

    async def broadcast_presence(self, user_id: int, online: bool) -> None:
        """Notify everyone who has user_id as a friend (and the user itself)."""
        db = dbmod.get_db()
        targets = {user_id}
        for row in db.list_users():
            if row["id"] == user_id:
                continue
            if db.friend_status(row["id"], user_id) == "accepted":
                targets.add(row["id"])
        frame = {
            "post_type": "meta_event",
            "meta_event_type": "presence",
            "user_id": user_id,
            "online": online,
            "time": int(time.time()),
        }
        await self.broadcast(frame, list(targets))

    # ------------------------------------------------------------- messaging
    async def deliver_session_message(self, session_id: str, sender_id: int,
                                      sender_name: str, segments: list[dict],
                                      message_id: int | None = None,
                                      ts: float | None = None) -> dict:
        """Store + fan out a message to all session members. Returns the frame."""
        db = dbmod.get_db()
        if message_id is None:
            message_id = db.add_message(
                session_id, sender_id, sender_name, ob.classify(segments),
                segments, ob.preview(segments),
            )
        frame = {
            "post_type": "message",
            "message_type": "private",
            "session_id": session_id,
            "message_id": message_id,
            "sender": {
                "user_id": sender_id,
                "nickname": sender_name,
            },
            "message": segments,
            "raw_message": ob.preview(segments, 4000),
            "time": int(ts or time.time()),
        }
        for uid in db.session_members(session_id):
            await self.send_to_user(uid, frame)
        db.update_session_preview(session_id, ob.preview(segments), ts or time.time())
        return frame

    async def handle_user_message(self, user_id: int, session_id: str,
                                  message: Any) -> dict:
        """Client sent a chat message. Route to friend / AI backend."""
        db = dbmod.get_db()
        sess = db.get_session(session_id)
        user = db.get_user(user_id)
        if sess is None or user is None:
            return {"status": "failed", "retcode": 100,
                    "msg": "会话不存在", "wording": "会话不存在"}
        segments = ob.normalize_message(message)
        if not segments:
            return {"status": "failed", "retcode": 100,
                    "msg": "消息为空", "wording": "消息为空"}
        frame = await self.deliver_session_message(
            session_id, user_id, user["nickname"] or user["username"], segments
        )

        if sess["kind"] == "ai":
            asyncio.create_task(self._ai_reply(user_id, session_id, segments,
                                               frame["message_id"]))
        return {"status": "ok", "retcode": 0, "data": {"message_id": frame["message_id"]}}

    async def _ai_reply(self, user_id: int, session_id: str,
                        segments: list[dict], in_message_id: int = 0) -> None:
        """Route an AI-session message to AstrBot (the only AI backend).

        KiteChat has no built-in LLM / offline responder: every chat reply
        comes from the connected OneBot V11 application (AstrBot).
        """
        db = dbmod.get_db()
        user = db.get_user(user_id)
        if user is None:
            return

        # 1) OneBot APP impl mode (AstrBot): push message event to the app;
        #    the app's reply comes back via send_private_msg API call
        #    which lands in BotBridge.handle_api_call.
        if self.bridge and self.bridge.impl_connected:
            virtual_qq = user["virtual_qq"]
            self.bridge.bind_session(virtual_qq, session_id)
            pushed = await self.bridge.push_impl_message(
                virtual_qq, user["nickname"] or user["username"], segments,
                in_message_id or int(time.time()))
            if not pushed:
                await self.deliver_session_message(session_id, 0, "Kite AI", [{
                    "type": "text", "data": {"text": "（AstrBot 连接中断，消息未送达）"}}])
            return

        # 2) OneBot forward mode (bot ws endpoint, e.g. NapCat)
        if self.bridge and self.bridge.connected:
            virtual_qq = user["virtual_qq"]
            self.bridge.bind_session(virtual_qq, session_id)
            sent = await self.bridge.send_private_msg(virtual_qq, segments)
            if not sent:
                await self.deliver_session_message(session_id, 0, "Kite AI", [{
                    "type": "text", "data": {"text": "（OneBot 后端发送失败）"}}])
            return

        # 3) nothing connected — OneBot WS is down
        await self.deliver_session_message(session_id, 0, "Kite AI", [{
            "type": "text",
            "data": {"text": "WS 未连接，AI 暂时无法回复。\n\n"
                            "请在管理后台「AI / Bot 接入」检查 OneBot 应用的 "
                            "WS 连接状态。"}}])

    # ------------------------------------------------------------- friends
    async def notify_friend(self, user_id: int, frame: dict) -> None:
        await self.send_to_user(user_id, frame)

    async def add_friend_request(self, from_id: int, to_username: str) -> dict:
        db = dbmod.get_db()
        target = db.get_user_by_name(to_username)
        if target is None:
            return _err("用户不存在")
        if target["id"] == from_id:
            return _err("不能添加自己为好友")
        status = db.friend_status(from_id, target["id"])
        if status == "accepted":
            return _err("已经是好友了")
        reverse = db.friend_status(target["id"], from_id)
        if reverse == "accepted":
            db.set_friend(from_id, target["id"], "accepted")
            await self._friend_established(from_id, target["id"])
            return {"status": "ok", "retcode": 0,
                    "data": {"direct": True, "user_id": target["id"]}}
        if status == "pending":
            return _err("已发送过请求，等待对方验证")
        from_user = db.get_user(from_id)
        db.set_friend(from_id, target["id"], "pending")
        await self.send_to_user(target["id"], {
            "post_type": "request", "request_type": "friend",
            "user_id": from_id,
            "comment": f"{from_user['nickname'] or from_user['username']} 请求添加你为好友",
            "time": int(time.time()),
        })
        return {"status": "ok", "retcode": 0, "data": {"pending": True}}

    async def handle_friend_request(self, owner_id: int, from_id: int,
                                    approve: bool) -> dict:
        db = dbmod.get_db()
        if db.friend_status(from_id, owner_id) != "pending":
            return _err("请求不存在或已处理")
        if approve:
            db.set_friend(from_id, owner_id, "accepted")
            db.set_friend(owner_id, from_id, "accepted")
            await self._friend_established(from_id, owner_id)
        else:
            db.remove_friend_pair(from_id, owner_id)
            await self.send_to_user(from_id, {
                "post_type": "notice", "notice_type": "friend_rejected",
                "user_id": owner_id, "time": int(time.time()),
            })
        return {"status": "ok", "retcode": 0, "data": {"approved": approve}}

    async def _friend_established(self, a: int, b: int) -> None:
        db = dbmod.get_db()
        ua, ub = db.get_user(a), db.get_user(b)
        for uid, other in ((a, ub), (b, ua)):
            await self.send_to_user(uid, {
                "post_type": "notice", "notice_type": "friend_added",
                "user_id": other["id"], "nickname": other["nickname"] or other["username"],
                "avatar_color": other["avatar_color"],
                "avatar": avatar_url(other),
                "online": self.online(other["id"]),
                "time": int(time.time()),
            })
        await self.ensure_direct_session(a, b)

    async def ensure_direct_session(self, a: int, b: int) -> str:
        db = dbmod.get_db()
        pair = f"{min(a, b)}:{max(a, b)}"
        sess = db.get_direct_session(pair)
        if sess:
            return sess["id"]
        sid = db.create_session("direct", "", None, pair_key=pair)
        db.add_member(sid, a)
        db.add_member(sid, b)
        for uid in (a, b):
            await self.send_to_user(uid, {
                "post_type": "notice", "notice_type": "session_created",
                "session": _session_view(db.get_session(sid), uid, self),
                "time": int(time.time()),
            })
        return sid

    async def delete_friend(self, owner_id: int, friend_id: int) -> dict:
        db = dbmod.get_db()
        db.remove_friend_pair(owner_id, friend_id)
        for uid in (owner_id, friend_id):
            await self.send_to_user(uid, {
                "post_type": "notice", "notice_type": "friend_removed",
                "user_id": friend_id if uid == owner_id else owner_id,
                "time": int(time.time()),
            })
        return {"status": "ok", "retcode": 0, "data": {}}


def _err(msg: str) -> dict:
    return {"status": "failed", "retcode": 100, "msg": msg, "wording": msg}


def _session_view(sess, viewer_id: int, hub: "Hub") -> dict:
    db = dbmod.get_db()
    view = {
        "id": sess["id"],
        "kind": sess["kind"],
        "name": sess["name"],
        "last_msg_ts": sess["last_msg_ts"],
        "last_msg_preview": sess["last_msg_preview"],
    }
    if sess["kind"] == "direct":
        others = [u for u in db.session_members(sess["id"]) if u != viewer_id]
        if others:
            peer = db.get_user(others[0])
            if peer:
                view["peer"] = {
                    "user_id": peer["id"],
                    "nickname": peer["nickname"] or peer["username"],
                    "avatar_color": peer["avatar_color"],
                    "avatar": avatar_url(peer),
                    "online": hub.online(peer["id"]),
                }
    return view


hub = Hub()
