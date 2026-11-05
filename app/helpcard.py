"""QQ 机器人**帮助图**（站点的 ``/help.jpg``）：**服务启动时自动重画一份**。

为什么要自己画而不是放一张图进仓库：图上的命令是**精确匹配**的，文案一改（加命令、
改说明），旧图就成了「照着打毫无反应」的陷阱；而图本身没有版本号（地址固定
``/help.jpg``）。所以让图跟着**内容模块**现画：内容改了，重启一次站点就是新的。
产物也不再入库（``static/help.jpg`` 与它的指纹文件都在 ``.gitignore`` 里）。

三条约定：

* **内容在** :mod:`app.helpcard_content`（纯数据、零依赖），排版在
  ``go/internal/render``（与比赛卡片共用一套配色与画法）；
* **画不出来只是「没有帮助图」**：插件会先探 ``HEAD /help.jpg``，探不到就退回私聊
  文字说明，功能一点不少（见 :func:`refresh`）；
* **画图跑在线程里**（:func:`refresh` 由启动流程用 ``asyncio.to_thread`` 调），
  不让它拖慢启动；画失败也只记日志。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import render as renderer
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

#: 产物与指纹文件名（``static/`` 下；两个都不入库）
ART_NAME = "help.jpg"
STAMP_SUFFIX = ".src.sha256"
#: JPEG 质量（帮助图是文字为主，92 已经看不出差别，体积却小三成）
QUALITY = 92


def available() -> bool:
    """能不能画（出图侧车在不在）。没有就退回文字说明。"""
    return renderer.available()


def default_path() -> Path:
    """默认产物路径：``<仓库根>/static/help.jpg``（与 ``main.STATIC_DIR`` 同一个位置）。"""
    return Path(__file__).resolve().parent.parent / "static" / ART_NAME


# --------------------------------------------------------------------------- #
# 内容 → 出图请求
# --------------------------------------------------------------------------- #
def payload() -> dict[str, Any]:
    """把内容模块整理成一张图的内容（纯数据，方便单测与指纹计算）。"""
    return {
        "label": TITLE_LABEL,
        "title": TITLE,
        "subtitle": SUBTITLE,
        "sections": [
            {
                "title": title,
                "note": SECTION_NOTES.get(title, ""),
                "rows": [{"cmd": cmd, "desc": desc} for cmd, desc in rows],
            }
            for title, rows in SECTIONS
        ],
        "privacyTitle": PRIVACY_TITLE,
        "privacy": [[label, text] for label, text in PRIVACY],
    }


def build(*, quality: int = QUALITY) -> bytes:
    """画出 JPEG 字节（**阻塞**；调用方负责放进线程）。

    画不出来（没有二进制 / 超时 / 出错）抛 :class:`RuntimeError`——调用方
    （:func:`write` / :func:`refresh`）会把它变成一行日志，不影响其它功能。
    """
    result = renderer.render("help", payload(), quality=quality)
    if result is None:
        raise RuntimeError("出图侧车不可用或渲染失败")
    return result[0]


# --------------------------------------------------------------------------- #
# 落盘与新鲜度
# --------------------------------------------------------------------------- #
def write(*, out: Path | None = None, quality: int = QUALITY) -> dict[str, Any]:
    """重新生成帮助图并写盘（连指纹一起）；返回 ``{ok, path, bytes}``。"""
    target = Path(out) if out else default_path()
    try:
        data = build(quality=quality)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：并发 / 中断都不会让插件探到半张图
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        stamp_path(target).write_text(stamp_text() + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001  (画不出来只是「没有帮助图」，不能影响启动)
        log.warning("帮助图生成失败（不影响其它功能）| %s", exc)
        return {"ok": False, "path": str(target), "reason": str(exc)}
    log.info("帮助图已生成 | %s | %.0f KB", target, len(data) / 1024)
    return {"ok": True, "path": str(target), "bytes": len(data)}


def stamp_text() -> str:
    """指纹内容：**文案 + 排版代码 + 渲染器**。

    后两项不能少：改了内容要让图重画，改了画法（换二进制 / 换字体）同样要重画——
    不然「排版修好了、群里看到的还是旧图」，用户只能得出「修了没用」的结论。
    """
    return f"{source_digest()}|{renderer.signature()}"


def stamp_path(out: Path | None = None) -> Path:
    """指纹文件路径（图**旁边**那一份，见 :func:`write`）。"""
    target = Path(out) if out else default_path()
    return target.with_name(target.name + STAMP_SUFFIX)


def is_stale(out: Path | None = None) -> bool:
    """这一份帮助图是不是**比出图代码旧**（或者压根没有）→ 该重画。"""
    target = Path(out) if out else default_path()
    if not target.is_file():
        return True
    stamp = stamp_path(target)
    if not stamp.is_file():
        return True
    return stamp.read_text(encoding="utf-8").strip() != stamp_text()


def refresh(*, out: Path | None = None, quality: int = QUALITY) -> bool:
    """启动时调用：重画一份帮助图（**不阻塞事件循环**由调用方负责）。"""
    if not available():
        log.info("没有出图侧车（nterender），跳过帮助图生成（插件会改用文字说明）")
        return False
    return bool(write(out=out, quality=quality)["ok"])


#: 「上次画失败」的指纹与时刻：画不出来时冷一会儿再试，别让每个请求都重画一遍
_LAST_FAIL: tuple[str, float] = ("", 0.0)


def ensure_fresh(*, out: Path | None = None, cooldown: float = 60.0) -> bool:
    """图旧了就**当场重画**一份；返回「手上这份能不能用」。**阻塞**，调用方放线程里。

    为什么不能只靠启动时那次重画：运维的常态是**代码更新了、进程没换**。启动那一刻
    一次性重画的图会一直挂在 ``/help.jpg`` 上，于是「排版修好了、群里看到的还是旧图」，
    用户只能得出「修了没用」的结论（真踩过）。所以真正的闸门放在**服务端发这张图的
    时候**，见 ``main.help_image``：旧了就现画，画不动就拿旧的顶着。

    失败**不抛异常**：拿旧图顶着也比 404 / 500 强。同一个指纹连续失败会冷却一分钟，
    免得每次请求都白画一遍。
    """
    global _LAST_FAIL
    target = Path(out) if out else default_path()
    if not is_stale(target):
        return True
    if not available():
        return False
    digest = stamp_text()
    if _LAST_FAIL[0] == digest and time.monotonic() - _LAST_FAIL[1] < cooldown:
        return False
    if write(out=target)["ok"]:
        _LAST_FAIL = ("", 0.0)
        return True
    _LAST_FAIL = (digest, time.monotonic())
    return False
