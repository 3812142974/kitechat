"""Generate app icons for all KiteChat targets from tools/kite-logo.png.

Outputs:
- client/web/favicon.ico            (16/32/48/64, webui + client favicon)
- client/desktop/app.ico            (16..256, PyInstaller EXE icon)
- client/android/app/src/main/res/mipmap-*/ic_launcher.png  (48..192)

Usage: .venv/Scripts/python.exe tools/make_icons.py
"""
import os
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tools", "kite-logo.png")


def main() -> None:
    img = Image.open(SRC).convert("RGBA")
    # center-crop to square in case the generated art isn't
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))

    # 1) web favicon (ico with common browser sizes)
    fav_dir = os.path.join(ROOT, "client", "web")
    img.save(os.path.join(fav_dir, "favicon.ico"),
             sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("web favicon.ico")

    # 2) windows exe icon
    desk_dir = os.path.join(ROOT, "client", "desktop")
    img.save(os.path.join(desk_dir, "app.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    print("desktop app.ico")

    # 3) android launcher icons (mipmap densities)
    res = os.path.join(ROOT, "client", "android", "app", "src", "main", "res")
    densities = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for dpi, size in densities.items():
        d = os.path.join(res, f"mipmap-{dpi}")
        os.makedirs(d, exist_ok=True)
        img.resize((size, size), Image.LANCZOS).save(
            os.path.join(d, "ic_launcher.png"))
        print(f"android mipmap-{dpi} {size}px")

    print("done")


if __name__ == "__main__":
    main()
