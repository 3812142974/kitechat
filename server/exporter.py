"""KiteChat one-click client export engine.

Generates a Windows EXE (pyinstaller onefile) and/or a signed Android APK
(gradle + apksigner) with the current server address injected into an
obfuscated config.bin (XOR + base64) so the shipped client connects on first
launch without any manual configuration.

Interfaces consumed by server/web.py:
  - start_build(target, ws, version, scheme, ca_cert_path) -> dict  # job
  - jobs_snapshot() -> dict  # {"current": job_or_none, "jobs": [...]}
  - artifact_path(target) -> str  # newest artifact path or ""
  - latest_apk_manifest() -> dict | None
  - shutdown() -> None            # best-effort flag
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import config as cfg

# ---- config.bin obfuscation (must match client/web/app.js + desktop/main.py) ----
_OBF_KEY = b"n0v4ch4t$cfg"


def _obfuscate_into_bin(payload: dict, out_path: str) -> None:
    """Write json payload into XOR+base64 config.bin at out_path."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out = bytes(b ^ _OBF_KEY[i % len(_OBF_KEY)] for i, b in enumerate(data))
    with open(out_path, "w", encoding="ascii") as f:
        f.write(base64.b64encode(out).decode("ascii"))


# --------------------------------------------------------------- tool detect
_SDK_CACHE = {"v": None}
_JDK_CACHE = {"v": None}


def _find_sdk() -> str:
    """Auto-detect Android SDK root. Order:
    env ANDROID_HOME -> ANDROID_SDK_ROOT -> project tools/ -> common dirs.
    Cached after first call."""
    if _SDK_CACHE["v"] is not None:
        return _SDK_CACHE["v"]
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var, "").strip()
        if v and os.path.isdir(v):
            _SDK_CACHE["v"] = v
            return v
    # project-local SDK (installed by tools/setup_android_sdk.sh)
    proj_local = os.path.join(cfg.ROOT, "tools", "android-sdk")
    if os.path.isdir(proj_local):
        _SDK_CACHE["v"] = proj_local
        return proj_local
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "AppData", "Local", "Android", "Sdk"),
        os.path.join(home, "Android", "Sdk"),
        r"C:\Android\Sdk",
        r"D:\Android\Sdk",
    ]
    for c in cands:
        if os.path.isdir(c):
            _SDK_CACHE["v"] = c
            return c
    _SDK_CACHE["v"] = ""
    return ""


def _find_jdk() -> str:
    """Auto-detect JDK home (must contain bin/java or bin/java.exe)."""
    if _JDK_CACHE["v"] is not None:
        return _JDK_CACHE["v"]
    for var in ("JAVA_HOME",):
        v = os.environ.get(var, "").strip()
        if v and os.path.isdir(v):
            _JDK_CACHE["v"] = v
            return v
    # project-local JDK
    proj_local = os.path.join(cfg.ROOT, "tools", "jdk")
    if os.path.isdir(proj_local):
        _JDK_CACHE["v"] = proj_local
        return proj_local
    _JDK_CACHE["v"] = ""
    return ""


def _find_xcodebuild() -> str:
    """Auto-detect xcodebuild path (macOS only)."""
    # Check common locations
    for candidate in ["/usr/bin/xcodebuild", "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild"]:
        if os.path.isfile(candidate):
            return candidate
    # Try PATH
    try:
        result = subprocess.run(["which", "xcodebuild"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _find_hdiutil() -> str:
    """Auto-detect hdiutil path (macOS only, for creating DMG)."""
    for candidate in ["/usr/bin/hdiutil", "/sbin/hdiutil"]:
        if os.path.isfile(candidate):
            return candidate
    try:
        result = subprocess.run(["which", "hdiutil"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# --------------------------------------------------------------- build jobs
_JOBS: dict[str, dict] = {}
_SHUTDOWN_FLAG = False


def _register_job(target: str, job: dict) -> None:
    _JOBS[target] = job


def _update_job(target: str, **kw) -> None:
    if target in _JOBS:
        _JOBS[target].update(kw)


# ------------------------------------------------------------------- public
def start_build(
    target: str,
    ws: str,
    version: str = "1.0.0",
    scheme: str = "https",
    ca_cert_path: str = "",
) -> dict:
    """Start a build job for the given target.

    target: "android" | "windows" | "ios" | "macos" | "web"
    ws: WebSocket address to embed (e.g. "wss://example.com/ws")
    version: version string for the artifact
    scheme: "https" or "http"
    ca_cert_path: optional path to CA cert to embed

    Returns: {"job_id": target, "status": "started"}
    """
    if target in _JOBS and _JOBS[target].get("status") == "building":
        return {"error": "build already in progress"}

    job = {
        "target": target,
        "status": "started",
        "started": datetime.now().isoformat(),
        "version": version,
        "progress": 0,
        "log": [],
        "error": None,
    }
    _register_job(target, job)

    # Build in a background thread
    t = threading.Thread(
        target=_run_build, args=(target, ws, version, scheme, ca_cert_path), daemon=True
    )
    t.start()
    return {"job_id": target, "status": "started"}


def jobs_snapshot() -> dict:
    """Return current build jobs state."""
    return {"current": _JOBS.get(list(_JOBS.keys())[-1]) if _JOBS else None, "jobs": dict(_JOBS)}


def artifact_path(target: str) -> str:
    """Return the newest artifact path for the given target, or ''."""
    exports = os.path.join(cfg.ROOT, "exports")
    if not os.path.isdir(exports):
        return ""
    # Find newest file matching target
    ext_map = {
        "android": [".apk"],
        "windows": [".exe"],
        "ios": [".ipa"],
        "macos": [".dmg"],
    }
    exts = ext_map.get(target, [])
    if not exts:
        return ""
    newest = ""
    newest_mtime = 0
    for f in os.listdir(exports):
        if any(f.endswith(ext) for ext in exts):
            fp = os.path.join(exports, f)
            mt = os.path.getmtime(fp)
            if mt > newest_mtime:
                newest_mtime = mt
                newest = fp
    return newest


def latest_apk_manifest() -> dict | None:
    """Return manifest info from the latest APK build, if available."""
    ap = artifact_path("android")
    if not ap:
        return None
    # Try to extract version info from the APK filename
    base = os.path.basename(ap)
    return {"path": ap, "filename": base}


def shutdown() -> None:
    """Best-effort shutdown flag for build threads."""
    global _SHUTDOWN_FLAG
    _SHUTDOWN_FLAG = True


# ----------------------------------------------------------------- builders
def _run_build(target: str, ws: str, version: str, scheme: str, ca_cert_path: str) -> None:
    """Main build dispatcher."""
    _update_job(target, status="building", progress=0)
    try:
        builder_map = {
            "android": _build_android,
            "windows": _build_windows,
            "ios": _build_ios,
            "macos": _build_macos,
        }
        builder = builder_map.get(target)
        if not builder:
            _update_job(target, status="failed", error=f"unsupported target: {target}")
            return
        builder(ws, version, scheme, ca_cert_path)
        _update_job(target, status="done", progress=100)
    except Exception as e:
        _update_job(target, status="failed", error=str(e))


def _build_android(ws: str, version: str, scheme: str, ca_cert_path: str) -> None:
    """Build Android APK."""
    sdk = _find_sdk()
    jdk = _find_jdk()
    if not sdk or not jdk:
        _update_job("android", status="failed", error="SDK or JDK not found")
        return

    # Config injection
    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    _update_job("android", progress=10, log=["Injecting config.bin..."])
    _obfuscate_into_bin(
        {"ws_address": ws, "server_url": server_url, "app_name": "KiteChat", "version": version},
        os.path.join(cfg.ROOT, "client", "web", "config.bin"),
    )

    # Build
    gradlew = os.path.join(cfg.ROOT, "tools", "gradlew")
    if sys.platform == "win32":
        gradlew += ".bat"
    if not os.path.isfile(gradlew):
        _update_job("android", status="failed", error="gradlew not found")
        return

    env = os.environ.copy()
    env["ANDROID_HOME"] = sdk
    env["JAVA_HOME"] = jdk
    _update_job("android", progress=20, log=["Building APK..."])
    proc = subprocess.Popen(
        [gradlew, "--no-daemon", "-p", os.path.join(cfg.ROOT, "client", "android"), "assembleRelease"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if "Task :" in line.strip():
            _update_job("android", log=[line.strip()])
    proc.wait()
    if proc.returncode != 0:
        _update_job("android", status="failed", error="Gradle build failed")
        return

    # Sign
    unsigned = os.path.join(cfg.ROOT, "client", "android", "app", "build", "outputs", "apk", "release", "app-release-unsigned.apk")
    if not os.path.isfile(unsigned):
        _update_job("android", status="failed", error="unsigned APK not found")
        return

    _update_job("android", progress=80, log=["Signing APK..."])
    keystore = os.path.join(cfg.ROOT, "data", "kitechat-release.jks")
    # Read keystore password from DB
    db = __import__("server.db", fromlist=["get_db"]).get_db()
    ks_pass = db.get_config("keystore_password", "kitechat")
    ks_alias = db.get_config("keystore_alias", "kitechat")
    apksigner = os.path.join(cfg.ROOT, "tools", "apksigner")
    if sys.platform == "win32":
        apksigner += ".bat"

    out_dir = os.path.join(cfg.ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    out_apk = os.path.join(out_dir, f"KiteChat-{version}-{int(time.time())}.apk")

    subprocess.run([
        apksigner, "sign",
        "--ks", keystore, "--ks-pass", f"pass:{ks_pass}",
        "--key-pass", f"pass:{ks_pass}", "--ks-key-alias", ks_alias,
        "--out", out_apk, unsigned,
    ], check=True)
    _update_job("android", progress=100, log=[f"APK exported: {out_apk}"])


def _build_windows(ws: str, version: str, scheme: str, ca_cert_path: str) -> None:
    """Build Windows EXE."""
    _update_job("windows", progress=10, log=["Preparing config.bin..."])
    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    _obfuscate_into_bin(
        {"ws_address": ws, "server_url": server_url, "app_name": "KiteChat", "version": version},
        os.path.join(cfg.ROOT, "client", "web", "config.bin"),
    )

    desktop = os.path.join(cfg.ROOT, "client", "desktop")
    _update_job("windows", progress=20, log=["Building EXE..."])
    proc = subprocess.Popen(
        [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed",
         "--name", f"KiteChat-{version}",
         "--distpath", os.path.join(cfg.ROOT, "exports"),
         "--workpath", os.path.join(cfg.ROOT, "build", "windows"),
         "--specpath", os.path.join(cfg.ROOT, "build"),
         os.path.join(desktop, "main.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if line.strip():
            _update_job("windows", log=[line.strip()])
    proc.wait()
    if proc.returncode != 0:
        _update_job("windows", status="failed", error="PyInstaller build failed")
        return
    _update_job("windows", progress=100, log=["EXE exported successfully"])


# ------------------------------------------------------------------- iOS ----
def _build_ios(ws: str, version: str, scheme: str, ca_cert_path: str) -> None:
    """Build iOS IPA via xcodebuild (requires macOS + Xcode)."""
    xcodebuild = _find_xcodebuild()
    if not xcodebuild:
        _update_job("ios", status="failed",
                    error="xcodebuild not found. iOS builds require macOS with Xcode installed.")
        return

    _update_job("ios", progress=10, log=["Injecting config..."])
    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    _obfuscate_into_bin(
        {"ws_address": ws, "server_url": server_url, "app_name": "KiteChat", "version": version},
        os.path.join(cfg.ROOT, "client", "ios", "KiteChat", "config.bin"),
    )

    _update_job("ios", progress=20, log=["Archiving iOS app..."])
    out_dir = os.path.join(cfg.ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    archive_path = os.path.join(out_dir, "KiteChat.xcarchive")
    ipa_path = os.path.join(out_dir, f"KiteChat-{version}-{int(time.time())}.ipa")

    # Archive
    proc = subprocess.Popen(
        [xcodebuild, "archive",
         "-scheme", "KiteChat",
         "-project", os.path.join(cfg.ROOT, "client", "ios", "KiteChat.xcodeproj"),
         "-archivePath", archive_path,
         "-configuration", "Release"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if line.strip():
            _update_job("ios", log=[line.strip()])
    proc.wait()
    if proc.returncode != 0:
        _update_job("ios", status="failed", error="xcodebuild archive failed")
        return

    _update_job("ios", progress=60, log=["Exporting IPA..."])

    # Export options plist (for Ad Hoc / development)
    export_plist = os.path.join(out_dir, "ExportOptions.plist")
    with open(export_plist, "w") as f:
        f.write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>ad-hoc</string>
    <key>stripSwiftSymbols</key>
    <true/>
    <key>compileBitcode</key>
    <false/>
</dict>
</plist>
""")

    proc = subprocess.Popen(
        [xcodebuild, "-exportArchive",
         "-archivePath", archive_path,
         "-exportOptionsPlist", export_plist,
         "-exportPath", out_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if line.strip():
            _update_job("ios", log=[line.strip()])
    proc.wait()
    if proc.returncode != 0:
        _update_job("ios", status="failed", error="IPA export failed")
        return

    # Rename exported IPA
    exported = os.path.join(out_dir, "KiteChat.ipa")
    if os.path.isfile(exported):
        os.rename(exported, ipa_path)

    _update_job("ios", progress=100, log=[f"IPA exported: {ipa_path}"])


# ------------------------------------------------------------------- macOS --
def _build_macos(ws: str, version: str, scheme: str, ca_cert_path: str) -> None:
    """Build macOS DMG via xcodebuild + hdiutil (requires macOS + Xcode)."""
    xcodebuild = _find_xcodebuild()
    hdiutil = _find_hdiutil()
    if not xcodebuild:
        _update_job("macos", status="failed",
                    error="xcodebuild not found. macOS builds require macOS with Xcode installed.")
        return

    _update_job("macos", progress=10, log=["Injecting config..."])
    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    _obfuscate_into_bin(
        {"ws_address": ws, "server_url": server_url, "app_name": "KiteChat", "version": version},
        os.path.join(cfg.ROOT, "client", "macos", "KiteChat", "config.bin"),
    )

    _update_job("macos", progress=20, log=["Building macOS app..."])
    out_dir = os.path.join(cfg.ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    archive_path = os.path.join(out_dir, "KiteChat-macOS.xcarchive")
    app_path = os.path.join(out_dir, "KiteChat.app")

    # Archive
    proc = subprocess.Popen(
        [xcodebuild, "archive",
         "-scheme", "KiteChat-macOS",
         "-project", os.path.join(cfg.ROOT, "client", "macos", "KiteChat.xcodeproj"),
         "-archivePath", archive_path,
         "-configuration", "Release"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if line.strip():
            _update_job("macos", log=[line.strip()])
    proc.wait()
    if proc.returncode != 0:
        _update_job("macos", status="failed", error="xcodebuild archive failed")
        return

    _update_job("macos", progress=50, log=["Extracting .app..."])

    # Export .app from archive
    proc = subprocess.Popen(
        [xcodebuild, "-exportArchive",
         "-archivePath", archive_path,
         "-exportOptionsPlist", os.path.join(cfg.ROOT, "client", "macos", "ExportOptions.plist"),
         "-exportPath", out_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        if line.strip():
            _update_job("macos", log=[line.strip()])
    proc.wait()

    if not os.path.isdir(app_path):
        _update_job("macos", status="failed", error=".app not found after export")
        return

    _update_job("macos", progress=70, log=["Creating DMG..."])

    # Create DMG
    if hdiutil:
        dmg_path = os.path.join(out_dir, f"KiteChat-macOS-{version}-{int(time.time())}.dmg")
        proc = subprocess.Popen(
            [hdiutil, "create",
             "-volname", "KiteChat",
             "-srcfolder", app_path,
             "-ov", "-format", "UDZO",
             dmg_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        proc.wait()
        if proc.returncode != 0:
            _update_job("macos", status="failed", error="hdiutil DMG creation failed")
            return
        _update_job("macos", progress=100, log=[f"DMG exported: {dmg_path}"])
    else:
        # Fallback: just export the .app as a zip
        import zipfile
        zip_path = os.path.join(out_dir, f"KiteChat-macOS-{version}-{int(time.time())}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(app_path):
                for file in files:
                    fp = os.path.join(root, file)
                    arcname = os.path.relpath(fp, os.path.dirname(app_path))
                    zf.write(fp, arcname)
        _update_job("macos", progress=100, log=[f"ZIP exported (no hdiutil): {zip_path}"])
