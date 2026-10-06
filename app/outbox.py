"""群消息的**真 @ 投递通道**：站点排队，AstrBot 里的插件来发。

为什么非得绕这么一圈：AstrBot 的 OpenAPI（``POST /api/v1/im/message``）**没有 at 段**
——它的消息段解析只认 ``plain / image / record / file / video``，而且是 ``strict=True``
（塞 ``at`` 进去直接报错）；官方文档列的也是这几种。所以站点从外面**发不出真 @**，
只能把 ``[CQ:at,qq=…]`` 写进文本，而多数 OneBot 实现只对「字符串消息」解析 CQ 码，
数组段里的 text 是**字面量**——群里看到的就是一串方括号。

真 @ 只能在 AstrBot **进程内部**用 ``At`` 组件发，而那正是插件呆的地方。于是分工：

* 站点把「要 @ 谁的 QQ + 正文」放进队列（``push_outbox`` 表）；
* 插件每隔几秒 ``GET /api/bot/outbox`` 取走，用 ``At`` 组件发出去，再 ``ack`` 回执；
* **取不走也不要紧**：超过 :data:`FALLBACK_SECONDS` 还没回执，站点自己按
  「文本 @ 写法」（``atMode``：CQ 码 / ``@QQ号`` / 不 @）发出去并标成 ``fallback``
  ——队列只是让 @ 变成真 @，**不是唯一通道**，消息不会因为它丢；
* 插件压根不在线（:data:`PLUGIN_TTL_SECONDS` 内没来取过件）时**根本不排队**：
  直接按文本写法发。省一次来回，也不给管理员「已交给插件」的假象。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from . import qqbot
from .logging_conf import get_logger
from .store import store

log = get_logger("outbox")

#: 多久没来取件就当插件不在线（插件默认每 5 秒取一次，留足重连 / 重启的余量）
PLUGIN_TTL_SECONDS = 90
#: 排队等多久还没被插件取走 → 站点自己退回文本发（宁可晚一点，也不能不发）
FALLBACK_SECONDS = 45
#: 巡检间隔（秒）：只干「超时退回 + 清理旧记录」这两件事
TICK_SECONDS = 15
#: 已收尾的记录最多留几条（未投递的永远不清）
KEEP_FINISHED = 50
#: 插件最后一次来取件的时间（meta 键）
SEEN_KEY = "outbox:plugin_seen"


def _now() -> datetime:
    return datetime.now()  # noqa: DTZ005


def _parse(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 插件在不在线
# --------------------------------------------------------------------------- #
async def mark_seen() -> None:
    """插件来取件时记一笔（存活判据就是这个时间戳）。"""
    await store.set_meta(SEEN_KEY, _now().isoformat(timespec="seconds"))


async def seen_at() -> str:
    """插件最后一次来取件的时间（空串 = 从没来过）。"""
    return (await store.meta(SEEN_KEY)).strip()


async def plugin_alive(*, now: datetime | None = None) -> bool:
    """插件还在取件吗（N 分钟内取过就算在线）。"""
    stamp = _parse(await seen_at())
    if stamp is None:
        return False
    return (now or _now()) - stamp <= timedelta(seconds=PLUGIN_TTL_SECONDS)


# --------------------------------------------------------------------------- #
# 投递
# --------------------------------------------------------------------------- #
async def enqueue(
    *, kind: str, body: str, mentions: list[str], umo: str = "", event_id: str = ""
) -> dict[str, Any]:
    """排一条待投递的消息（插件取走后用 ``At`` 组件发真 @）。"""
    item = await store.push_enqueue(
        kind=kind, body=body, mentions=mentions, umo=umo, event_id=event_id
    )
    log.info(
        "真 @ 消息已排队 | 类型=%s | @%d 人 | id=%s", kind, len(item["mentions"]), item["id"]
    )
    return item


async def send_text(
    *,
    kind: str,
    body: str,
    mentions: list[str],
    settings: dict[str, Any],
    umo: str = "",
    item_id: str = "",
    detail: str = "",
) -> dict[str, Any]:
    """**退回写法**：把 @ 写进文本再发（CQ 码 / ``@QQ号`` / 不 @，看 ``atMode``）。

    ``item_id`` 非空表示这是在替队列里的一条消息收尾：发成功标 ``fallback``、
    失败标 ``failed``（失败不丢信息，管理员能到面板里看见原因）。
    """
    head = qqbot.at_text(mentions, settings)
    text = "\n".join(part for part in (head, body) if part)
    parts = qqbot.split_message(qqbot.to_plain_text(text), int(settings.get("maxChars") or 1200))
    result = await qqbot.send_parts(parts, settings=settings)
    ok = bool(result.get("ok"))
    if item_id:
        note = detail or ("已按文本写法发出" if ok else "文本写法也发不出去")
        await store.push_finish(
            item_id,
            status="fallback" if ok else "failed",
            via="webhook",
            detail=f"{note}｜{result.get('detail') or ''}".strip("｜"),
        )
    if not ok:
        log.warning("文本退回也没发出去 | 类型=%s | %s", kind, result.get("detail"))
    return {
        "ok": ok,
        "via": "webhook",
        "detail": result.get("detail") or "",
        "sent": result.get("sent") or 0,
        "total": len(parts),
        "umo": result.get("umo") or umo or qqbot.resolved_umo(settings),
    }


async def deliver(
    *,
    kind: str,
    body: str,
    mentions: list[str],
    settings: dict[str, Any] | None = None,
    umo: str = "",
    event_id: str = "",
) -> dict[str, Any]:
    """**要 @ 人的消息走这里**：插件在线就排队（真 @），否则当场按文本写法发。

    返回里 ``via`` 说明这条走了哪条路：``plugin``（排队等插件真 @）/
    ``webhook``（站点自己按文本发）。两条路都 ``ok`` 才算发出去。
    """
    settings = settings or store.qqbot_settings()
    if not settings.get("enabled"):
        return {"ok": False, "via": "", "detail": "未启用 QQ 机器人推送"}
    qqs = [str(q).strip() for q in (mentions or []) if str(q).strip()]
    target = umo or qqbot.resolved_umo(settings)
    if qqs and await plugin_alive():
        item = await enqueue(
            kind=kind, body=body, mentions=qqs, umo=target, event_id=event_id
        )
        return {
            "ok": True,
            "via": "plugin",
            "detail": "",
            "queued": item,
            "mentions": qqs,
            "sent": 0,
            "total": 1,
            "umo": target,
        }
    return await send_text(
        kind=kind, body=body, mentions=qqs, settings=settings, umo=target
    )


# --------------------------------------------------------------------------- #
# 插件侧：取件与回执
# --------------------------------------------------------------------------- #
async def pending(limit: int = 20) -> list[dict[str, Any]]:
    """待投递的消息（老消息在前）。取件本身就算「插件在线」——由调用方 :func:`mark_seen`。"""
    return await store.push_pending(limit=limit)


async def ack(item_id: str, *, ok: bool, detail: str = "") -> dict[str, Any]:
    """插件回执。

    * 发出去了 → 标 ``sent``；
    * 插件说发不出去 → **立刻**退回文本写法（不等超时，别让消息卡在队列里）。
    """
    if ok:
        done = await store.push_finish(item_id, status="sent", via="plugin", detail=detail)
        if done:
            log.info("真 @ 消息已由插件发出 | id=%s", item_id)
        return {"ok": True, "sent": True, "handled": done}
    item = await store.push_item(item_id)
    if item is None:
        return {"ok": False, "detail": "这条消息已经不在队列里"}
    log.warning("插件发不出去，改按文本写法发 | id=%s | %s", item_id, detail)
    return await send_text(
        kind=item["kind"],
        body=item["body"],
        mentions=item["mentions"],
        settings=store.qqbot_settings(),
        umo=item["umo"],
        item_id=item_id,
        detail=f"插件发失败：{detail}" if detail else "插件发失败",
    )


# --------------------------------------------------------------------------- #
# 巡检：超时退回 + 清理
# --------------------------------------------------------------------------- #
def _expired(item: dict[str, Any], *, now: datetime, seconds: int = FALLBACK_SECONDS) -> bool:
    stamp = _parse(item.get("createdAt") or "")
    if stamp is None:  # 时间读不出来：当作早就过期，别让它永远卡在队列里
        return True
    return now - stamp > timedelta(seconds=seconds)


async def tick(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """巡检一次：排太久的消息退回文本发出，顺带清掉旧的已收尾记录。"""
    moment = now or _now()
    settings = store.qqbot_settings()
    sent: list[dict[str, Any]] = []
    for item in await store.push_pending(limit=20):
        if not _expired(item, now=moment):
            continue
        if not settings.get("enabled"):
            # 推送整个关着：收尾掉（否则它会一直排在这里反复过期），原因写进 detail
            await store.push_finish(
                item["id"], status="failed", via="", detail="未启用 QQ 机器人推送"
            )
            continue
        log.warning("真 @ 消息等超时了，改按文本写法发 | id=%s", item["id"])
        sent.append(
            await send_text(
                kind=item["kind"],
                body=item["body"],
                mentions=item["mentions"],
                settings=settings,
                umo=item["umo"],
                item_id=item["id"],
                detail="插件没来取件，已按文本写法发出",
            )
        )
    await store.push_prune(KEEP_FINISHED)
    return sent


async def loop() -> None:
    """常驻巡检：每 :data:`TICK_SECONDS` 秒看一眼有没有排太久的消息。"""
    log.info("真 @ 投递巡检已启动 | 每 %d 秒一次", TICK_SECONDS)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 巡检不能因为一次异常就停摆（下一轮继续）
            log.warning("真 @ 投递巡检出错（已忽略，下一轮继续）", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
