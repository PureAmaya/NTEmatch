"""生成默认分享图（og:image）。

站点的 HUD 风格（深色底 + 网格 + 切角括号 + 青/品红）**用代码画出来**，而不是塞一份
设计稿：这样改主色、改站名都能重新生成，仓库里也不会多一个二进制设计源文件。

用法（Pillow 只在「生成」时需要，不进入运行依赖）：

    uv run --with pillow python tools/make_share_card.py
    uv run --with pillow python tools/make_share_card.py --title "异环赛事" --tagline "S2 · 秋季赛"

产物：``static/og.png``（1200×630，绝大多数分享卡片都吃这个尺寸）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 630
ACCENT = (34, 224, 232)
ACCENT_2 = (255, 47, 142)
DIM = (147, 167, 193)
TXT = (230, 240, 255)

# 站点字体栈里就是这几支：Bahnschrift 负责「科技感」的西文与数字，中文走微软雅黑。
_LATIN_CANDIDATES = (
    r"C:\Windows\Fonts\bahnschrift.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
)
_CJK_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)


def _font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    print(f"! 没有可用的字体，退回内置位图字体（观感会差）：{candidates[0]}")
    return ImageFont.load_default(size)


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


def brand_mark(img: Image.Image) -> None:
    """六边形 + 准星（与页面顶栏那个 mark 同构），描边做成青→品红的渐变。"""
    cx, cy, r = 168, 296, 78
    pts = [
        (cx, cy - r),
        (cx + r * 0.866, cy - r / 2),
        (cx + r * 0.866, cy + r / 2),
        (cx, cy + r),
        (cx - r * 0.866, cy + r / 2),
        (cx - r * 0.866, cy - r / 2),
    ]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    segs = list(zip(pts, pts[1:] + pts[:1]))
    for i, (a, b) in enumerate(segs):
        draw.line([a, b], fill=(*_lerp(ACCENT, ACCENT_2, i / (len(segs) - 1)), 255), width=7, joint="curve")
    inner = r * 0.42
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], outline=(*ACCENT, 255), width=5)
    draw.line([(cx - inner, cy), (cx + inner, cy)], fill=(*ACCENT, 190), width=3)
    draw.line([(cx, cy - inner), (cx, cy + inner)], fill=(*ACCENT, 190), width=3)

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
    """右下角的品红斜切：整张图的第二色只出现在这里，量少但点得住。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon([(W, H - 150), (W, H), (W - 190, H)], fill=(*ACCENT_2, 210))
    draw.polygon([(W, H - 178), (W, H - 150), (W - 28, H - 150)], fill=(*ACCENT_2, 90))
    img.alpha_composite(layer)


def text_block(img: Image.Image, title: str, tagline: str, note: str) -> None:
    f_title = _font(_CJK_CANDIDATES, 84)
    f_tag = _font(_LATIN_CANDIDATES, 27)
    # 说明文案默认是中文：必须用中文字体（西文字体在这里只会画出方框）
    f_note = _font(_CJK_CANDIDATES, 22)

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
