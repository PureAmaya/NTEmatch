"""生成默认分享图（og:image）。

站点的 HUD 风格（深色底 + 网格 + 切角括号 + 青/紫）**用代码画出来**，而不是塞一份
设计稿：这样改主色、改站名都能重新生成，仓库里也不会多一个二进制设计源文件。

用法（Pillow 只在「生成」时需要，不进入运行依赖）：

    uv run python tools/make_share_card.py
    uv run python tools/make_share_card.py --title "异环赛事" --tagline "S2 · 秋季赛"

产物：``static/og.png``（1200×630，绝大多数分享卡片都吃这个尺寸）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import 到仓库里的 app 包（``python tools/xxx.py`` 时 sys.path[0] 是 tools/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter

from app import fonts

W, H = 1200, 630
ACCENT = (34, 224, 232)
ACCENT_2 = (125, 92, 255)
DIM = (147, 167, 193)
TXT = (230, 240, 255)

# 字体不在这里写死：中文字体装在哪各发行版都不一样（Debian 是
# /usr/share/fonts/opentype/noto，RHEL 是 google-noto-cjk，Arch 是 noto-cjk…），
# 统一走 app/fonts 的四层查找（环境变量 → 常见路径 → 扫字体目录 → 内置兜底）。


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def background() -> Image.Image:
    """深色纵向渐变 + 网格 + 扫描线 + 左上角光晕。"""
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    top, bottom = (13, 20, 32), (7, 9, 15)
    for y in range(H):
        draw.line([(0, y), (W, y)], fill=_lerp(top, bottom, y / (H - 1)))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(0, W + 60, 60):
        od.line([(x, 0), (x, H)], fill=(*ACCENT, 13), width=1)
    for y in range(0, H + 60, 60):
        od.line([(0, y), (W, y)], fill=(*ACCENT, 13), width=1)
    for y in range(0, H, 3):  # 扫描线：让纯色底有「屏幕」的质感
        od.line([(0, y), (W, y)], fill=(0, 0, 0, 10))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-260, -320, 700, 380], fill=(*ACCENT, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    overlay = Image.alpha_composite(overlay, glow)
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _grad_ring(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, width: int,
               segs: int = 120, alpha: int = 255) -> None:
    """用一串小圆弧拼出「青→紫→青」的渐变环（Pillow 的 arc 不能直接吃渐变）。"""
    box = [cx - r, cy - r, cx + r, cy + r]
    for i in range(segs):
        start = i * 360 / segs
        end = start + 360 / segs + 0.8  # 轻微重叠，避免分段之间出现缝
        k = 1 - abs(2 * (i / segs) - 1)  # 三角波：两端青、中间紫
        draw.arc(box, start=start, end=end, fill=(*_lerp(ACCENT, ACCENT_2, k), alpha), width=width)


def brand_mark(img: Image.Image) -> None:
    """同心环 + 中心点（与页面顶栏那个 mark 同构）。"""
    cx, cy, r = 168, 296, 78
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    _grad_ring(draw, cx, cy, r, 7)
    _grad_ring(draw, cx, cy, round(r * 0.53), 5, alpha=200)
    dot = round(r * 0.145)
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=(*ACCENT, 255))

    halo = layer.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(halo)
    img.alpha_composite(layer)


def brackets(img: Image.Image) -> None:
    """四角的 L 形括号（页面里 .panel 的那种记号）。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    pad, size, width = 34, 46, 3
    color = (*ACCENT, 150)
    for x, y, dx, dy in (
        (pad, pad, 1, 1),
        (W - pad, pad, -1, 1),
        (pad, H - pad, 1, -1),
        (W - pad, H - pad, -1, -1),
    ):
        draw.line([(x, y), (x + size * dx, y)], fill=color, width=width)
        draw.line([(x, y), (x, y + size * dy)], fill=color, width=width)
    img.alpha_composite(layer)


def wedge(img: Image.Image) -> None:
    """右下角的紫色斜切：整张图的第二色只出现在这里，量少但点得住。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon([(W, H - 150), (W, H), (W - 190, H)], fill=(*ACCENT_2, 210))
    draw.polygon([(W, H - 178), (W, H - 150), (W - 28, H - 150)], fill=(*ACCENT_2, 90))
    img.alpha_composite(layer)


def text_block(img: Image.Image, title: str, tagline: str, note: str) -> None:
    f_title = fonts.load("cjk", 84)
    # 副标题默认是西文（NEVERNESS TO EVERNESS），用西文字体的等宽观感更好；
    # 但有人会传中文（`--tagline "S2 · 秋季赛"`），那时**必须换 CJK 字体**——
    # 西文字体画中文只会得到方块。
    f_tag = fonts.load("latin" if tagline.isascii() else "cjk", 27)
    # 说明文案默认是中文：必须用中文字体（西文字体在这里只会画出方框）
    f_note = fonts.load("cjk", 22)

    x, y = 316, 196
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((x, y), title, font=f_title, fill=(*ACCENT, 210))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))
    ImageDraw.Draw(img).text((x, y), title, font=f_title, fill=TXT)

    # 标题下的渐变小条：与品牌描边同一套颜色
    bar = Image.new("RGBA", (330, 4), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for i in range(330):
        bd.line([(i, 0), (i, 4)], fill=(*_lerp(ACCENT, ACCENT_2, i / 329), 235))
    img.alpha_composite(bar, (x, y + 112))

    # 标签逐个画：手动加字距，等宽感更接近页面里的 --font-mono
    cx = x + 2
    for ch in tagline:
        ImageDraw.Draw(img).text((cx, y + 142), ch, font=f_tag, fill=DIM)
        cx += f_tag.getlength(ch) + 5

    # 说明跟着副标题走（原来放在 470 太靠下，和上面的文字断成两块）
    ImageDraw.Draw(img).text((x, 402), note, font=f_note, fill=(*DIM, 255))


def main() -> None:
    parser = argparse.ArgumentParser(description="生成分享图 static/og.png")
    parser.add_argument("--title", default="NTE 比赛", help="大标题（默认 NTE 比赛）")
    parser.add_argument("--tagline", default="NEVERNESS TO EVERNESS", help="副标题（建议西文）")
    parser.add_argument("--note", default="自动分组 · 积分结算 · 实时排行 · 直播推流", help="左下角说明")
    parser.add_argument("--out", default="static/og.png", help="输出路径")
    args = parser.parse_args()

    img = background()
    # 斜切先画、括号后画：否则右下角那个括号会被色块盖住
    wedge(img)
    brand_mark(img)
    brackets(img)
    text_block(img, args.title, args.tagline, args.note)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, format="PNG", optimize=True)
    print(f"已生成 {out} ({out.stat().st_size // 1024} KB, {W}×{H})")


if __name__ == "__main__":
    main()
