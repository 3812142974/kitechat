"""OneBot V11 bot bridge — both directions.

1. Forward WS: server connects OUT to a bot's ws endpoint (config bot_ws_url).
2. Reverse WS: bot connects IN to /onebot (configured in the bot as
   ws://<server>:<port>/onebot). This endpoint lives in web.py and hands
   the websocket to BotBridge.attach_reverse().

KiteChat users carry a virtual number ("代号", users.virtual_qq) starting
from #1. Replies addressed to that number are routed back to the
originating AI session.

The bridge also answers OneBot API calls from external apps
(send_private_msg / send_msg / get_login_info / get_friend_list ...),
so KiteChat itself behaves like an OneBot V11 implementation for the
virtual users.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from . import db as dbmod
from . import onebot as ob
from .config import APP_NAME
from .hub import hub

log = logging.getLogger("kitechat.bridge")

IMPL_SELF_ID = 1_000_000_000  # self id of KiteChat itself as OneBot impl


def _reconnect_interval() -> float:
    """Seconds between automatic reconnect attempts (admin-configurable)."""
    try:
        v = float(dbmod.get_db().get_config("bridge_reconnect_interval") or 5)
        return max(2.0, min(v, 600.0))
    except (TypeError, ValueError):
        return 5.0


class BotBridge:
    def __init__(self) -> None:
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reverse: set = set()          # reverse-connected bot websockets
        self._session_task: asyncio.Task | None = None
        self._impl_task: asyncio.Task | None = None
        self._impl_ws: aiohttp.ClientWebSocketResponse | None = None
        self._impl_url: str = ""
        self._fwd_url: str = ""
        self.impl_connected = False
        self._qq_to_session: dict[int, str] = {}
        self._echo_seq = 0
        self._pending: dict[str, asyncio.Future] = {}
        self.self_info: dict = {"user_id": 0, "nickname": APP_NAME}
        self.connected = False
        # manual-reconnect support: wakes the reconnect loops and lets them
        # report the outcome of their next attempt back to the caller
        self._reconnect_event = asyncio.Event()
        self._reconnect_result: asyncio.Future | None = None
        self.last_reconnect_msg = ""

    @property
    def any_connected(self) -> bool:
        return self.connected or self.impl_connected or bool(self._reverse)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._session_task is None or self._session_task.done():
            self._session_task = asyncio.create_task(self._run())
        if self._impl_task is None or self._impl_task.done():
            self._impl_task = asyncio.create_task(self._run_impl())

    async def stop(self) -> None:
        for task in (self._session_task, self._impl_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        for ws in list(self._reverse):
            await ws.close()
        if self._impl_ws is not None:
            await self._impl_ws.close()

    async def _wait_interval(self) -> None:
        """Sleep for the configured reconnect interval, unless a manual
        reconnect request wakes us up early."""
        interval = _reconnect_interval()
        self._reconnect_event.clear()
        try:
            await asyncio.wait_for(self._reconnect_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    def _report_attempt(self, ok: bool, msg: str) -> None:
        """Resolve a pending manual-reconnect future, if any."""
        self.last_reconnect_msg = msg
        fut = self._reconnect_result
        if fut is not None and not fut.done():
            fut.set_result((ok, msg))
        self._reconnect_result = None

    async def reconnect(self, timeout: float = 25.0) -> dict:
        """Try to (re)connect right now; returns {connected, msg}.

        Used by the admin "reconnect" button and by clients. If a bot WS
        URL is configured, the reconnect loop is woken up and the outcome
        of its next attempt is awaited.
        """
        if self.any_connected:
            return {"connected": True, "msg": "已连接，无需重连"}
        db = dbmod.get_db()
        impl_url = db.get_config("app_ws_url").strip()
        fwd_url = db.get_config("bot_ws_url").strip()
        if not impl_url and not fwd_url:
            msg = "未配置 Bot WS 地址，请先在后台「AI / Bot 接入」填写后再试"
            self.last_reconnect_msg = msg
            return {"connected": False, "msg": msg}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._reconnect_result = fut
        self._reconnect_event.set()
        try:
            ok, msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            msg = f"连接超时（{_reconnect_interval()} 秒内未收到结果）"
            ok = self.any_connected
            self.last_reconnect_msg = msg
        # a second reconnect loop may have connected right after the first
        # failure report — prefer the real state over the stale report
        if not ok and self.any_connected:
            ok, msg = True, "已连接"
        return {"connected": ok, "msg": msg}

    async def _run(self) -> None:
        """Maintain forward WS connection with reconnect."""
        while True:
            url = dbmod.get_db().get_config("bot_ws_url").strip()
            if not url:
                self._set_connected(False)
                await self._wait_interval()
                continue
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(url, timeout=15) as ws:
                        log.info("bridge: connected to %s", url)
                        self._fwd_url = url
                        self._ws = ws
                        self._set_connected(True)
                        self._report_attempt(True, f"已连接到 {url}")
                        await self._identify(ws)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._on_event(json.loads(msg.data), ws)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("bridge connection error: %s", e)
                self._report_attempt(False, f"连接失败: {e}")
            self._ws = None
            self._set_connected(False)
            await self._wait_interval()

    async def _identify(self, ws) -> None:
        resp = await self._call(ws, "get_login_info", {})
        if resp and resp.get("status") == "ok":
            self.self_info = resp.get("data") or self.self_info

    # ------------------------------------------------------------ impl mode
    async def _run_impl(self) -> None:
        """Maintain Universal reverse-WS connection to a OneBot APP
        (e.g. AstrBot). KiteChat acts as the OneBot *implementation*:
        it pushes message events for virtual users and answers the app's
        API calls (send_private_msg / get_login_info / ...)."""
        while True:
            url = dbmod.get_db().get_config("app_ws_url").strip()
            if not url:
                self._set_impl_connected(False)
                await self._wait_interval()
                continue
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(
                            url, timeout=15,
                            headers={"X-Client-Role": "Universal",
                                     "X-Self-ID": str(IMPL_SELF_ID)}) as ws:
                        log.info("bridge(impl): connected to %s", url)
                        self._impl_url = url
                        self._impl_ws = ws
                        self._set_impl_connected(True)
                        self._report_attempt(True, f"已连接到 {url}")
                        self.self_info = {"user_id": IMPL_SELF_ID,
                                          "nickname": APP_NAME}
                        await ws.send_json({
                            "time": int(time.time()),
                            "self_id": IMPL_SELF_ID,
                            "post_type": "meta_event",
                            "meta_event_type": "lifecycle",
                            "sub_type": "connect",
                        })
                        hb = asyncio.create_task(self._impl_heartbeat(ws))
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        data = json.loads(msg.data)
                                    except json.JSONDecodeError:
                                        continue
                                    if isinstance(data, dict) and "action" in data:
                                        await ws.send_json(
                                            await self.handle_api_call(data))
                                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                                  aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("bridge(impl) connection error: %s", e)
                self._report_attempt(False, f"连接失败: {e}")
            self._impl_ws = None
            self._set_impl_connected(False)
            await self._wait_interval()

    async def apply_config(self) -> None:
        """Hot-reload WS urls from config.

        Called after admin saves settings: if the configured URL changed
        (or was cleared), drop the live connection so the reconnect loop
        picks up the new URL / goes offline immediately.
        """
        db = dbmod.get_db()
        want_impl = db.get_config("app_ws_url").strip()
        if want_impl != self._impl_url and self._impl_ws is not None:
            log.info("bridge(impl): config changed, reconnecting")
            try:
                await self._impl_ws.close()
            except Exception:  # noqa: BLE001
                pass
        want_fwd = db.get_config("bot_ws_url").strip()
        if want_fwd != self._fwd_url and self._ws is not None:
            log.info("bridge: config changed, reconnecting")
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _impl_heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await ws.send_json({
                    "time": int(time.time()),
                    "self_id": IMPL_SELF_ID,
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "status": {"online": True, "good": True},
                    "interval": 15000,
                })
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    async def push_impl_message(self, virtual_qq: int, nickname: str,
                                segments: list[dict], message_id: int) -> bool:
        """Push a OneBot V11 private-message event to the connected app."""
        if self._impl_ws is None:
            return False
        event = {
            "time": int(time.time()),
            "self_id": IMPL_SELF_ID,
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": message_id,
            "user_id": virtual_qq,
            "message": segments,
            "raw_message": ob.segments_to_cq(segments),
            "font": 0,
            "sender": {"user_id": virtual_qq, "nickname": nickname,
                       "sex": "unknown", "age": 0},
        }
        try:
            await self._impl_ws.send_json(event)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _set_impl_connected(self, value: bool) -> None:
        changed = self.impl_connected != value
        self.impl_connected = value
        if changed:
            asyncio.create_task(hub.broadcast({
                "post_type": "meta_event", "meta_event_type": "bridge",
                "connected": self.any_connected, "time": int(time.time()),
            }))

    def _set_connected(self, value: bool) -> None:
        changed = self.connected != value
        self.connected = value
        if changed:
            asyncio.create_task(hub.broadcast({
                "post_type": "meta_event", "meta_event_type": "bridge",
                "connected": self.any_connected, "time": int(time.time()),
            }))

    # ------------------------------------------------------------ sending
    def bind_session(self, virtual_qq: int, session_id: str) -> None:
        self._qq_to_session[virtual_qq] = session_id

    async def send_private_msg(self, user_id: int,
                               segments: list[dict]) -> bool:
        message = ob.segments_to_cq(segments)
        for ws in list(self._reverse):
            try:
                await ws.send_json({
                    "action": "send_private_msg",
                    "params": {"user_id": user_id, "message": message},
                    "echo": f"nova-{time.time_ns()}",
                })
                return True
            except Exception:  # noqa: BLE001
                self._reverse.discard(ws)
        if self._ws is not None:
            await self._call(self._ws, "send_private_msg",
                             {"user_id": user_id, "message": message})
            return True
        return False

    async def _call(self, ws, action: str, params: dict,
                    timeout: float = 10.0) -> dict | None:
        self._echo_seq += 1
        echo = f"nova-{self._echo_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            log.debug("bridge call %s failed: %s", action, e)
            return None
        finally:
            self._pending.pop(echo, None)

    # ------------------------------------------------------------ events
    async def _on_event(self, event: dict, source_ws) -> None:
        # action responses (for _call)
        echo = event.get("echo")
        if echo is not None and "retcode" in event:
            fut = self._pending.get(str(echo))
            if fut and not fut.done():
                fut.set_result(event)
            return
        if event.get("post_type") != "message":
            return
        if event.get("message_type") != "private":
            return
        qq = int(event.get("user_id", 0))
        raw = event.get("message") or event.get("raw_message") or ""
        segments = ob.normalize_message(raw)
        if not segments:
            return
        await self.route_incoming(qq, segments, event)

    async def route_incoming(self, qq: int, segments: list[dict],
                             event: dict) -> None:
        db = dbmod.get_db()
        sender_name = (event.get("sender") or {}).get("nickname") or "Bot"
        owner = db.get_user_by_vqq(qq)
        if owner is not None:
            user_id = owner["id"]
            session_id = self._qq_to_session.get(qq)
            if not session_id or db.get_session(session_id) is None:
                # fallback: user's newest AI session
                for s in db.list_user_sessions(user_id):
                    if s["kind"] == "ai":
                        session_id = s["id"]
                        break
            if not session_id:
                return
            await hub.deliver_session_message(
                session_id, 0, sender_name, segments, ts=event.get("time")
            )
        else:
            # external QQ user -> deliver to admin (user #1) external session
            admin = db.get_user(1)
            if admin is None:
                return
            pair = f"ext:{qq}"
            sess = db.get_direct_session(pair)
            if sess is None:
                sid = db.create_session("direct", f"QQ:{qq}", None, pair_key=pair)
                db.add_member(sid, admin["id"])
                await hub.send_to_user(admin["id"], {
                    "post_type": "notice", "notice_type": "session_created",
                    "session": {"id": sid, "kind": "direct",
                                "name": f"QQ:{qq}", "external_qq": qq},
                    "time": int(time.time()),
                })
                sess = db.get_session(sid)
            await hub.deliver_session_message(
                sess["id"], qq, f"QQ用户{qq}", segments, ts=event.get("time")
            )

    # ------------------------------------------------------------ reverse WS
    async def attach_reverse(self, ws) -> None:
        """Serve a reverse-WS OneBot bot connection."""
        self._reverse.add(ws)
        self._set_connected(True)
        try:
            # lifecycle connect event per onebot v11
            await ws.send_json({
                "time": int(time.time()), "self_id": self.self_info.get("user_id", 0),
                "post_type": "meta_event", "meta_event_type": "lifecycle",
                "sub_type": "connect",
            })
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if "action" in data:
                        await ws.send_json(await self.handle_api_call(data))
                    else:
                        await self._on_event(data, ws)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            self._reverse.discard(ws)
            if not self._reverse and self._ws is None:
                self._set_connected(False)

    async def handle_api_call(self, data: dict) -> dict:
        """Answer OneBot V11 API calls from external apps/bots."""
        action = data.get("action")
        params = data.get("params") or {}
        echo = data.get("echo")
        db = dbmod.get_db()

        def ok(payload: Any = None) -> dict:
            return {"status": "ok", "retcode": 0, "data": payload, "echo": echo}

        def fail(msg: str) -> dict:
            return {"status": "failed", "retcode": 100, "msg": msg,
                    "wording": msg, "echo": echo}

        if action in ("send_private_msg", "send_msg"):
            qq = int(params.get("user_id", 0))
            segments = ob.normalize_message(params.get("message"))
            owner = db.get_user_by_vqq(qq)
            if owner is not None:
                user_id = owner["id"]
                session_id = self._qq_to_session.get(qq)
                if not session_id or db.get_session(session_id) is None:
                    for s in db.list_user_sessions(user_id):
                        if s["kind"] == "ai":
                            session_id = s["id"]
                            break
                if not session_id:
                    return fail("用户无 AI 会话")
                sender = params.get("sender") or {}
                name = sender.get("nickname") or "Bot"
                frame = await hub.deliver_session_message(
                    session_id, 0, name, segments
                )
                return ok({"message_id": frame["message_id"]})
            return fail("目标不是 KiteChat 虚拟用户")

        if action == "get_login_info":
            return ok(self.self_info)

        if action == "get_friend_list":
            out = []
            for row in db.list_users():
                out.append({
                    "user_id": row["virtual_qq"],
                    "nickname": f"#{row['virtual_qq']} {row['username']}",
                    "remark": "",
                })
            return ok(out)

        if action == "get_version_info":
            return ok({"app_name": APP_NAME, "app_version": "1.0.0",
                       "protocol_version": "v11"})

        if action == "get_status":
            return ok({"online": True, "good": True})

        return fail(f"不支持的 action: {action}")


bridge = BotBridge()
