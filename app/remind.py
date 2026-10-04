"""赛前提醒：开赛前在群里 @ 举办者。

为什么放在**站点**而不是插件里：只有站点知道开赛时间（读的是届配置），也只有站点
握着群推送通道。插件跑在 AstrBot 里、每次命令都是被动的，要做定时任务还得依赖框架
的调度能力——多一套环境就多一处会坏的地方。这里复用常驻巡检（和自动备份同一套写法）。

两条硬规矩：

* **去重**：同一条提前量只发一次，标记记在 ``meta`` 表里（进程重启也记得）；
* **窗口**：只在「提前量 − 1 小时」到「提前量」之间发。服务器中途重启时，不会把
  「明天开赛」补发成「还有 3 小时开赛」——**宁可少发一条，也不发一条错的**。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from . import logic, qqbot
from .logging_conf import get_logger
from .store import store

log = get_logger("remind")

#: 巡检间隔（秒）。提醒的粒度是小时级，5 分钟足够及时，也不必每分钟都去读库。
TICK_SECONDS = 300
#: 允许的迟到窗口（分钟）：见模块文档的「窗口」
WINDOW_MINUTES = 60
#: 去重标记的前缀（meta 表里的键：``remind:<届 id>:<提前量>``）
MARK_PREFIX = "remind:"


def _now() -> datetime:
    return datetime.now()  # noqa: DTZ005


def due_leads(start_time: str, leads: list[int], *, now: datetime | None = None) -> list[int]:
    """现在该发哪几条提醒（已按窗口过滤；是否已发过由 :func:`tick` 判断）。"""
    start = logic.parse_time(start_time or "")
    if start is None:
        return []
    now = now or _now()
    left = (start - now).total_seconds() / 60
    if left <= 0:  # 已经开赛（或时间已过）
        return []
    # 下界留 1 分钟余量：`left` 是浮点分钟，边界上（比如正在「还剩 23 小时」那一刻）
    # 会因为几毫秒抖动算成 1379.999，被自己的窗口挡在门外——实测踩过这个坑。
    floor = WINDOW_MINUTES + 1
    return [lead for lead in leads if lead - floor <= left <= lead]


async def tick(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """巡检一次，返回这次**真的发出去**的提醒（便于测试与日志）。

    一条都没发是常态（没到点 / 没开开关 / 举办者没登记 QQ 都不算异常）。
    """
    settings = store.qqbot_settings()
    if not settings.get("enabled") or not settings.get("remindEnabled"):
        return []
    leads = qqbot.remind_leads(settings)
    if not leads:
        return []

    cfg = store.snapshot()
    event = cfg.event
    # 已结束 / 已开赛（锁定）的届不再提醒：那时候提醒只会打扰人
    if event.status == "closed" or event.locked:
        return []

    candidates = due_leads(event.start_time, leads, now=now)
    if not candidates:
        return []

    owner = store.member(event.owner_uid) if event.owner_uid else None
    if owner is None:
        # 没记归属的届（老库 / 出厂那一个）退到服务器管理员：总得 @ 到一个人，
        # 否则提醒发出去也没人负责。
        owner = store.server_admin()
    owner_name = owner.display_name if owner else ""
    owner_qq = (owner.qq or "").strip() if owner else ""

    sent: list[dict[str, Any]] = []
    for lead in candidates:
        mark = f"{MARK_PREFIX}{store.current_id}:{lead}"
        if (await store.meta(mark)).strip():
            continue
        text = qqbot.build_remind_message(
            cfg,
            lead_minutes=lead,
            owner_qq=owner_qq,
            owner_name=owner_name,
            settings=settings,
        )
        result = await qqbot.send_text(text, settings=settings)
        if not result.get("ok"):
            # 发失败**不记标记**：下一轮还会再试（机器人恢复后照样提醒得上）
            log.warning("赛前提醒发送失败 | 提前=%s 分钟 | %s", lead, result.get("detail"))
            continue
        await store.set_meta(mark, _now().isoformat(timespec="seconds"))
        log.warning(
            "赛前提醒已发送 | 届=%s | 提前=%s 分钟 | 举办者=%s",
            store.current_id,
            lead,
            owner_name or "(未登记 / 无成员)",
        )
        sent.append(
            {
                "eventId": store.current_id,
                "lead": lead,
                "owner": owner_name,
                "hasOwnerQq": bool(owner_qq),
                "text": text,
            }
        )
    return sent


async def loop() -> None:
    """常驻巡检：每 :data:`TICK_SECONDS` 秒看一眼有没有到点的提醒。"""
    log.info("赛前提醒已启动 | 每 %d 秒巡检一次", TICK_SECONDS)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 巡检不能因为一次异常就停摆（下一轮继续）
            log.warning("赛前提醒巡检出错（已忽略，下一轮继续）", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
