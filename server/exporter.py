def _ws_to_http_tls(ws: str) -> str:
    """ws://ip:port/ws -> https://ip:port (same port, HTTPS scheme)."""
    base = _ws_to_http(ws)
    if "://" not in base:
        return ""
    # just swap the scheme: http → https, ws → wss
    return base.replace("http://", "https://", 1).replace("ws://", "wss://", 1)
