"""打完一场就播报一场：**录入比分那一刻**，把这一场的结果发到群里。

只讲**这一场**：正文是这一场的对阵 / 比分 / 胜方 / 时间，**不带别的场次**
（整届的结果图由「推送到群 → 比赛结果」手工发，那是另一回事——自动播报要是把之前
打过的场次再列一遍，就是刷屏）。

三条硬规矩：

* **只由录分触发，绝不补发**：录分 / 判弃权那一刻发**这一场**，没有定时巡检，
  也不去扫「哪些场次有结果但还没发过」。扫历史是个坑：升级一次（标记键格式变过）、
  重启一次、打开一次网站，就会把**整届打过的场次**一条条重发进群
  （用户明确要的是「录完分才发」这一件事，不是「有结果就发」）。
* **一场只播一次**：标记记在 ``meta``（键 ``announce:<届>:<场次编号>``），重启也不重发；
  把这一场**重置**之后标记会清掉，改完重新录分能再播一次。
* **发失败当场再试两次就作罢**：不写标记、只记日志——**这场**下次录分（或重置后重录）
  还会再试。要人工补一条就用「推送到群 → 比赛结果」，不必靠后台补发。

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

#: 去重标记前缀（meta 表里的键：``announce:<届 id>:<场次编号>``）
MARK_PREFIX = "announce:"
#: 发失败之后**当场**再试的间隔（秒）。只发生在录分那一刻的这一次调用里，
#: 不做任何后台补发（见模块说明）；测试里可覆盖成 ``(0, 0)`` 免得白等。
RETRY_DELAYS: tuple[float, ...] = (2.0, 6.0)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005


def mark_of(event_id: str, ref: str) -> str:
    """去重标记的 meta 键（**一场一个**）。"""
    return f"{MARK_PREFIX}{event_id}:{ref}"


def settled(rnd: Round) -> bool:
    """这一场是不是**已经有结果**（有胜者，或规则允许的平局）。"""
    return rnd.status == "done" and bool(rnd.winner)


def find_round(cfg: Any, ref: str) -> Round | None:
    """按对局编号（``code``，如 WB-1-2）或全局序号定位一场比赛。

    与接口定位对局是同一套规则（``app/main.py`` 的 ``_find_round``）。
    """
    target = str(ref or "").strip()
    if not target:
        return None
    return next((r for r in cfg.rounds if str(r.code or "") == target), None) or next(
        (r for r in cfg.rounds if str(r.index) == target), None
    )


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


async def announce_round(cfg: Any, rnd: Round, *, event_id: str = "") -> dict[str, Any] | None:
    """把**这一场**的结果发出去；没有结果 / 已经播报过 / 开关没开 → 返回 ``None``。

    ``cfg`` 必须是**已经落库的那一份**（调用方从 ``store.mutate`` 拿到的就是），
    这样正文里的比分与刚录进去的完全一致。
    """
    settings = store.qqbot_settings()
    if not settings.get("enabled") or not settings.get("autoResultEnabled", True):
        return None
    if not settled(rnd):
        return None
    target = event_id or store.current_id
    ref = rnd.code or str(rnd.index)
    mark = mark_of(target, ref)
    if (await store.meta(mark)).strip():
        return None  # 这一场播报过了（改比分不重发；重置之后标记会清掉）
    # 只这一场：整届结果图不在这里发（见模块说明）
    view = logic.round_view(cfg, rnd)
    text = qqbot.to_plain_text(qqbot.build_match_result_message(cfg, view))
    parts = qqbot.split_message(text, int(settings.get("maxChars") or 1200))
    result: dict[str, Any] = {"ok": False, "detail": ""}
    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            await asyncio.sleep(RETRY_DELAYS[attempt - 1])
        result = await qqbot.send_parts(parts, settings=settings)
        if result.get("ok"):
            break
    if not result.get("ok"):
        # 不记标记：这场下次录分还会再试（没有后台巡检来补，见模块说明）
        log.warning(
            "自动播报失败（这场不记标记，下次录分会再试）| 场=%s | %s",
            ref,
            result.get("detail"),
        )
        return None
    await store.set_meta(mark, _now())
    log.warning(
        "已自动播报比赛结果 | 届=%s | 场=%s | %s",
        target,
        ref,
        view.get("label") or "",
    )
    return {"ref": ref, "label": view.get("label") or "", "text": text}


async def after_settle(ref: str, cfg: Any = None) -> dict[str, Any] | None:
    """录分 / 判弃权那一刻的入口：**只发 ``ref`` 这一场**（fire-and-forget）。

    绝不抛异常：它挂在录分请求后面，出错也不能影响录分本身。也**不看别的场次**——
    哪怕库里还躺着十场有结果、没播报过的历史成绩，这一次也只说刚录的这场。
    """
    try:
        current = cfg if cfg is not None else store.snapshot()
        rnd = find_round(current, ref)
        if rnd is None:
            return None
        return await announce_round(current, rnd)
    except asyncio.CancelledError:
        raise
    except Exception:  # 附加动作出错不该把录分请求带崩
        log.warning("自动播报出错（已忽略，这场下次录分还会再试）", exc_info=True)
        return None
