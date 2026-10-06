"""打完一场就播报一场：**录入比分那一刻**，把这一场的结果发到群里。

只讲**这一场**：正文是这一场的对阵 / 比分 / 胜方 / 时间，**不带别的场次**
（整届的结果图由「推送到群 → 比赛结果」手工发，那是另一回事——自动播报要是把之前
打过的场次再列一遍，就是刷屏）。

三条硬规矩：

* **一场只播一次**：标记记在 ``meta``（键 ``announce:<届>:<场次编号>``），重启也不重发；
  把这一场**重置**之后标记会清掉，改完重新录分能再播一次；
* **发失败不记标记**：下一轮巡检会重试——宁可晚一点，也不能漏一条；
* **结算那一刻就发**：录分 / 判弃权之后立刻起个后台任务试一次（**不等它**，别拖慢录分），
  巡检（60 秒）只是兜底（进程重启、上次发失败、一场分两次录）。

与手工推送的区别：自动播报**不占推送限流额度**（它由录入节奏天然限流：一场一条），
但同样要在「服务器 → QQ 机器人」里开着推送、且开着「打完后自动播报」。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from . import logic, qqbot
from .logging_conf import get_logger
from .models import Round
from .store import store

log = get_logger("announce")

#: 巡检间隔（秒）。结算那一刻会立刻试一次，这里是**兜底**：进程重启、上次发失败。
TICK_SECONDS = 60
#: 去重标记前缀（meta 表里的键：``announce:<届 id>:<场次编号>``）
MARK_PREFIX = "announce:"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005


def mark_of(event_id: str, ref: str) -> str:
    """去重标记的 meta 键（**一场一个**）。"""
    return f"{MARK_PREFIX}{event_id}:{ref}"


def settled(rnd: Round) -> bool:
    """这一场是不是**已经有结果**（有胜者，或规则允许的平局）。"""
    return rnd.status == "done" and bool(rnd.winner)


async def due(cfg: Any, event_id: str = "") -> list[Round]:
    """现在该播报哪几场：已有结果、且还没播报过（按赛程顺序）。"""
    target = event_id or store.current_id
    out: list[Round] = []
    for rnd in cfg.rounds:
        if not settled(rnd):
            continue
        if (await store.meta(mark_of(target, rnd.code or str(rnd.index)))).strip():
            continue
        out.append(rnd)
    return out


async def forget(cfg: Any, ref: str, event_id: str = "") -> bool:
    """把某一场的「已播报」标记清掉（重置一场时调它）。

    清掉之后这一场再结算时会**重新播报一次**：重置本来就是为了改数据，
    改完不给个新结果反而奇怪。
    """
    key = mark_of(event_id or store.current_id, str(ref or "").strip())
    if not (await store.meta(key)).strip():
        return False
    await store.set_meta(key, "")
    return True


async def tick(cfg: Any = None, *, event_id: str = "") -> list[dict[str, Any]]:
    """巡检一次，返回这次**真的播报出去**的那几场（测试与日志用）。

    一条都没发是常态（没开开关 / 没有刚打完的场 / 已经播报过都不算异常）。
    """
    settings = store.qqbot_settings()
    if not settings.get("enabled") or not settings.get("autoResultEnabled", True):
        return []
    current = cfg if cfg is not None else store.snapshot()
    pending = await due(current, event_id)
    if not pending:
        return []
    target = event_id or store.current_id
    sent: list[dict[str, Any]] = []
    for rnd in pending:
        # 只这一场：整届结果图不在这里发（见模块说明）
        view = logic.round_view(current, rnd)
        text = qqbot.to_plain_text(qqbot.build_match_result_message(current, view))
        parts = qqbot.split_message(text, int(settings.get("maxChars") or 1200))
        result = await qqbot.send_parts(parts, settings=settings)
        if not result.get("ok"):
            # 不记标记：下一轮巡检还会再试（机器人恢复后照样播报得上）
            log.warning(
                "自动播报失败（稍后重试）| 场=%s | %s",
                rnd.code or rnd.index,
                result.get("detail"),
            )
            continue
        await store.set_meta(mark_of(target, rnd.code or str(rnd.index)), _now())
        log.warning(
            "已自动播报比赛结果 | 届=%s | 场=%s | %s",
            target,
            rnd.code or rnd.index,
            view.get("label") or "",
        )
        sent.append(
            {
                "ref": rnd.code or str(rnd.index),
                "label": view.get("label") or "",
                "text": text,
            }
        )
    return sent


async def after_settle() -> None:
    """结算之后立刻试播一次（**fire-and-forget**，由录分接口起个任务就跑）。

    绝不抛异常：它挂在录分请求后面，出错也不能影响录分本身；漏掉的那次由巡检兜。
    """
    try:
        await tick()
    except asyncio.CancelledError:
        raise
    except Exception:  # 附加动作出错不该把录分请求带崩
        log.warning("自动播报出错（已忽略，巡检会重试）", exc_info=True)


async def loop() -> None:
    """常驻巡检：每隔 :data:`TICK_SECONDS` 秒看一眼有没有刚打完、还没播报的场。"""
    log.info("自动播报已启动 | 每 %d 秒巡检一次", TICK_SECONDS)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 巡检不能因为一次异常就停摆（下一轮继续）
            log.warning("自动播报巡检出错（已忽略，下一轮继续）", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
