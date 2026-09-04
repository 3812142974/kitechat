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
    """Write json payload into an XOR+base64 config.bin at out_path."""
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
    """Auto-detect JDK home (must contain bin/java.exe). Order:
    env JAVA_HOME -> java cmd -> project tools/ -> common dirs, validating.
    Cached after first call."""
    if _JDK_CACHE["v"] is not None:
        return _JDK_CACHE["v"]
    v = os.environ.get("JAVA_HOME", "").strip()
    if v:
        v = _resolve_jdk_home(v)
        if v:
            _JDK_CACHE["v"] = v
            return v
    javac = shutil.which("java")
    if javac:
        root = os.path.dirname(os.path.dirname(os.path.abspath(javac)))
        root = _resolve_jdk_home(root)
        if root:
            _JDK_CACHE["v"] = root
            return root
    # project-local JDK (installed by tools/setup_android_sdk.sh -> tools/jdk)
    proj_jdk = os.path.join(cfg.ROOT, "tools", "jdk")
    if os.path.isdir(proj_jdk):
        # default dist layout: tools/jdk/<jdk-17.x.y>/bin/java.exe
        for name in sorted(os.listdir(proj_jdk)):
            p = os.path.join(proj_jdk, name)
            r = _resolve_jdk_home(p)
            if r:
                _JDK_CACHE["v"] = r
                return r
        r = _resolve_jdk_home(proj_jdk)
        if r:
            _JDK_CACHE["v"] = r
            return r
    home = os.path.expanduser("~")
    _je = _java_exe()
    _jc = _javac_exe()
    if _is_windows():
        candidate_bases = [
            os.path.join(home, "AppData", "Local", "Programs"),
            r"D:\Program Files\Java",
            r"C:\Program Files\Eclipse Adoptium",
            r"C:\Program Files\Java",
            r"C:\Program Files\Microsoft",
        ]
    else:
        # Linux / other: common JDK install roots
        candidate_bases = [
            "/usr/lib/jvm",
            "/opt/java",
            "/opt",
            os.path.join(home, ".jdks"),
            "/usr/java",
        ]
    # collect all valid JDK homes then pick the one with the highest version
    found: list[str] = []
    for base in candidate_bases:
        if not os.path.isdir(base):
            continue
        # avoid enumerating huge unrelated dirs that aren't JDK-ish (slow)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            # only bother on plausible JDK dirs (has bin/java)
            if not os.path.isfile(os.path.join(p, "bin", _je)):
                continue
            jdk = _resolve_jdk_home(p)
            if jdk and jdk not in found:
                found.append(jdk)
    if found:
        best = _pick_highest_jdk(found)
        _JDK_CACHE["v"] = best
        return best
    # last resort: search shallowly under known roots for java+javac
    # (limited to a small, targeted set; never scan the whole filesystem tree)
    for base in (r"D:\Program Files", r"C:\Program Files", r"C:\Program Files (x86)",
                 "/usr/lib/jvm", "/opt", "/usr/java"):
        if not os.path.isdir(base):
            continue
        for name in ("Java", "Eclipse Adoptium", "jdk*", "java*", "Microsoft"):
            import glob
            for dirpath in glob.glob(os.path.join(base, name)):
                if not os.path.isdir(dirpath):
                    continue
                if os.path.isfile(os.path.join(dirpath, "bin", _je)) \
                        and os.path.isfile(os.path.join(dirpath, "bin", _jc)):
                    jdk = _resolve_jdk_home(dirpath)
                    if jdk and jdk not in found:
                        found.append(jdk)
    if found:
        best = _pick_highest_jdk(found)
        _JDK_CACHE["v"] = best
        return best
    _JDK_CACHE["v"] = ""
    return ""


def _pick_highest_jdk(cands: list[str]) -> str:
    """Prefer the candidate with the highest version (e.g. Java21 > Java17)."""
    def ver(p: str) -> int:
        base = os.path.basename(p.rstrip("\\/"))
        digits = [int(d) for d in re.findall(r"\d+", base)]
        return digits[0] if digits else 0

    def path_bias(p: str) -> int:
        # prefer D: over C: (space preference) when version ties
        return 0 if p.upper().startswith("D:") else 1

    return sorted(cands, key=lambda p: (-ver(p), path_bias(p)))[0]


def _java_exe() -> str:
    """Platform-aware java binary name: 'java.exe' on Windows, 'java' elsewhere."""
    return "java.exe" if _is_windows() else "java"


def _javac_exe() -> str:
    """Platform-aware javac binary name: 'javac.exe' on Windows, 'javac' elsewhere."""
    return "javac.exe" if _is_windows() else "javac"


def _resolve_jdk_home(cand: str) -> str:
    """Return cand if it's a real JDK home (has bin/java AND bin/javac),
    else try to follow symlink-ish 'latest' folders / children. Empty when invalid.
    Requiring javac excludes JREs (Android build needs the full JDK)."""
    _je = _java_exe()
    _jc = _javac_exe()

    def is_jdk(p: str) -> bool:
        return (os.path.isfile(os.path.join(p, "bin", _je))
                and os.path.isfile(os.path.join(p, "bin", _jc)))
    saw: set[str] = set()

    def walk(p: str) -> str:
        p = os.path.abspath(p)
        if p in saw:
            return ""
        saw.add(p)
        if is_jdk(p):
            return p
        if os.path.isdir(p):
            for name in os.listdir(p):
                sub = os.path.join(p, name)
                # avoid recursing into obviously bad large dirs
                if name in ("lib", "bin", "site-packages", ".venv"):
                    continue
                # follow deeper into 'latest' / versioned jdk dirs
                if os.path.isdir(sub):
                    r = walk(sub)
                    if r:
                        return r
        return ""

    return walk(cand)


def _is_windows() -> bool:
    """True on native Windows (not under WSL/MSYS)."""
    return os.name == "nt"


def _auto_install_sdk_jdk() -> str:
    """Run tools/setup_android_sdk.py to install missing SDK/JDK.

    The Python installer is the canonical path: it is cross-platform and has
    NO dependency on Git (the .sh/.bat wrappers would need git-bash/cmd and
    may not exist on the user's machine). Returns a status string to append
    to the detection message; "" when the script is missing."""
    script = os.path.join(cfg.ROOT, "tools", "setup_android_sdk.py")
    if not os.path.isfile(script):
        return ""
    try:
        cp = subprocess.run(
            [sys.executable, script], capture_output=True, text=True,
            timeout=900, encoding="utf-8", errors="replace")
        return "auto-install done" if cp.returncode == 0 \
            else f"auto-install failed: {(cp.stderr or cp.stdout)[-300:]}"
    except Exception as e:  # noqa: BLE001
        return f"auto-install error: {e}"


def _clear_tool_cache() -> None:
    """Force _find_sdk/_find_jdk to re-scan (e.g. after an auto-install)."""
    _SDK_CACHE["v"] = None
    _JDK_CACHE["v"] = None


def _ensure_android_config(android_dir: str) -> tuple[bool, str]:
    """Auto-detect SDK/JDK and write sdk.dir into local.properties.

    When SDK and/or JDK are missing, attempts an automatic install via
    tools/setup_android_sdk.sh (platform-aware). Retries detection once
    after the install so a successful run proceeds to build.

    Returns (ok, message). ok is True when an SDK was located so Gradle can
    build; a missing JDK is reported as a warning but not fatal here."""

    sdk = _find_sdk()
    jdk = _find_jdk()
    msg = []

    # --- auto-install when anything is missing ---
    if not sdk or not jdk:
        ans = _auto_install_sdk_jdk()
        if ans:
            msg.append(ans)
        # re-detect after install attempt (clear cache so _find_* re-scans)
        _clear_tool_cache()
        sdk2 = _find_sdk()
        jdk2 = _find_jdk()
        sdk = sdk2 or sdk
        jdk = jdk2 or jdk

    if not sdk:
        msg.append(
            "未检测到 Android SDK，请设置 ANDROID_HOME 或安装后重试"
            " (https://developer.android.com/studio#command-line-tools-only)"
        )
    else:
        lp = os.path.join(android_dir, "local.properties")
        # Windows path with forward slashes is accepted & escaped for Gradle
        sdk_dir = sdk.replace("\\", "/")
        content = f"sdk.dir={sdk_dir}\n"
        with open(lp, "w", encoding="utf-8") as f:
            f.write(content)
        msg.append(f"SDK={sdk}")
    if not jdk:
        msg.append(
            "未检测到 JDK，请设置 JAVA_HOME (Android 构建需 JDK 17+)"
            " (https://adoptium.net/temurin/releases/?version=17)"
        )
    return bool(sdk), "; ".join(msg)


def _keystore_pass() -> str:
    """Keystore password. Prefer KITECHAT_KEYSTORE_PASS env, else db config."""
    v = os.environ.get("KITECHAT_KEYSTORE_PASS", "").strip()
    if v:
        return v
    try:
        from . import db as dbmod
        return (dbmod.get_db().get_config("keystore_pass") or "").strip()
    except Exception:
        return ""


def _keystore_alias() -> str:
    v = os.environ.get("KITECHAT_KEYSTORE_ALIAS", "").strip()
    if v:
        return v
    try:
        from . import db as dbmod
        return (dbmod.get_db().get_config("keystore_alias") or "").strip()
    except Exception:
        return ""


def _ws_to_http(ws: str) -> str:
    """ws://ip:port/ws -> http://ip:port ; wss:// -> https://. Empty allowed."""
    if ws.startswith("ws://"):
        return ws.replace("ws://", "http://", 1).rstrip("/")
    if ws.startswith("wss://"):
        return ws.replace("wss://", "https://", 1).rstrip("/")
    # something like 192.168.1.2:8920 or already http(s)://
    if "://" in ws:
        return ws.rstrip("/")
    return ws.rstrip("/")


def _ws_to_http_tls(ws: str) -> str:
    """ws://ip:port/ws -> https://ip:port (same port, HTTPS scheme)."""
    base = _ws_to_http(ws)
    if "://" not in base:
        return ""
    return base.replace("http://", "https://", 1).replace("ws://", "wss://", 1)


# --------------------------------------------------------------- job tracking
class _Job:
    def __init__(self, target: str):
        self.id = f"{target}-{int(time.time())}"
        self.target = target
        self.status = "queued"      # queued | running | done | failed
        self.stage = "排队中"
        self.progress = 0
        self.error = ""
        self.started_at = int(time.time())
        self.artifact = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target": self.target,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "started_at": self.started_at,
            "artifact": self.artifact or None,
        }


_jobs: dict[str, _Job] = {}
_current: _Job | None = None
_lock = threading.Lock()
_shutdown_flag = False


def jobs_snapshot() -> dict:
    with _lock:
        cur = _current.to_dict() if _current else None
        history = [j.to_dict() for j in _jobs.values()]
    return {"current": cur, "jobs": history}


# --------------------------------------------------------------- artifact paths
def _artifact_name(target: str, version: str) -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    if target == "android":
        return f"KiteChat-{version}-{ts}.apk"
    if target == "windows":
        return f"KiteChat-Windows-{version}-{ts}.exe"
    return f"KiteChat-{version}-{ts}.bin"


def _exports_dir() -> str:
    return cfg.EXPORT_DIR


def _android_exports_dir() -> str:
    return os.path.join(cfg.EXPORT_DIR, "android")


def _newest_in(directory: str, prefix: str) -> str:
    """Newest file matching prefix (lexicographic on ts-suffixed filename)."""
    if not os.path.isdir(directory):
        return ""
    best, best_ts = "", -1
    for f in os.listdir(directory):
        if not f.startswith(prefix) or not f.endswith(".apk"):
            continue
        m = re.search(r"-(\d{14})\.apk$", f)
        ts = int(m.group(1)) if m else -1
        if ts > best_ts:
            best_ts, best = ts, os.path.join(directory, f)
    return best


def artifact_path(target: str) -> str:
    if target == "android":
        return _newest_in(_android_exports_dir(), "KiteChat-")
    if target == "windows":
        d = _exports_dir()
        best, best_ts = "", -1
        if os.path.isdir(d):
            for f in os.listdir(d):
                if not f.startswith("KiteChat-Windows-") or not f.endswith(".exe"):
                    continue
                m = re.search(r"-(\d{14})\.exe$", f)
                ts = int(m.group(1)) if m else -1
                if ts > best_ts:
                    best_ts, best = ts, os.path.join(d, f)
        return best
    return ""


def latest_apk_manifest() -> dict | None:
    path = artifact_path("android")
    if not path:
        return None
    m = os.path.basename(path)
    ver_m = re.search(r"KiteChat-([\d.]+)-\d{14}\.apk$", m)
    version = ver_m.group(1) if ver_m else cfg.VERSION
    return {
        "version": version,
        "filename": m,
        "size": os.path.getsize(path),
        "ts": int(os.path.getmtime(path)),
    }


# --------------------------------------------------------------- actual builds
def _pyinstaller() -> list[str]:
    """Command prefix to run PyInstaller. Prefer the active venv's module
    ('python -m PyInstaller') because uv-created trampoline scripts fail with
    'uv trampoline failed to canonicalize script path'."""
    py = sys.executable
    pref = getattr(sys, "prefix", "") or ""
    if py:
        mod = os.path.join(pref, "Lib", "site-packages", "PyInstaller")
        if os.path.isdir(mod):
            return [py, "-m", "PyInstaller"]
        # last resort: try a one-dir-up module check
        mod2 = os.path.join(os.path.dirname(os.path.dirname(py)),
                            "Lib", "site-packages", "PyInstaller")
        if os.path.isdir(mod2):
            return [py, "-m", "PyInstaller"]
    w = shutil.which("pyinstaller")
    if w:
        return [w]
    return []


def _build_windows(job: _Job, ws: str, version: str, scheme: str, app_name: str):
    pyi = _pyinstaller()
    if not pyi:
        job.status = "failed"
        job.error = "未找到 pyinstaller，请先安装：pip install pyinstaller"
        return
    job.stage = "构建 Windows EXE"
    job.progress = 10

    server_url = (f"https://{_ws_to_http_tls(ws)}" if scheme == "https"
                  else f"http://{_ws_to_http(ws)}")
    # normalize: _ws_to_http already includes scheme
    if "://" in _ws_to_http(ws):
        http_base = _ws_to_http(ws)
    else:
        http_base = (("https://" if scheme == "https" else "http://")
                     + _ws_to_http(ws))

    # desktop package dir = client/desktop ; web assets = client/web
    pkg_dir = cfg.DESKTOP_DIR
    web_dir = cfg.CLIENT_WEB_DIR
    build_dir = os.path.join(cfg.ROOT, "build", "exe-dist")
    os.makedirs(build_dir, exist_ok=True)

    spec = os.path.join(pkg_dir, "kitechat.spec")
    # Ensure spec references the web dir relative to the repo.
    spec_data = _desktop_spec_text(cfg.ROOT, app_name)
    tmp_spec = os.path.join(build_dir, "kitechat.spec")
    with open(tmp_spec, "w", encoding="utf-8") as f:
        f.write(spec_data)

    # Inject config.bin into the staging web dir that pyinstaller bundles.
    # We copy client/web -> build/<jobid>_web and write config.bin there.
    stage_web = os.path.join(build_dir, job.id + "_web")
    shutil.rmtree(stage_web, ignore_errors=True)
    shutil.copytree(web_dir, stage_web)
    payload = {"ws_address": ws, "server_url": http_base,
               "app_name": app_name, "version": version,
               "embed_version": True}
    _obfuscate_into_bin(payload, os.path.join(stage_web, "config.bin"))

    outdir = os.path.join(build_dir, job.id + "_out")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)

    job.progress = 30
    cmd = [
        *pyi, "--noconfirm", "--clean", "--onefile",
        "--name", app_name,
        "--distpath", outdir,
        "--workpath", os.path.join(build_dir, job.id + "_work"),
        "--specpath", build_dir,
        "--add-data", f"{stage_web};web",
        os.path.join(pkg_dir, "main.py"),
    ]
    job.progress = 40
    res = _run_job_cmd(job, cmd, cwd=pkg_dir)
    if res != 0:
        return

    exe = os.path.join(outdir, app_name + ".exe")
    if os.path.exists(exe):
        target = os.path.join(_exports_dir(),
                              _artifact_name("windows", version))
        shutil.copy2(exe, target)
        job.artifact = target
        job.stage = "完成"
        job.progress = 100
        job.status = "done"
    else:
        job.status = "failed"
        job.error = f"EXE 未生成: {exe}"


def _gradlew(android_dir: str) -> str:
    """Platform-aware gradlew wrapper: gradlew.bat on Windows, gradlew elsewhere."""
    if _is_windows():
        return os.path.join(android_dir, "gradlew.bat")
    return os.path.join(android_dir, "gradlew")


def _apksigner(sdk: str) -> str:
    """Locate apksigner, platform-aware (apksigner.bat on Windows, apksigner elsewhere)."""
    if not sdk:
        return ""
    name = "apksigner.bat" if _is_windows() else "apksigner"
    for bt in ("35.0.0", "34.0.0", "33.0.0"):
        cand = os.path.join(sdk, "build-tools", bt, name)
        if os.path.exists(cand):
            return cand
    return shutil.which("apksigner") or ""


def _build_android(job: _Job, ws: str, version: str, scheme: str,
                   app_name: str, ca_cert: str):
    android_dir = cfg.ANDROID_DIR
    ok, sdk_msg = _ensure_android_config(android_dir)
    if not ok:
        job.status = "failed"
        job.error = sdk_msg
        return

    job.stage = "准备 Android 构建"
    job.progress = 5

    # backup & restore app/build.gradle versionName so we don't touch git tree
    app_build = os.path.join(android_dir, "app", "build.gradle")
    with open(app_build, "rb") as f:
        orig_build = f.read()
    # operate on text; preserve exact original bytes on restore
    text = orig_build.decode("utf-8")
    new_text = re.sub(r'versionName "[^"]*"', f'versionName "{version}"', text)
    new_text = re.sub(r"versionCode \d+", f"versionCode {int(version.replace('.', '') or '1')}", new_text)
    with open(app_build, "wb") as f:
        f.write(new_text.encode("utf-8"))

    # inject config.bin into android assets/web
    assets_web = os.path.join(android_dir, "app", "src", "main", "assets", "web")
    http_base = (("https://" if scheme == "https" else "http://") + _ws_to_http(ws)) \
        if "://" not in _ws_to_http(ws) else _ws_to_http(ws)
    payload = {"ws_address": ws, "server_url": http_base,
               "app_name": app_name, "version": version, "embed_version": True}
    if ca_cert:
        payload["ca_cert"] = ca_cert
    _obfuscate_into_bin(payload, os.path.join(assets_web, "config.bin"))

    # decide gradle: prefer wrapper
    gradle = _gradlew(android_dir)
    jdk = _find_jdk()
    env = dict(os.environ)
    if jdk:
        env["JAVA_HOME"] = jdk
    env.setdefault("ANDROID_HOME", _find_sdk())

    if not _is_windows():
        # gradlew shell script needs the exec bit on Linux
        try:
            os.chmod(gradle, os.stat(gradle).st_mode | 0o111)
        except OSError:
            pass

    job.stage = "Gradle assembleRelease"
    job.progress = 15
    cmd = [gradle, "--no-daemon", "-q", "assembleRelease"]
    res = _run_job_cmd(job, cmd, cwd=android_dir, env=env)

    # restore build.gradle regardless of outcome (exact original bytes)
    with open(app_build, "wb") as f:
        f.write(orig_build)

    if res != 0:
        return

    apk_cand = os.path.join(android_dir, "app", "build", "outputs",
                            "apk", "release", "app-release.apk")
    if not os.path.exists(apk_cand):
        apk_cand = os.path.join(android_dir, "app", "build", "outputs",
                                "apk", "release", "app-release-unsigned.apk")
    if not os.path.exists(apk_cand):
        job.status = "failed"
        job.error = "未能定位生成的 APK"
        return

    job.progress = 70
    job.stage = "签名 APK"
    signed = _sign_apk(job, apk_cand)
    if not signed:
        return

    out_name = _artifact_name("android", version)
    os.makedirs(_android_exports_dir(), exist_ok=True)
    target = os.path.join(_android_exports_dir(), out_name)
    shutil.copy2(signed, target)
    # idsig (apksigner v2/v3 v4 signature file) if produced
    idsig = signed + ".idsig"
    if os.path.exists(idsig):
        shutil.copy2(idsig, target + ".idsig")
    job.artifact = target
    job.progress = 100
    job.stage = "完成"
    job.status = "done"
    _write_latest_manifest(version, out_name)


def _sign_apk(job: _Job, apk: str) -> str:
    sdk = _find_sdk()
    keystore = os.path.join(cfg.DATA_DIR, "kitechat-release.jks")
    if not sdk or not os.path.exists(keystore):
        job.status = "failed"
        job.error = "缺少 SDK 或签名密钥 (data/kitechat-release.jks)"
        return ""
    passw = _keystore_pass()
    alias = _keystore_alias()
    if not passw or not alias:
        job.status = "failed"
        job.error = "缺少签名密码/别名，请设置 KITECHAT_KEYSTORE_PASS / KITECHAT_KEYSTORE_ALIAS"
        return ""
    apksigner = _apksigner(sdk)
    if not apksigner:
        job.status = "failed"
        job.error = "未找到 apksigner (build-tools)，请安装 build-tools;35.0.0"
        return ""
    signed = apk + ".kitesigned.apk"
    cmd = [
        apksigner, "sign", "--ks", keystore,
        "--ks-pass", f"pass:{passw}",
        "--ks-key-alias", alias,
        "--out", signed, apk,
    ]
    env = dict(os.environ)
    if _find_jdk():
        env["JAVA_HOME"] = _find_jdk()
    res = _run_job_cmd(job, cmd, cwd=os.path.dirname(apk), env=env)
    if res != 0 or not os.path.exists(signed):
        return ""
    return signed


def _write_latest_manifest(version: str, filename: str) -> None:
    d = _android_exports_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, filename)
    manifest = {
        "version": version,
        "filename": filename,
        "size": os.path.getsize(path),
        "ts": int(time.time()),
    }
    with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


# --------------------------------------------------------------- helpers
def _run_job_cmd(job: _Job, cmd: list[str], cwd: str,
                 env: dict | None = None) -> int:
    """Run cmd capturing output into job stage; returns exit code."""
    job.stage = "运行: " + " ".join(os.path.basename(str(c)) for c in cmd[:1])
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        **kwargs,
    )
    lines = []
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        if len(lines) > 30:
            lines = lines[-30:]
    proc.wait()
    if proc.returncode != 0:
        job.status = "failed"
        job.error = "\n".join(lines[-8:]) or f"退出码 {proc.returncode}"
        return proc.returncode
    return 0


def start_build(target: str, ws: str, version: str, scheme: str,
                ca_cert_path: str = "") -> dict:
    """Kick off an export job in a background thread. Returns the job dict."""
    global _current
    if target not in ("windows", "android", "all"):
        raise ValueError("target 必须是 windows / android / all")
    app_name = _app_name()
    ca_cert = ""
    if ca_cert_path and os.path.exists(ca_cert_path):
        with open(ca_cert_path, "r", encoding="utf-8") as f:
            ca_cert = f.read().strip()

    targets = ["android", "windows"] if target == "all" else [target]
    jobs = []
    with _lock:
        for t in targets:
            j = _Job(t)
            j.target = t
            _jobs[j.id] = j
            _current = j
            jobs.append(j)
        _current = jobs[0]

    def runner():
        try:
            for j in jobs:
                with _lock:
                    _current = j
                j.status = "running"
                if j.target == "windows":
                    _build_windows(j, ws, version, scheme, app_name)
                elif j.target == "android":
                    _build_android(j, ws, version, scheme, app_name, ca_cert)
                elif j.target == "ios":
                    _build_ios(j, ws, version, scheme, app_name, ca_cert)
                elif j.target == "macos":
                    _build_macos(j, ws, version, scheme, app_name, ca_cert)
                if j.status == "running":
                    j.status = "failed"
                    j.error = "构建未完成"
        except Exception as e:  # pragma: no cover - defensive
            for j in jobs:
                if j.status != "done":
                    j.status = "failed"
                    j.error = str(e)

    threading.Thread(target=runner, daemon=True).start()
    return jobs[0].to_dict()


def shutdown() -> None:
    global _shutdown_flag
    _shutdown_flag = True


def _app_name() -> str:
    try:
        from . import db as dbmod
        return (dbmod.get_db().get_config("app_name") or cfg.APP_NAME).strip()
    except Exception:
        return cfg.APP_NAME


# --------------------------------------------------------------- spec templates
def _desktop_spec_text(root: str, app_name: str) -> str:
    """Build a pyinstaller spec that bundles client/web as 'web' with 2.x API."""
    main_py = os.path.join(root, "client", "desktop", "main.py").replace("\\", "/")
    desktop_dir = os.path.join(root, "client", "desktop").replace("\\", "/")
    web_dir = os.path.join(root, "client", "web").replace("\\", "/")
    return f"""# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['{main_py}'],
    pathex=['{desktop_dir}'],
    datas=[('{web_dir}', 'web')],
    hiddenimports=['webview', 'webview.platforms.edgechromium'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='{app_name}',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False, icon=None,
)
"""


# -------------------------------------------------------------- Apple tools
def _find_xcodebuild() -> str:
    """Auto-detect xcodebuild (macOS only)."""
    for p in ("/usr/bin/xcodebuild",
              "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild"):
        if os.path.isfile(p):
            return p
    try:
        out = subprocess.check_output(["which", "xcodebuild"], text=True, timeout=5).strip()
        return out if out else ""
    except Exception:
        return ""


def _find_hdiutil() -> str:
    """Auto-detect hdiutil (macOS only, for DMG creation)."""
    for p in ("/usr/bin/hdiutil", "/sbin/hdiutil"):
        if os.path.isfile(p):
            return p
    try:
        out = subprocess.check_output(["which", "hdiutil"], text=True, timeout=5).strip()
        return out if out else ""
    except Exception:
        return ""


# ------------------------------------------------------------- Apple builds
def _build_ios(job: _Job, ws: str, version: str, scheme: str, app_name: str,
               ca_cert: str = "") -> None:
    """Build iOS IPA via xcodebuild (requires macOS + Xcode)."""
    xcodebuild = _find_xcodebuild()
    if not xcodebuild:
        job.status = "failed"
        job.error = "xcodebuild not found. iOS builds require macOS with Xcode."
        return

    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    web_dir = os.path.join(cfg.ROOT, "client", "web")
    _obfuscate_into_bin({"ws_address": ws, "server_url": server_url, "app_name": app_name, "version": version},
                        os.path.join(web_dir, "config.bin"))
    # Also copy to iOS asset dir if it exists
    ios_dir = os.path.join(cfg.ROOT, "client", "ios", "KiteChat")
    if os.path.isdir(ios_dir):
        _obfuscate_into_bin({"ws_address": ws, "server_url": server_url, "app_name": app_name, "version": version},
                            os.path.join(ios_dir, "config.bin"))

    out_dir = os.path.join(cfg.ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    archive_path = os.path.join(out_dir, "KiteChat.xcarchive")

    job.progress = 20
    job.log.append("Archiving iOS app...")
    proc = subprocess.Popen(
        [xcodebuild, "archive",
         "-scheme", "KiteChat",
         "-project", os.path.join(ios_dir, "KiteChat.xcodeproj"),
         "-archivePath", archive_path,
         "-configuration", "Release"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if line.strip():
            job.log.append(line.strip())
    proc.wait()
    if proc.returncode != 0:
        job.status = "failed"
        job.error = "xcodebuild archive failed"
        return

    job.progress = 60
    job.log.append("Exporting IPA...")
    export_plist = os.path.join(out_dir, "ExportOptions.plist")
    with open(export_plist, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>method</key><string>ad-hoc</string>\n'
                '  <key>stripSwiftSymbols</key><true/>\n'
                '  <key>compileBitcode</key><false/>\n'
                '</dict></plist>\n')
    ipa_out = os.path.join(out_dir, f"KiteChat-{version}-{int(time.time())}.ipa")
    proc = subprocess.Popen(
        [xcodebuild, "-exportArchive",
         "-archivePath", archive_path,
         "-exportOptionsPlist", export_plist,
         "-exportPath", out_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if line.strip():
            job.log.append(line.strip())
    proc.wait()
    exported_ipa = os.path.join(out_dir, "KiteChat.ipa")
    if os.path.isfile(exported_ipa):
        os.rename(exported_ipa, ipa_out)
    job.progress = 100
    job.log.append(f"IPA: {ipa_out}")


def _build_macos(job: _Job, ws: str, version: str, scheme: str, app_name: str,
                 ca_cert: str = "") -> None:
    """Build macOS DMG via xcodebuild + hdiutil (requires macOS + Xcode)."""
    xcodebuild = _find_xcodebuild()
    if not xcodebuild:
        job.status = "failed"
        job.error = "xcodebuild not found. macOS builds require macOS with Xcode."
        return

    ws_address = ws.replace("ws://", f"{scheme}://").replace("wss://", f"{scheme}://")
    server_url = ws_address.rsplit("/ws", 1)[0] if "/ws" in ws_address else ws_address
    web_dir = os.path.join(cfg.ROOT, "client", "web")
    _obfuscate_into_bin({"ws_address": ws, "server_url": server_url, "app_name": app_name, "version": version},
                        os.path.join(web_dir, "config.bin"))
    macos_dir = os.path.join(cfg.ROOT, "client", "macos", "KiteChat")
    if os.path.isdir(macos_dir):
        _obfuscate_into_bin({"ws_address": ws, "server_url": server_url, "app_name": app_name, "version": version},
                            os.path.join(macos_dir, "config.bin"))

    out_dir = os.path.join(cfg.ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    archive_path = os.path.join(out_dir, "KiteChat-macOS.xcarchive")
    app_path = os.path.join(out_dir, "KiteChat.app")

    job.progress = 20
    job.log.append("Building macOS app...")
    proc = subprocess.Popen(
        [xcodebuild, "archive",
         "-scheme", "KiteChat-macOS",
         "-project", os.path.join(macos_dir, "KiteChat.xcodeproj"),
         "-archivePath", archive_path,
         "-configuration", "Release"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if line.strip():
            job.log.append(line.strip())
    proc.wait()
    if proc.returncode != 0:
        job.status = "failed"
        job.error = "xcodebuild archive failed"
        return

    job.progress = 50
    job.log.append("Exporting .app...")
    proc = subprocess.Popen(
        [xcodebuild, "-exportArchive",
         "-archivePath", archive_path,
         "-exportOptionsPlist", os.path.join(macos_dir, "ExportOptions.plist"),
         "-exportPath", out_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if line.strip():
            job.log.append(line.strip())
    proc.wait()
    if not os.path.isdir(app_path):
        job.status = "failed"
        job.error = ".app not found after export"
        return

    job.progress = 70
    hdiutil = _find_hdiutil()
    if hdiutil:
        dmg_path = os.path.join(out_dir, f"KiteChat-macOS-{version}-{int(time.time())}.dmg")
        job.log.append("Creating DMG...")
        proc = subprocess.Popen(
            [hdiutil, "create", "-volname", "KiteChat",
             "-srcfolder", app_path, "-ov", "-format", "UDZO", dmg_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        proc.wait()
        job.progress = 100
        job.log.append(f"DMG: {dmg_path}")
    else:
        import zipfile
        zip_path = os.path.join(out_dir, f"KiteChat-macOS-{version}-{int(time.time())}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(app_path):
                for f in files:
                    fp = os.path.join(root, f)
                    zf.write(fp, os.path.relpath(fp, os.path.dirname(app_path)))
        job.progress = 100
        job.log.append(f"ZIP (no hdiutil): {zip_path}")
