"""KiteChat server entry-point.

HTTP and HTTPS share the *same* port (default 8920).  When the TLS toggle
is on and valid cert/key files exist the port serves HTTPS, otherwise HTTP.
"""

import asyncio
import os
import sys

from server import config as cfg
from aiohttp import web as _web


def main() -> None:
    from server.web import create_app
    app = create_app()  # this also initializes the database

    from server import db as dbmod
    db = dbmod.get_db()
    port = int(db.get_config("port") or 8920)
    host = str(db.get_config("host") or "0.0.0.0").strip()

    disp = host if host != "0.0.0.0" else cfg.lan_ip()
    print("=" * 62)
    print("  KiteChat 服务端")
    print(f"  后台面板    :  http://{disp}:{port}/admin")
    print(f"  客户端页面  :  http://{disp}:{port}/")
    print(f"  WS 接入     :  ws://{disp}:{port}/ws")
    print(f"  OneBot V11  :  ws://{disp}:{port}/onebot")
    print(f"  管理员令牌  :  {db.get_config('admin_token')}")

    # ---- TLS (same port) ----
    tls_enabled = str(db.get_config("tls_enabled") or "0").strip()
    cert = os.path.join(cfg.DATA_DIR, "tls", "fullchain.pem")
    key = os.path.join(cfg.DATA_DIR, "tls", "privkey.pem")
    ssl_ctx = None
    if tls_enabled == "1" and os.path.exists(cert) and os.path.exists(key):
        import ssl as _ssl
        ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert, key)
        print(f"  传输安全    :  https://{disp}:{port}/")
    else:
        print(f"  传输安全    :  http://{disp}:{port}/")
    print("=" * 62)

    async def start() -> None:
        runner = _web.AppRunner(app)
        await runner.setup()
        if ssl_ctx is not None:
            site = _web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
        else:
            site = _web.TCPSite(runner, host, port)
        await site.start()
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
