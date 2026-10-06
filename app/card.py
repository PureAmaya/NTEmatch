"""比赛信息卡片：**服务器实时渲染**的图片推送。

为什么要图片
------------

比赛信息推到群里时，除了名字 / 简介 / 时间 / 赛制，还要带上**比赛规则**；
而规则是随赛制自动生成的（见 :func:`app.logic.rulebook`），条目一多，纯文本在群里
就是一屏接一屏地刷（还得被切成好几段发）。渲染成一张图：信息密度高、一眼看得完、
方便群友转发，也不再受「单条消息长度」的限制。

三条硬约束（都直接关系到「别把服务搞卡」）
------------------------------------------

1. **内容定址 + 落盘缓存**：卡片内容的 sha256 就是文件名（``data/cards/<hash>.png``）。
   同一份内容**只渲染一次**——只要没人改赛制 / 赛程，反复推送都不会再花一次 CPU；
2. **渲染在线程里、并发受限**：:func:`asyncio.to_thread` + 信号量 + 超时，事件循环一秒都
   不等；同一份内容的并发请求还会合并成一次渲染（单飞锁），不会出现「十个人同时点预览，
   十次全量渲染」；
3. **Pillow 是正式依赖**（``pyproject.toml`` 的 ``dependencies`` 里）：图片推送就是靠它
   渲染，默认装好就有图。万一某个环境没装上（自建镜像漏了依赖、装了坏版本），
   :func:`available` 是 ``False``、:func:`card_for_event` 回 ``None``，调用方
   **自动退回纯文本**（信息一条不少，只是排版朴素）——那是**兜底**，不是常态。

安全性
------

卡片里只有**本站本来就会公开展示**的东西（届名 / 赛制 / 规则 / 分组），但地址不是
「换个链接就能下载点什么」的口子：

* 文件名是内容哈希，猜不出来；
* :func:`resolve` **只认本站签发过的哈希**（:data:`_ISSUED`）——没经推送 / 预览
  流出去的卡片，就算有人凑出哈希也读不到。

签到表只在内存里（重启即清空）。代价是「重启后，历史上发出去的那张图链接会 404」；
好处是**永远不会**因为一份陈年文件而泄露一届已经被隐藏的赛事——群里那张图早已送达，
而链接重新签发一次（再推一次）成本几乎为零。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any

from . import fonts, logic, markdown
from .logging_conf import get_logger
from .models import Config
from .store import DATA_ROOT

log = get_logger("card")

#: 卡片落盘目录（与备份、公告图片分开存放）
CARD_ROOT = DATA_ROOT / "cards"

#: 画布宽度（与帮助图同一档，手机上缩放后仍清晰）
WIDTH = 1080
PAD = 64
#: 高度上限：规则再多也不至于出一张几万像素的图（超出部分写到「见站点」）
MAX_HEIGHT = 6000
#: 一张卡片最多几张（内容定址的目录会攒文件，按 mtime 清最旧的）
MAX_FILES = 240
#: 渲染超时：超了就当作「这次没图」，退回文本推送（总比把请求挂在这儿强）
RENDER_TIMEOUT = 20.0
#: 同时渲染数（Pillow 是纯 CPU，放开跑会把核占满，界面就卡了）
MAX_CONCURRENT = 2
#: 卡片文件名的白名单：只认我们自己写出去的形态
_NAME_RE = re.compile(r"^[0-9a-f]{16,64}\.png$")

# 站点配色（与 static/css/nte.css 的变量一致，图与网页是一套观感）
INK = (4, 7, 12)
PANEL = (16, 23, 37)
ACCENT = (34, 224, 232)
ACCENT_2 = (255, 47, 142)
DIM = (147, 167, 193)
TXT = (230, 240, 255)
LINE = (34, 224, 232, 60)
WARN = (255, 196, 92)

# 字体不在本模块里写死：中文必须是 CJK 字体，而「它装在哪」各发行版都不一样，
# 统一由 app/fonts.py 分四层去找（环境变量 → 常见路径 → 扫字体目录 → 内置兜底）。

#: 已签发的卡片（哈希 → 签发信息）。只读内存，见模块说明。
_ISSUED: dict[str, dict[str, Any]] = {}
_render_locks: dict[str, asyncio.Lock] = {}
_semaphore: asyncio.Semaphore | None = None


# --------------------------------------------------------------------------- #
# Pillow（图片推送的渲染器）
# --------------------------------------------------------------------------- #
def available() -> bool:
    """这台机器上能不能画图（Pillow 装没装）。

    Pillow 是正式依赖，正常情况下这里一直是 ``True``；留着这道判断是为了
    **兜底**：环境没装全 / 装坏了时，与其整个推送报错，不如安静退回纯文本。

    只探「装没装」（``find_spec``），**不导入**：这条判断每次推送都会问一次，
    没必要为它真把 Pillow 拉进来（真装坏了也由 :func:`render` 兜住，同样退回文本）。
    """
    try:
        return importlib.util.find_spec("PIL") is not None
    except (ImportError, ValueError):  # 包元数据坏掉之类：当作没有
        return False


def _pil():
    """延迟导入 Pillow（导入失败由调用方兜住，退回纯文本）。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    return Image, ImageDraw, ImageFilter, ImageFont


# --------------------------------------------------------------------------- #
# 内容：**纯函数**（不碰 Pillow、不碰网络），所以卡片内容本身能直接单测
# --------------------------------------------------------------------------- #
def _clean_text(text: str, limit: int) -> str:
    """压平空白、去掉 Markdown 记号、按字符截断。

    这里**必须**过一遍 :func:`app.markdown.to_text`：规则条目里带着 ``**强调**``
    （那是给网页用的），直接画进图里就是一堆星号——渲染成图时最容易漏的一步。
    图片里没有滚动条，太长也只能截。
    """
    return markdown.to_text(str(text or ""), limit)


def _key_pairs(cfg: Config, facts: dict[str, Any]) -> list[tuple[str, str]]:
    """卡片上半部分的「一栏一项」：赛制 / 人数 / 分组 / 打法 / 计分口径…"""
    evt = cfg.event
    rules = cfg.rules
    sc = rules.scoring
    players = logic.joined_players(cfg)
    status = {"draft": "筹备中", "active": "进行中", "closed": "已结束"}.get(
        str(evt.status), str(evt.status)
    )
    pairs: list[tuple[str, str]] = [("状态", status)]
    if evt.start_time:
        pairs.append(("开赛", str(evt.start_time).replace("T", " ")[:16]))
    if evt.end_time:
        pairs.append(("结束", str(evt.end_time).replace("T", " ")[:16]))
    pairs.append(("赛制", str(facts.get("formatLabel") or "")))
    if facts.get("format") == "league":
        pairs.append(("每局", f"{rules.team_size} 人对 {rules.team_size} 人"))
        pairs.append(("局数", f"共 {rules.total_rounds} 局"))
    else:
        pairs.append(("每队", f"{rules.team_size} 人"))
    pairs.append(("参赛", f"{len(players)} 人" + (f" · {facts.get('teams')} 队" if facts.get("teams") else "")))
    if facts.get("groups"):
        pairs.append(("分组", f"{facts['groups']} 组（{facts.get('groupSizes') or '—'}）"))
    pairs.append(("每场", str(facts.get("shape") or "")))
    if facts.get("format") != "league":
        if facts.get("groupRounds"):
            per = str(facts.get("perTeamMatches") or "")
            pairs.append(("小组赛", f"{facts['groupRounds']} 轮" + (f" · {per}" if per else "")))
        pairs.append(
            (
                "淘汰赛",
                f"{facts['size']} 强 · {'双败' if facts.get('loserBracket') else '单败'}"
                if facts.get("size")
                else ("双败" if facts.get("loserBracket") else "单败"),
            )
        )
    pairs.append(("计分", f"{sc.type_label}（{sc.label_text}）· {sc.better_label}"))
    if facts.get("targetScore") and facts.get("format") != "league" and not sc.low_wins:
        pairs.append(("单轮目标", f"{facts['targetScore']} 分"))
    pairs.append(("名次", "计算名次与晋级" if cfg.event.ranked else "娱乐模式（不排名）"))
    return pairs


def _group_lines(cfg: Config) -> list[tuple[str, str]]:
    """分组名单：让群友一眼看到「我跟谁一组」。队伍太多时不画（免得卡片变成花名册）。"""
    teams = cfg.teams
    if not teams or len(teams) > 24:
        return []
    buckets: dict[str, list[str]] = {}
    for team in teams:
        buckets.setdefault(team.group or "A", []).append(team.label)
    return [(f"{key} 组", "、".join(names)) for key, names in sorted(buckets.items())]


def _caption(cfg: Config, facts: dict[str, Any]) -> str:
    """配在图前面的一行话：图挂了 / 不方便看图的人也能知道这是什么。"""
    evt = cfg.event
    name = evt.name or evt.title or "比赛"
    bits = [f"【{name}】"]
    if evt.start_time:
        bits.append(str(evt.start_time).replace("T", " ")[:16])
    if facts.get("teams"):
        bits.append(f"{facts['teams']} 队 / {facts.get('players')} 人")
    if facts.get("shape"):
        bits.append(str(facts["shape"]))
    bits.append("完整赛制与规则见图")
    return " · ".join(bits)


def payload_for(cfg: Config, event_id: str = "", state: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一届的「信息 + 规则」整理成卡片内容（纯函数，方便单测与指纹计算）。

    规则部分**直接来自** :func:`app.logic.rulebook`——它就是「比赛规则」面板上那一份，
    所以卡片永远不会跟实际赛制脱节（改了赛制，卡片的指纹就变了，下次推送自动重画）。
    """
    rb = logic.rulebook(cfg)
    facts = rb.get("facts") or {}
    evt = cfg.event
    sections = [
        {
            "title": str(section.get("title") or ""),
            "items": [_clean_text(item, 160) for item in (section.get("items") or [])],
        }
        for section in (rb.get("sections") or [])
    ]
    note = markdown.to_text(cfg.event.rules_text or "", 600)
    return {
        "title": str(evt.name or evt.title or "比赛"),
        "id": str(event_id or ""),
        "status": {"draft": "筹备中", "active": "进行中", "closed": "已结束"}.get(
            str(evt.status), str(evt.status)
        ),
        "brief": _clean_text(evt.brief, 60),
        "headline": _clean_text(str(rb.get("headline") or ""), 80),
        "caption": _caption(cfg, facts),
        "meta": _key_pairs(cfg, facts),
        "groups": _group_lines(cfg),
        "sections": sections,
        "note": note,
        "noteTitle": "赛事信息",
        "ranks": bool(cfg.event.ranked),
    }


def digest(payload: dict[str, Any]) -> str:
    """内容指纹：同样的内容永远得到同一个文件名（这就是「不重复生成」的凭据）。

    ``sort_keys`` + ``ensure_ascii=False``：两边算出来的必须一模一样，
    否则「同样的内容」会被当成两份，缓存也就白做了。

    **字体也算进指纹**：图是拿当时的字体画出来的——换了字体（或者这台机器从
    「没装中文字体」变成「装了」），同一份内容画出来就是另一张图。不算进去的话
    会一直发着旧那张（真踩过：中文是一排方块的那版）。
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((blob + _font_signature()).encode("utf-8")).hexdigest()


def _font_signature() -> str:
    """画这张图用的字体文件（找不到就是 ``-``）：并入指纹，换字体就自动重画。"""
    return "|" + "|".join(str(fonts.resolve(kind) or "-") for kind in ("cjk", "latin", "mono"))


# --------------------------------------------------------------------------- #
# 渲染（Pillow）—— 所有函数都在线程里跑，见 card_for_event
# --------------------------------------------------------------------------- #
#: 不该出现在**行首**的标点：折行时它们要跟着前一个字走（「。」独占一行很难看）
_TRAILING_PUNCT = "。，、；：？！）】》」』”’…%,.;:?!)]}"


def _tokens(text: str) -> list[str]:
    """按「中文逐字、西文成串」切词——中文没有空格，只能逐字折行。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_/+.:%()"):
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    # 收尾标点并进前一个词：否则折行时会出现「……跟谁打」+「。」的孤行
    merged: list[str] = []
    for token in out:
        if merged and token and all(ch in _TRAILING_PUNCT for ch in token):
            merged[-1] += token
        else:
            merged.append(token)
    return merged or [""]


def _wrap(text: str, font, width: int) -> list[str]:
    """把一段文字折成若干行；**单个词比整行还宽**时硬切，避免溢出画布。"""
    lines: list[str] = []
    cur = ""
    for token in _tokens(text):
        probe = cur + token
        if font.getlength(probe) <= width or not cur:
            while font.getlength(probe) > width and len(probe) > 1:
                # 单个长词（一长串英文 / 数字）：按宽度硬切
                cut = max(1, len(probe) // 2)
                while cut > 1 and font.getlength(probe[:cut]) > width:
                    cut -= 1
                lines.append(probe[:cut])
                probe = probe[cut:]
            cur = probe
            continue
        lines.append(cur)
        cur = token
    if cur:
        lines.append(cur)
    return lines or [""]


def render(payload: dict[str, Any], *, site: str = "") -> bytes | None:
    """把 :func:`payload_for` 的内容画成 PNG（**阻塞**，调用方负责放进线程）。

    画法：先画到一张够高的画布上，最后按实际用掉的高度裁掉——省掉一堆高度预估，
    也让「规则条目多一条」这种事不需要改任何布局常量。
    """
    try:
        Image, ImageDraw, _Filter, _ImageFont = _pil()
    except Exception as exc:  # noqa: BLE001  (Pillow 装坏了 / 缺依赖：这次没图，退回文本)
        log.info("没有装 Pillow，卡片渲染跳过 | %s", exc)
        return None
    try:
        img = Image.new("RGBA", (WIDTH, MAX_HEIGHT), (*PANEL, 255))
        draw = ImageDraw.Draw(img)
        f_title = fonts.load("cjk", 56)
        # 副标题那行是「编号 · 状态 · 一句话」，**含中文**（「筹备中」「第 1 届」…）：
        # 必须用 CJK 字体。曾经这里用等宽西文字体（bahnschrift），中文全成了空心方块。
        f_sub = fonts.load("cjk", 24)
        f_key = fonts.load("cjk", 24)
        f_val = fonts.load("cjk", 26)
        f_body = fonts.load("cjk", 26)
        f_tab = fonts.load("cjk", 26)
        f_note = fonts.load("cjk", 22)
        inner = WIDTH - PAD * 2

        y = PAD
        # ---- 标题区 ----
        for line in _wrap(str(payload.get("title") or "比赛"), f_title, inner)[:2]:
            draw.text((PAD, y), line, font=f_title, fill=TXT)
            y += int(f_title.size * 1.25)
        sub_bits = [b for b in (payload.get("id"), payload.get("status"), payload.get("headline")) if b]
        if sub_bits:
            draw.text((PAD, y), " · ".join(str(b) for b in sub_bits), font=f_sub, fill=ACCENT)
            y += int(f_sub.size * 1.6)
        # 渐变小条：与站点品牌线同色
        bar = Image.new("RGBA", (inner, 4), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        for i in range(inner):
            t = i / max(1, inner - 1)
            bd.line(
                [(i, 0), (i, 4)],
                fill=(
                    round(ACCENT[0] + (ACCENT_2[0] - ACCENT[0]) * t),
                    round(ACCENT[1] + (ACCENT_2[1] - ACCENT[1]) * t),
                    round(ACCENT[2] + (ACCENT_2[2] - ACCENT[2]) * t),
                    235,
                ),
            )
        img.alpha_composite(bar, (PAD, y))
        y += 26

        def panel_lines(pairs: list[tuple[str, str]], *, label: str = "") -> int:
            """画一块「键：值」面板，返回新的 y。

            「键」在左边（暗色），值的第一行跟它**同一行**，长值折行时缩进对齐到值那一列。
            """
            nonlocal y
            if not pairs:
                return y
            if label:
                draw.text((PAD + 18, y), label, font=f_key, fill=ACCENT)
                y += int(f_key.size * 1.9)
            for key, value in pairs:
                key_span = int(f_key.getlength(key) + 18)
                lines = _wrap(str(value), f_val, inner - key_span - 36) or [""]
                draw.text((PAD + 8, y), key, font=f_key, fill=DIM)
                for index, line in enumerate(lines):
                    draw.text((PAD + 8 + key_span, y), line, font=f_val, fill=TXT)
                    if index < len(lines) - 1:
                        y += 44
                y += 44
            draw.line([(PAD, y - 14), (WIDTH - PAD, y - 14)], fill=LINE, width=1)
            y += 10
            return y

        y = panel_lines(list(payload.get("meta") or []))
        y = panel_lines(list(payload.get("groups") or []), label="分组名单")

        # ---- 比赛规则（就是「规则」面板那一份，自动跟着赛制变）----
        for section in payload.get("sections") or []:
            items = [item for item in (section.get("items") or []) if item]
            if not items:
                continue
            title = str(section.get("title") or "")
            if title:
                tab_w = int(f_tab.getlength(title)) + 40
                tab = Image.new("RGBA", (tab_w, f_tab.size + 20), (*ACCENT, 235))
                img.alpha_composite(tab, (PAD, y))
                ImageDraw.Draw(img).text(
                    (PAD + 20, y + 9), title, font=f_tab, fill=INK
                )
                y += f_tab.size + 34
            for item in items:
                wrapped = _wrap(str(item), f_body, inner - 26)
                for index, line in enumerate(wrapped):
                    if index == 0:
                        draw.text((PAD + 6, y), "·", font=f_body, fill=ACCENT)
                    draw.text((PAD + 26, y), line, font=f_body, fill=TXT)
                    y += int(f_body.size * 1.42)
            y += 14
            if y > MAX_HEIGHT - 400:
                draw.text((PAD + 26, y), "…（还有更多规则，见站点「比赛规则」）", font=f_note, fill=WARN)
                y += 40
                break

        # ---- 赛事信息（Markdown 原文的纯文本摘要）----
        note = str(payload.get("note") or "").strip()
        if note and y < MAX_HEIGHT - 260:
            title = str(payload.get("noteTitle") or "赛事信息")
            tab_w = int(f_tab.getlength(title)) + 40
            tab = Image.new("RGBA", (tab_w, f_tab.size + 20), (*DIM, 60))
            img.alpha_composite(tab, (PAD, y))
            ImageDraw.Draw(img).text((PAD + 20, y + 9), title, font=f_tab, fill=TXT)
            y += f_tab.size + 34
            for line in _wrap(note, f_note, inner):
                if y > MAX_HEIGHT - 140:
                    break
                draw.text((PAD + 6, y), line, font=f_note, fill=DIM)
                y += int(f_note.size * 1.5)
            y += 16

        # ---- 页脚 ----
        y = min(y, MAX_HEIGHT - 110)
        draw.line([(PAD, y), (WIDTH - PAD, y)], fill=LINE, width=1)
        y += 26
        draw.text((PAD, y), str(payload.get("caption") or ""), font=f_note, fill=DIM)
        if site:
            draw.text((PAD, y + 34), f"{site} · 规则随赛制自动生成", font=f_note, fill=ACCENT)
            y += 34
        cropped = img.crop((0, 0, WIDTH, min(MAX_HEIGHT, y + PAD)))

        import io

        buf = io.BytesIO()
        # 转 RGB 再存：截图类内容用不着 alpha，JPEG 化也小得多
        cropped.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        log.warning("卡片渲染失败（本次退回纯文本推送）", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# 缓存 / 签发 / 读取
# --------------------------------------------------------------------------- #
def _prune() -> None:
    """卡片攒太多时清最旧的（内容定址的目录只增不减，得有人扫一下）。"""
    try:
        files = sorted(CARD_ROOT.glob("*.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[: max(0, len(files) - MAX_FILES)]:
        try:
            path.unlink()
            _ISSUED.pop(path.stem, None)
        except OSError:  # 正被读取 / 没权限：下次再说
            continue


def resolve(name: str) -> Path | None:
    """按文件名取卡片；**只认签发过的哈希**（见模块说明）。"""
    clean = str(name or "").strip()
    if not _NAME_RE.fullmatch(clean):
        return None
    if clean[:-4] not in _ISSUED:
        return None
    path = CARD_ROOT / clean
    return path if path.exists() else None


def card_bytes(name: str) -> bytes | None:
    """读回一张已签发卡片的 PNG 字节；读不到（名字非法 / 文件没了）回 ``None``。

    给推送用：新版 AstrBot 要「先把图传上去换 attachment_id」，而
    :func:`card_for_event` 缓存命中时不带字节（省一次读盘），那就从磁盘补读。

    ``name`` 既可以是文件名（``<哈希>.png``）也可以只给哈希——
    :func:`card_for_event` 返回的就是后者，别让调用方自己拼后缀。
    """
    clean = str(name or "").strip()
    if clean and not clean.endswith(".png"):
        clean += ".png"
    path = resolve(clean)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def stats() -> dict[str, Any]:
    """卡片缓存占用（管理端展示用）。"""
    files = list(CARD_ROOT.glob("*.png")) if CARD_ROOT.exists() else []
    return {
        "cards": len(files),
        "bytes": sum(path.stat().st_size for path in files if path.exists()),
        "issued": len(_ISSUED),
        "available": available(),
    }


async def card_for_event(
    cfg: Config,
    event_id: str = "",
    state: dict[str, Any] | None = None,
    *,
    site: str = "",
) -> dict[str, Any] | None:
    """拿到这一届的卡片（**没有就渲染**）；不可用 / 失败一律回 ``None``。

    回 ``{hash, caption, url, bytes}``：``url`` 是给人看的相对地址（调用方补上站点域名），
    ``bytes`` 只在刚刚渲染出来时非空（缓存命中时为 ``None``，省一次读盘）。
    """
    if not available():
        return None
    payload = payload_for(cfg, event_id, state)
    key = digest(payload)
    path = CARD_ROOT / f"{key}.png"
    fresh = b""
    if not path.exists():
        lock = _render_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not path.exists():  # 单飞：等锁的后来者会看到文件已经在了
                global _semaphore
                if _semaphore is None:
                    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
                async with _semaphore:
                    try:
                        data = await asyncio.wait_for(
                            asyncio.to_thread(render, payload, site=site), timeout=RENDER_TIMEOUT
                        )
                    except TimeoutError:
                        log.warning("卡片渲染超时（%s 秒），本次退回纯文本", RENDER_TIMEOUT)
                        return None
                    except Exception:
                        # render 内部已经兜了一层；这里再兜一层是因为**推送绝不能因图挂掉**：
                        # 线程里漏出来的异常会一路冒到请求上，变成群里收不到任何消息。
                        log.warning("卡片渲染异常（本次退回纯文本）", exc_info=True)
                        return None
                if not data:
                    return None
                try:
                    CARD_ROOT.mkdir(parents=True, exist_ok=True)
                    # 先写临时文件再改名：并发 / 中断都不会留下半张图
                    tmp = path.with_suffix(".png.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    fresh = data
                except OSError as exc:
                    log.warning("卡片落盘失败（本次仍可推送）| %s", exc)
        _render_locks.pop(key, None)
        _prune()
    if not path.exists():
        return None
    _ISSUED[key] = {"at": time.time(), "eventId": str(event_id or "")}
    return {
        "hash": key,
        "caption": str(payload.get("caption") or ""),
        "url": f"/api/cards/{key}.png",
        "title": str(payload.get("title") or ""),
        "bytes": fresh,
    }
