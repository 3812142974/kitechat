"""Verify client-side reconnect_bridge op (the banner retry button)."""
import asyncio, json, os, sqlite3, time
import aiohttp
from testutil import cleanup_users

BASE = "http://127.0.0.1:8920"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kitechat.db")
created = []

def get_token():
    c = sqlite3.connect(DB)
    tok = c.execute("SELECT value FROM config WHERE key='admin_token'").fetchone()[0]
    c.close()
    return tok

async def main():
    admin = get_token()
    h = {"X-Admin-Token": admin, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        # register a user
        suffix = str(int(time.time()))[-6:]
        user, email = f"rc{suffix}", f"rc{suffix}@t.local"
        created.append(user)
        c = sqlite3.connect(DB)
        c.execute("INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at) VALUES('register',?,?,?, ?,0,?)",
                  (email, "123456", "{}", time.time()+600, time.time()))
        c.commit(); c.close()
        r = await s.post(f"{BASE}/api/register", json={"username":user,"password":"rctest1","password2":"rctest1","email":email,"code":"123456"})
        tok = (await r.json())["data"]["token"]

        ws = await s.ws_connect(f"{BASE}/ws")
        await ws.send_json({"op":"auth","token":tok})
        f = await ws.receive_json()
        assert f["op"] == "auth_ok"

        # 1) while connected: reconnect op should say already connected
        await ws.send_json({"op":"reconnect_bridge","req_id":1})
        while True:
            f = await asyncio.wait_for(ws.receive_json(), 30)
            if f.get("op") == "result" and f.get("req_id") == 1:
                print("已连接时:", f["status"], "|", f.get("msg",""))
                break

        # 2) break config -> banner scenario; op should report failure msg
        await s.post(f"{BASE}/api/admin/config", headers=h,
                     json={"app_ws_url":"ws://127.0.0.1:19999/ws","bot_ws_url":" "})
        await asyncio.sleep(7)
        await ws.send_json({"op":"reconnect_bridge","req_id":2})
        while True:
            f = await asyncio.wait_for(ws.receive_json(), 40)
            if f.get("op") == "result" and f.get("req_id") == 2:
                print("断开时:", f["status"], "|", f.get("msg","")[:60])
                break

        # 3) restore
        await s.post(f"{BASE}/api/admin/config", headers=h,
                     json={"app_ws_url":"ws://127.0.0.1:6199/ws"})
        await asyncio.sleep(7)
        r = await s.get(f"{BASE}/api/admin/config", headers=h)
        print("恢复后 bridge_connected:", (await r.json())["data"]["bridge_connected"])
        await ws.close()
    print("PASS")

try:
    asyncio.run(main())
finally:
    cleanup_users(created)
