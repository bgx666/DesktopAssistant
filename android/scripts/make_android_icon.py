"""从小助的 bubble_256.png 生成安卓启动图标（含 API 26+ 自适应图标）。

为什么不留着默认图标：默认是 Android 那个绿色机器人，用户一眼看不出这是小助。
素材直接用 PC 前端那份（frontend/assets/bubble_256.png），保证两端同一个形象。
"""
import sys
from pathlib import Path

from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

SRC = Path(r"D:\xiaob\planner\frontend\assets\bubble_256.png")
RES = Path(r"D:\xiaob\planner\android\app\src\main\res")

# 传统图标（每档 48dp 基准）
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# 自适应图标：前景/背景各 108dp
ADAPTIVE = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}

# 自适应图标的前景安全区：内容只占中间 66/108（约 61%），外面会被各种形状裁掉。
# 取 0.60 略小于安全区，四角/圆形裁切都不会切到球。
FOREGROUND_RATIO = 0.60


def main() -> int:
    art = Image.open(SRC).convert("RGBA")
    print(f"素材 {SRC.name} {art.size[0]}x{art.size[1]}")

    for density, size in LEGACY.items():
        out_dir = RES / f"mipmap-{density}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 传统图标：整幅球（本来就带透明边），圆形版直接用同一张（它本身是圆的）
        scaled = art.resize((size, size), Image.LANCZOS)
        scaled.save(out_dir / "ic_launcher.png")
        scaled.save(out_dir / "ic_launcher_round.png")
        print(f"  mipmap-{density}/ic_launcher.png (+round)  {size}x{size}")

    for density, canvas in ADAPTIVE.items():
        out_dir = RES / f"mipmap-{density}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 自适应前景：画布 108dp，球缩到 60% 居中，四周留透明
        fg = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        inner = max(1, int(canvas * FOREGROUND_RATIO))
        ball = art.resize((inner, inner), Image.LANCZOS)
        offset = (canvas - inner) // 2
        fg.paste(ball, (offset, offset), ball)
        fg.save(out_dir / "ic_launcher_foreground.png")
        print(f"  mipmap-{density}/ic_launcher_foreground.png  {canvas}x{canvas} (球 {inner}px)")

    # 自适应图标描述（API 26+）：浅色底 + 球
    anydpi = RES / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        (anydpi / name).write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<!--\n"
            "  自适应图标：浅色底 + 小助那个球（前景只占中间 60%，各厂商的圆形/方形/水滴裁切\n"
            "  都不会切到球）。素材来自 PC 前端 frontend/assets/bubble_256.png，两端同一个形象。\n"
            "  由 scripts/make_android_icon.py 生成，改图标就重跑它。\n"
            "-->\n"
            '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <background android:drawable="@color/ic_launcher_background" />\n'
            '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
            "</adaptive-icon>\n",
            encoding="utf-8",
        )
        print(f"  mipmap-anydpi-v26/{name}")

    colors = RES / "values" / "colors.xml"
    colors.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<resources>\n"
        "    <!-- 自适应图标的底色（浅到几乎白，衬托那个蓝色球） -->\n"
        '    <color name="ic_launcher_background">#EEF6FB</color>\n'
        "</resources>\n",
        encoding="utf-8",
    )
    print("  values/colors.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
