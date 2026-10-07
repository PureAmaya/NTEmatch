"""QQ 机器人**帮助图**（站点的 ``/help.jpg``）：**服务启动时自动重画一份**。

为什么要自己画而不是放一张图进仓库：图上的命令是**精确匹配**的，文案一改（加命令、
改说明），旧图就成了「照着打毫无反应」的陷阱；而图本身没有版本号（地址固定
``/help.jpg``）。所以让图跟着**内容模块**现画：内容改了，重启一次站点就是新的。
产物也不再入库（``static/help.jpg`` 与它的指纹文件都在 ``.gitignore`` 里）。

三条约定：

* **内容在** :mod:`app.helpcard_content`（纯数据、零依赖），排版在这里——测试比对
  「图上的命令」与插件 ``HELP_TEXT``，两边漂了直接红；
* **Pillow 是正式依赖**（与比赛卡片同一个）：正常情况下一直可用；万一这台机器上没装，
  就只记一行日志，插件那边会退回文字说明，功能一点不少（见 :func:`refresh`）；
* **画图跑在线程里**（:func:`refresh` 由启动流程用 ``asyncio.to_thread`` 调），
  不让它拖慢启动；画失败也只记日志。

配色 / 圆角 / 字体照站点 CSS 变量来，所以图与网页是一套观感。
``tools/make_share_card.py``（分享图）用的是同一套常量，但那是**离线工具**：
``app`` 不能反过来依赖 ``tools``（装成 wheel 后它不存在），所以这里自带一份最小实现。
"""

from __future__ import annotations

import importlib.util
import io
import time
from pathlib import Path
from typing import Any

from . import fonts
from .helpcard_content import (
    PRIVACY,
    PRIVACY_TITLE,
    SECTION_NOTES,
    SECTIONS,
    SUBTITLE,
    TITLE,
    TITLE_LABEL,
    source_digest,
)
from .logging_conf import get_logger

log = get_logger("helpcard")

W = 1080
PAD = 72  # 左右安全边距
BOTTOM_PAD = 48
#: 先画到一张够高的画布上，最后按实际用掉的高度裁掉——省掉一堆高度计算
CANVAS_H = 5600

INK = (4, 7, 12)  # 铺在强调色上的深色字（与站点 ::selection 同色）
PANEL = (16, 23, 37)  # --panel
LINE = (34, 224, 232, 41)  # 描边：rgba(34,224,232,.16)
ACCENT = (34, 224, 232)
ACCENT_2 = (255, 47, 142)
DIM = (147, 167, 193)
TXT = (230, 240, 255)

ROW_H = 54  # 一行命令的高度（说明跟在后面）
DESC_H = 34  # 说明被折到下一行时，每行的高度
LIST_H = 36  # 「先看这三步 / 群里 vs 私信」那种成对列表的行高

# 字体不在本模块里写死（站点字体栈里是 Bahnschrift + 微软雅黑 / Noto Sans CJK）：
# 各发行版把中文字体装在不同的目录，统一由 app/fonts.py 分四层去找。

#: 产物与指纹文件名（``static/`` 下；两个都不入库）
ART_NAME = "help.jpg"
STAMP_SUFFIX = ".src.sha256"


def available() -> bool:
    """有没有 Pillow（没有就画不出帮助图，插件会退回文字说明）。"""
    try:
        return importlib.util.find_spec("PIL") is not None
    except (ImportError, ValueError):
        return False


def _pil():
    """延迟导入 Pillow（导入失败由调用方兜住，退回文字说明）。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    return Image, ImageDraw, ImageFilter, ImageFont


def default_path() -> Path:
    """默认产物路径：``<仓库根>/static/help.jpg``（与 ``main.STATIC_DIR`` 同一个位置）。"""
    return Path(__file__).resolve().parent.parent / "static" / ART_NAME


# --------------------------------------------------------------------------- #
# 画布与基础件
# --------------------------------------------------------------------------- #
def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def background():
    """深色纵向渐变 + 网格 + 扫描线（与分享图同一套底）。"""
    Image, ImageDraw, ImageFilter, _Font = _pil()
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


def gradient_bar(img, xy: tuple[int, int], size: tuple[int, int]) -> None:
    """青→品红的小渐变条（标题下 / 页脚上）。"""
    Image, ImageDraw, _Filter, _Font = _pil()
    x, y = xy
    w, h = size
    bar = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for i in range(w):
        bd.line([(i, 0), (i, h)], fill=(*_lerp(ACCENT, ACCENT_2, i / max(1, w - 1)), 255))
    img.alpha_composite(bar, (x, y))


def panel(img, box: tuple[int, int, int, int], *, cut: int = 20, radius: int = 10) -> None:
    """站点那种「左上切角 + 其余小圆角」的面板：填充 + 极淡青边 + 顶部高光。"""
    Image, ImageDraw, _Filter, _Font = _pil()
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


def tab(img, xy: tuple[int, int], text: str, font) -> int:
    """强调色的分组标题条（① 看比赛 …）；返回它的高度。"""
    Image, ImageDraw, _Filter, _Font = _pil()
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


#: 不该出现在**行首**的标点：折行时它们要跟着前一个字走（「。」独占一行很难看）
_TRAILING_PUNCT = "。，、；：？！）】》」』”’…%,.;:?!)]}"


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
    merged: list[str] = []
    for token in out:
        if merged and token and all(ch in _TRAILING_PUNCT for ch in token):
            merged[-1] += token
        else:
            merged.append(token)
    return merged or [""]


def wrap(text: str, font, max_width: int) -> list[str]:
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


def list_block(img, draw, xy: tuple[int, int], title: str, rows, fonts) -> int:
    """「左标签 + 右说明」的成对列表面板，返回它的高度。

    两边都先**折行量好高度**再画面板：否则说明一写长就冲出卡片边框
    （这是这类「信息更详细一点」的改动最容易踩的坑）。
    """
    _x, y = xy
    label_w = max(int(fonts["label"].getlength(label)) for label, _ in rows) + 20
    text_w = W - PAD * 2 - 44 - label_w
    laid = [(label, wrap(text, fonts["text"], text_w)) for label, text in rows]
    body_h = 18 + 42 + sum(len(lines) * LIST_H for _l, lines in laid) + 14
    panel(img, (PAD, y, W - PAD, y + body_h), cut=20)
    ry = y + 16
    draw.text((PAD + 22, ry), title, font=fonts["head"], fill=(*ACCENT, 255))
    ry += 42
    for label, lines in laid:
        draw.text((PAD + 22, ry), label, font=fonts["label"], fill=(*ACCENT_2, 255))
        for line in lines:
            draw.text((PAD + 22 + label_w, ry), line, font=fonts["text"], fill=DIM)
            ry += LIST_H
    return body_h


# --------------------------------------------------------------------------- #
# 版面
# --------------------------------------------------------------------------- #
def render():
    Image, ImageDraw, ImageFilter, _Font = _pil()
    img = background()
    f_label = fonts.load("latin", 22)
    f_title = fonts.load("cjk", 66)
    f_sub = fonts.load("cjk", 25)
    f_small = fonts.load("cjk", 21)
    f_tab = fonts.load("cjk", 28)
    f_cmd = fonts.load("cjk", 25)
    f_desc = fonts.load("cjk", 23)
    list_fonts = {
        "head": fonts.load("cjk", 27),
        "label": fonts.load("cjk", 23),
        "text": fonts.load("cjk", 22),
    }
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

    # —— 三张命令卡：**先排版再画面板**（折行会顶高卡片，先算好才不会被顶出框）——
    #
    # 说明放哪，只看**跟命令同一行放不放得下**：
    # 放不下就整段落到下一行起画——以前这里还有一道「折出来只有一行就塞回同一行」的
    # 判断，那正是「说明冲出图片右边缘」的根因（按整行宽度量，塞进半行位置当然溢出）。
    cmd_x = PAD + 44
    inner_right = W - PAD - 24  # 卡片内右侧安全边（与面板留 24px）
    for title, rows in SECTIONS:
        y += tab(img, (PAD, y), title, f_tab) + 16

        #: 说明**跟在大字号命令后面**时该从哪儿起画：这个横坐标必须**随每一行**一起带下去。
        #: 踩过的坑：以前画的时候用了排版循环结束后残留的 `head_w`，于是所有说明都被画在
        #: 「最后一条命令」的横坐标上，长命令的说明直接压在命令上（右边缘一点没越界，
        #: 「有没有画到图片外」的检查全绿，眼睛却一眼看得出来）。
        laid: list[tuple[str, list[str], int, bool, float]] = []
        for cmd, desc in rows:
            text = f"—— {desc}"
            head_w = cmd_x + f_cmd.getlength(cmd) + 14
            if f_desc.getlength(text) <= inner_right - head_w:
                laid.append((cmd, [text], ROW_H, True, head_w))
            else:
                # 折到下一行（缩进在命令下面），行高按折了几行算
                parts = wrap(text, f_desc, inner_right - (cmd_x + 22))
                laid.append((cmd, parts, 34 + DESC_H * len(parts) + 10, False, head_w))
        # 分组小字也**先折行**：它是单行硬画的，说明一写长就冲出卡片右边缘
        note = SECTION_NOTES.get(title)
        note_lines = wrap(note, f_small, inner_right - (cmd_x + 22)) if note else []
        body_h = 20 + sum(h for _c, _p, h, _i, _x in laid) + (DESC_H * len(note_lines)) + 12
        panel(img, (PAD, y, W - PAD, y + body_h), cut=20)

        ry = y + 20
        for cmd, parts, height, inline, head_w in laid:
            draw.ellipse([PAD + 24, ry + 12, PAD + 31, ry + 19], fill=(*ACCENT, 255))
            draw.text((cmd_x, ry), cmd, font=f_cmd, fill=(*ACCENT, 255))
            if inline:
                draw.text((head_w, ry + 2), parts[0], font=f_desc, fill=DIM)
            else:
                ty = ry + 34
                for part in parts:
                    draw.text((cmd_x + 22, ty), part, font=f_desc, fill=DIM)
                    ty += DESC_H
            ry += height
        for i, part in enumerate(note_lines):
            draw.text((cmd_x + 22, ry + 6 + i * DESC_H), part, font=f_small, fill=(*ACCENT_2, 255))
        y += body_h + 24

    # —— 群里 vs 私信（省得到处找答案）——
    y += list_block(img, draw, (PAD, y), PRIVACY_TITLE, PRIVACY, list_fonts) + 24

    return img.crop((0, 0, W, min(CANVAS_H, y + BOTTOM_PAD)))


def build(*, quality: int = 92) -> bytes:
    """画出 JPEG 字节（**阻塞**；调用方负责放进线程）。"""
    img = render().convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def write(*, out: Path | None = None, quality: int = 92) -> dict[str, Any]:
    """重新生成帮助图并写盘（连指纹一起）；返回 ``{ok, path, bytes, width, height}``。

    指纹写在图**旁边**（``static/help.jpg.src.sha256``）：图不入库，所以它只用来
    判断「本地这一份图是不是比文案旧」（自检与测试都据此）。
    """
    target = Path(out) if out else default_path()
    try:
        data = build(quality=quality)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：并发 / 中断都不会让插件探到半张图
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        stamp = target.with_name(target.name + STAMP_SUFFIX)
        stamp.write_text(source_digest() + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001  (画不出来只是「没有帮助图」，不能影响启动)
        log.warning("帮助图生成失败（不影响其它功能）| %s", exc)
        return {"ok": False, "path": str(target), "reason": str(exc)}
    info = {"ok": True, "path": str(target), "bytes": len(data)}
    log.info("帮助图已生成 | %s | %.0f KB", target, len(data) / 1024)
    return info


def refresh(*, out: Path | None = None, quality: int = 92) -> bool:
    """启动时调用：重画一份帮助图（**不阻塞事件循环**由调用方负责）。

    没装 Pillow 就只记一行 info：帮助图没有也能用——插件会先 ``HEAD /help.jpg``，
    探不到就退回私聊文字说明；比赛卡片那边同样是「画不出来就退回文本」的兜底。
    """
    if not available():
        log.info("这台机器上没装 Pillow，跳过帮助图生成（插件会改用文字说明；重装依赖即可）")
        return False
    return bool(write(out=out, quality=quality)["ok"])


def stamp_path(out: Path | None = None) -> Path:
    """指纹文件路径（图**旁边**那一份，见 :func:`write`）。"""
    target = Path(out) if out else default_path()
    return target.with_name(target.name + STAMP_SUFFIX)


def is_stale(out: Path | None = None) -> bool:
    """这一份帮助图是不是**比出图代码旧**（或者压根没有）→ 该重画。

    两条判据：图不在 → 该画；指纹对不上 :func:`app.helpcard_content.source_digest`
    （它把**文案与排版代码**都算进去）→ 该重画。
    """
    target = Path(out) if out else default_path()
    if not target.is_file():
        return True
    stamp = stamp_path(target)
    if not stamp.is_file():
        return True
    return stamp.read_text(encoding="utf-8").strip() != source_digest()


#: 「上次画失败」的指纹与时刻：画不出来时冷一会儿再试，别让每个请求都重画一遍
_LAST_FAIL: tuple[str, float] = ("", 0.0)


def ensure_fresh(*, out: Path | None = None, cooldown: float = 60.0) -> bool:
    """图旧了就**当场重画**一份；返回「手上这份能不能用」。**阻塞**，调用方放线程里。

    为什么不能只靠启动时那次重画：运维的常态是**代码更新了、进程没换**。启动那一刻
    一次性重画的图会一直挂在 ``/help.jpg`` 上，于是「排版修好了、群里看到的还是旧图」，
    用户只能得出「修了没用」的结论（真踩过）。所以真正的闸门放在**服务端发这张图的
    时候**，见 ``main.help_image``：旧了就现画，画不动就拿旧的顶着。

    失败**不抛异常**：拿旧图顶着也比 404 / 500 强。同一个指纹连续失败会冷却一分钟，
    免得每次请求都白画一遍（画一次约半秒）。
    """
    global _LAST_FAIL
    target = Path(out) if out else default_path()
    if not is_stale(target):
        return True
    if not available():
        return False
    digest = source_digest()
    if _LAST_FAIL[0] == digest and time.monotonic() - _LAST_FAIL[1] < cooldown:
        return False
    if write(out=target)["ok"]:
        _LAST_FAIL = ("", 0.0)
        return True
    _LAST_FAIL = (digest, time.monotonic())
    return False
