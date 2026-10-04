"""生成 QQ 机器人**帮助图**（站点的 ``/help.jpg``）。

图上有 **19 条命令**，而命令是精确匹配的——写错一个字，群友照着打就是毫无反应。
所以文字由代码排版（**一个字都不会错**、也不会漏项），配色 / 圆角 / 字体照站点的
CSS 变量来，出图与站点是一套观感。

用法（Pillow 只在「生成」时需要，不进入运行依赖）：

    uv run --with pillow python tools/make_help_card.py
    uv run --with pillow python tools/make_help_card.py --out /tmp/help.jpg

产物默认写进 ``static/help.jpg``；换图**地址不变**，插件侧不用改任何配置
（`比赛帮助` 会先探一下这张图在不在，在就当场发图）。

出图时顺手在它旁边写一个 ``help.jpg.src.sha256``（源文件指纹）：图没有版本号，
只能靠这个认「新旧」——``tools/check_assets.py`` 与 ``tests/test_plugin_helpers.py``
据此拦住「改了文案忘了重画」（那意味着群里发着一张写着旧命令的图）。

**文字不在这个文件里**：全部来自 :mod:`help_card_content`（零依赖，测试直接读它，
比对「图上的命令 == 插件 HELP_TEXT 的命令」）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

# 同目录的两个模块：`python tools/make_help_card.py` 会把脚本所在目录放进 sys.path，
# 所以直接按名字导入即可（不需要 sys.path 那套补丁）。同一套画法（字体查找 / 颜色 /
# 渐变）与分享图共用，别再抄一份。
from help_card_content import (
    FOOTER_NOTE,
    FOOTER_TITLE,
    NOTICE_NOTE,
    NOTICE_TITLE,
    SECTION_NOTES,
    SECTIONS,
    SUBTITLE,
    TIPS,
    TITLE,
    TITLE_LABEL,
    source_digest,
)
from make_share_card import (
    _CJK_CANDIDATES,
    _LATIN_CANDIDATES,
    ACCENT,
    ACCENT_2,
    DIM,
    TXT,
    _font,
    _lerp,
)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W = 1080
PAD = 72  # 左右安全边距
BOTTOM_PAD = 48
#: 先画到一张够高的画布上，最后按实际用掉的高度裁掉——省掉一堆高度计算
CANVAS_H = 4200

INK = (4, 7, 12)  # 铺在强调色上的深色字（与站点 ::selection 同色）
PANEL = (16, 23, 37)  # --panel
LINE = (34, 224, 232, 41)  # 描边：rgba(34,224,232,.16)

ROW_H = 54  # 一行命令的高度（说明跟在后面）
DESC_H = 34  # 说明被折到下一行时，每行的高度


# --------------------------------------------------------------------------- #
# 画布与基础件
# --------------------------------------------------------------------------- #
def background() -> Image.Image:
    """深色纵向渐变 + 网格 + 扫描线（与分享图同一套底）。"""
    img = Image.new("RGB", (W, CANVAS_H))
    draw = ImageDraw.Draw(img)
    top, bottom = (13, 20, 32), (7, 9, 15)
    for y in range(CANVAS_H):
        draw.line([(0, y), (W, y)], fill=_lerp(top, bottom, min(1.0, y / 1600)))

    overlay = Image.new("RGBA", (W, CANVAS_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(0, W + 60, 60):
        od.line([(x, 0), (x, CANVAS_H)], fill=(*ACCENT, 11), width=1)
    for y in range(0, CANVAS_H + 60, 60):
        od.line([(0, y), (W, y)], fill=(*ACCENT, 11), width=1)
    for y in range(0, CANVAS_H, 3):
        od.line([(0, y), (W, y)], fill=(0, 0, 0, 12))

    glow = Image.new("RGBA", (W, CANVAS_H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-300, -380, 820, 420], fill=(*ACCENT, 40))
    overlay = Image.alpha_composite(overlay, glow.filter(ImageFilter.GaussianBlur(110)))
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def gradient_bar(img: Image.Image, xy: tuple[int, int], size: tuple[int, int]) -> None:
    """青→品红的小渐变条（标题下 / 页脚上）。"""
    x, y = xy
    w, h = size
    bar = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for i in range(w):
        bd.line([(i, 0), (i, h)], fill=(*_lerp(ACCENT, ACCENT_2, i / max(1, w - 1)), 255))
    img.alpha_composite(bar, (x, y))


def panel(
    img: Image.Image, box: tuple[int, int, int, int], *, cut: int = 20, radius: int = 10
) -> None:
    """站点那种「左上切角 + 其余小圆角」的面板：填充 + 极淡青边 + 顶部高光。"""
    x0, y0, x1, y1 = box
    pts = [
        (x0 + cut, y0),
        (x1 - radius, y0),
        (x1, y0 + radius),
        (x1, y1 - radius),
        (x1 - radius, y1),
        (x0 + radius, y1),
        (x0, y1 - radius),
        (x0, y0 + cut),
    ]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon(pts, fill=(*PANEL, 245))
    draw.line(pts + [pts[0]], fill=LINE, width=1, joint="curve")
    draw.line(
        [(x0 + cut + 2, y0 + 1), (x1 - radius - 2, y0 + 1)], fill=(186, 224, 255, 16), width=1
    )
    img.alpha_composite(layer)


def tab(img: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont) -> int:
    """强调色的分组标题条（① 看比赛 …）；返回它的高度。"""
    x, y = xy
    pad_x, pad_y = 18, 9
    w = int(font.getlength(text)) + pad_x * 2
    h = font.size + pad_y * 2
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon(
        [
            (x + 12, y),
            (x + w, y),
            (x + w, y + h - 10),
            (x + w - 10, y + h),
            (x, y + h),
            (x, y + 12),
        ],
        fill=(*ACCENT, 255),
    )
    img.alpha_composite(layer)
    ImageDraw.Draw(img).text((x + pad_x, y + pad_y - 1), text, font=font, fill=INK)
    return h


def _tokens(text: str) -> list[str]:
    """按「中文逐字、西文成串」切词——中文没有空格，只能逐字换行。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_/+."):
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    return out


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """把一段文字折成若干行（中文逐字断，西文词尽量不切）。"""
    lines: list[str] = []
    cur = ""
    for tok in _tokens(text):
        if cur and font.getlength(cur + tok) > max_width:
            lines.append(cur)
            cur = tok
        else:
            cur += tok
    if cur:
        lines.append(cur)
    return lines or [""]


# --------------------------------------------------------------------------- #
# 版面
# --------------------------------------------------------------------------- #
def render() -> Image.Image:
    img = background()
    f_label = _font(_LATIN_CANDIDATES, 22)
    f_title = _font(_CJK_CANDIDATES, 66)
    f_sub = _font(_CJK_CANDIDATES, 25)
    f_notice = _font(_CJK_CANDIDATES, 25)
    f_small = _font(_CJK_CANDIDATES, 21)
    f_tab = _font(_CJK_CANDIDATES, 28)
    f_cmd = _font(_CJK_CANDIDATES, 25)
    f_desc = _font(_CJK_CANDIDATES, 23)
    f_tip_h = _font(_CJK_CANDIDATES, 26)
    f_tip = _font(_CJK_CANDIDATES, 22)
    f_foot = _font(_CJK_CANDIDATES, 26)
    draw = ImageDraw.Draw(img)

    # —— 标题区 ——
    y = 76
    cx = PAD
    for ch in TITLE_LABEL:
        draw.text((cx, y), ch, font=f_label, fill=(*ACCENT, 255))
        cx += f_label.getlength(ch) + 4
    y += 40

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((PAD, y), TITLE, font=f_title, fill=(*ACCENT, 210))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(24)))
    draw.text((PAD, y), TITLE, font=f_title, fill=TXT)
    y += 94
    gradient_bar(img, (PAD, y), (360, 5))
    y += 22
    draw.text((PAD, y), SUBTITLE, font=f_sub, fill=DIM)
    y += 62

    # —— 醒目提示条 ——
    band_h = 104
    panel(img, (PAD, y, W - PAD, y + band_h), cut=18)
    draw.ellipse([PAD + 24, y + 26, PAD + 35, y + 37], fill=(*ACCENT, 255))
    draw.text((PAD + 50, y + 16), NOTICE_TITLE, font=f_notice, fill=TXT)
    draw.text((PAD + 50, y + 60), NOTICE_NOTE, font=f_small, fill=DIM)
    y += band_h + 28

    # —— 三张命令卡：**先排版再画面板**（折行会顶高卡片，先算好才不会被顶出框）——
    cmd_x = PAD + 44
    for title, rows in SECTIONS:
        y += tab(img, (PAD, y), title, f_tab) + 16

        laid: list[tuple[str, list[str], int]] = []
        for cmd, desc in rows:
            text = f"—— {desc}"
            room = W - PAD - 24 - (cmd_x + f_cmd.getlength(cmd) + 14)
            if f_desc.getlength(text) <= room:
                laid.append((cmd, [text], ROW_H))
            else:
                # 说明太长就折到下一行（缩进在命令下面），行高按折了几行算
                parts = wrap(text, f_desc, W - PAD * 2 - 66)
                laid.append((cmd, parts, 34 + DESC_H * len(parts) + 10))
        # 分组小字也**先折行**：它是单行硬画的（这里以前直接画一行），说明一写长就冲出卡片
        # 右边缘——而这类权限说明恰恰容易写长（「谁创建的谁可以召集」）。
        note = SECTION_NOTES.get(title)
        note_lines = wrap(note, f_small, W - PAD * 2 - 66) if note else []
        body_h = 20 + sum(h for _c, _p, h in laid) + (DESC_H * len(note_lines)) + 12
        panel(img, (PAD, y, W - PAD, y + body_h), cut=20)

        ry = y + 20
        for cmd, parts, height in laid:
            draw.ellipse([PAD + 24, ry + 12, PAD + 31, ry + 19], fill=(*ACCENT, 255))
            draw.text((cmd_x, ry), cmd, font=f_cmd, fill=(*ACCENT, 255))
            if len(parts) == 1:
                draw.text(
                    (cmd_x + f_cmd.getlength(cmd) + 14, ry + 2), parts[0], font=f_desc, fill=DIM
                )
            else:
                ty = ry + 34
                for part in parts:
                    draw.text((cmd_x + 22, ty), part, font=f_desc, fill=DIM)
                    ty += DESC_H
            ry += height
        for i, part in enumerate(note_lines):
            draw.text((cmd_x + 22, ry + 6 + i * DESC_H), part, font=f_small, fill=(*ACCENT_2, 255))
        y += body_h + 24

    # —— 两张小卡（并排）——
    tip_w = (W - PAD * 2 - 24) // 2
    tip_top = y
    bodies: list[list[list[str]]] = []
    tip_h = 0
    for _title, lines in TIPS:
        body = [wrap(line, f_tip, tip_w - 44) for line in lines]
        bodies.append(body)
        tip_h = max(tip_h, 60 + sum(len(parts) * 31 for parts in body) + 16)
    for i, (title, _lines) in enumerate(TIPS):
        x0 = PAD + i * (tip_w + 24)
        panel(img, (x0, tip_top, x0 + tip_w, tip_top + tip_h), cut=18)
        draw.text((x0 + 22, tip_top + 16), title, font=f_tip_h, fill=(*ACCENT, 255))
        ty = tip_top + 58
        for parts in bodies[i]:
            for part in parts:
                draw.text((x0 + 22, ty), part, font=f_tip, fill=DIM)
                ty += 31
    y = tip_top + tip_h + 28

    # —— 页脚 ——
    gradient_bar(img, (PAD, y), (W - PAD * 2, 3))
    y += 22
    draw.text((PAD, y), FOOTER_TITLE, font=f_foot, fill=TXT)
    y += 42
    draw.text((PAD, y), FOOTER_NOTE, font=f_small, fill=DIM)
    y += 50

    return img.crop((0, 0, W, min(CANVAS_H, y + BOTTOM_PAD)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 QQ 机器人帮助图（站点 /help.jpg）",
        epilog=(
            "产物默认写进 static/help.jpg：站点会在 /help.jpg 提供它，插件「比赛帮助」先探一下\n"
            "在不在——在就当场发图，没放图则回私聊文字说明（不会发破图）。换图地址不变。\n"
            "\n"
            "图上的文字在 tools/help_card_content.py，排版（配色 / 圆角 / 字体）照站点 CSS；\n"
            "改了命令重跑一次即可——测试 test_help_card_lists_exactly_the_same_commands\n"
            "会比对「图上的命令」与插件 HELP_TEXT，两边漂了直接红。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", default="static/help.jpg", help="输出路径（默认 static/help.jpg）"
    )
    parser.add_argument("--quality", type=int, default=92, help="JPEG 质量（默认 92）")
    args = parser.parse_args()

    img = render().convert("RGB")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="JPEG", quality=args.quality, optimize=True, progressive=True)
    # 指纹写在图**旁边**（static/help.jpg.src.sha256）：自检与用例据此判断
    # 「图是不是比文案旧」——改了文案忘重画，群里就是一张写着旧命令的图。
    stamp = out.with_name(out.name + ".src.sha256")
    stamp.write_text(source_digest() + "\n", encoding="utf-8")
    total = sum(len(rows) for _t, rows in SECTIONS)
    print(f"已生成 {out}（{img.width}×{img.height}，{total} 条命令，{out.stat().st_size // 1024} KB）")
    print(f"指纹已写入 {stamp}（tools/check_assets.py 与测试据此判断新旧）")


if __name__ == "__main__":
    main()
