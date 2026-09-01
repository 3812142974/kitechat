"""KiteChat Windows desktop client (pywebview shell).

Reads config.bin (obfuscated, injected by server one-click export) next to
the executable for the server WS/HTTP address, then loads the bundled web
app. The server address is never stored in plaintext inside the client.
Single-file friendly: web assets can live in sys._MEIPASS (PyInstaller).
"""
from __future__ import annotations

import base64
import json
import os
import sys

_OBF_KEY = b"n0v4ch4t$cfg"


def deobfuscate(b64: str) -> str:
    raw = base64.b64decode(b64.strip())
    out = bytes(b ^ _OBF_KEY[i % len(_OBF_KEY)] for i, b in enumerate(raw))
    return out.decode("utf-8")


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def assets_dir() -> str:
    # PyInstaller bundles data into _MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = os.path.join(sys._MEIPASS, "web")
        if os.path.isdir(cand):
            return cand
    # dev layout: client/desktop -> ../web
    return os.path.normpath(os.path.join(base_dir(), "..", "web"))


def load_config() -> dict:
    """config.bin search order:
    1) next to the exe (injected by export, survives updates)
    2) inside bundled assets
    """
    for cand in (os.path.join(base_dir(), "config.bin"),
                 os.path.join(assets_dir(), "config.bin")):
        if os.path.exists(cand):
            try:
                with open(cand, "r", encoding="ascii") as f:
                    return json.loads(deobfuscate(f.read()))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return {}


def set_window_icon(window) -> None:
    """Set the runtime window/taskbar icon (Windows).

    The kite icon is embedded in the .exe resource, so we extract it from
    sys.executable and apply it via WM_SETICON. Falls back to app.ico in the
    bundle (dev) if present.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes

        # pywebview doesn't expose hwnd on the Window object; find it by title.
        hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
        if not hwnd:
            return

        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1

        def apply(h_small, h_large):
            if h_small:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
            if h_large:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_large)

        if getattr(sys, "frozen", False):
            # pull icon group 0 out of the running exe
            large = ctypes.wintypes.HICON()
            small = ctypes.wintypes.HICON()
            got = ctypes.windll.shell32.ExtractIconExW(
                sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1)
            if got:
                apply(small.value, large.value)
                return

        # fallback: load app.ico from disk (dev layout / bundle)
        candidates = [os.path.join(base_dir(), "app.ico")]
        if hasattr(sys, "_MEIPASS"):
            candidates.append(os.path.join(sys._MEIPASS, "app.ico"))
        for cand in candidates:
            if cand and os.path.exists(cand):
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x0010
                s = ctypes.windll.user32.LoadImageW(None, cand, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                l = ctypes.windll.user32.LoadImageW(None, cand, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                apply(s, l)
                return
    except Exception:
        pass


def main() -> None:
    import webview

    cfg = load_config()
    web_dir = assets_dir()
    entry = os.path.join(web_dir, "index.html")
    title = cfg.get("app_name", "KiteChat")

    if not os.path.exists(entry):
        # last resort: open remote web client directly
        url = cfg.get("server_url", "")
        if url:
            win = webview.create_window(title, url, width=1180, height=800,
                                        min_size=(860, 600))
        else:
            print("找不到客户端资源，也缺少服务端配置")
            sys.exit(1)
    else:
        # copy config.bin into assets so the web app sees it via fetch
        cfg_in_assets = os.path.join(web_dir, "config.bin")
        if cfg and not os.path.exists(cfg_in_assets):
            try:
                payload = json.dumps(cfg, ensure_ascii=False)
                data = payload.encode("utf-8")
                out = bytes(b ^ _OBF_KEY[i % len(_OBF_KEY)] for i, b in enumerate(data))
                with open(cfg_in_assets, "w", encoding="ascii") as f:
                    f.write(base64.b64encode(out).decode("ascii"))
            except OSError:
                pass
        win = webview.create_window(title, entry, width=1180, height=800,
                                    min_size=(860, 600))

    webview.start(set_window_icon, win)


if __name__ == "__main__":
    main()
