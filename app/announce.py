"""打完后自动播报：**每打完一轮**，往群里发一次「比赛结果」。

为什么放在站点：只有站点知道「这一轮打完了没有」，也只有它握着群推送通道
（与赛前提醒同一套理由，见 :mod:`app.remind`）。

三条硬规矩：

* **一轮只播一次**：标记记在 ``meta``（键 ``announce:<届>:<阶段>:<组>:<轮>``），
  重启也不会重发；把这一轮里的某场**重置**之后标记会清掉，重新打完可以再播一次；
* **发失败不记标记**：下一轮巡检会重试——宁可晚一点，也不能漏一条；
* **图片只是锦上添花**：本站对外地址来自**触发结算的那次请求**（只放内存，故意不落库：
  它是从 Host 头推出来的，落库等于把一个可伪造的值长期留着）；不知道地址时只发文字，
  信息一条不少——画得出图就带图，画不出 / 发不出去照样把结果说清楚。

与手工推送的区别：自动播报**不占推送限流额度**（它由赛程天然限流：一轮一条），
但同样要在「服务器 → QQ 机器人」里开着推送、且开着「打完后自动播报」。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from . import card, logic, qqbot
from .logging_conf import get_logger
from .models import Round
from .store import store

log = get_logger("announce")

#: 巡检间隔（秒）。结算那一刻会立刻试一次，这里是**兜底**：进程重启、上次发失败、
#: 或者分两次录完同一轮——一分钟内补上，不必等人再动一次手。
TICK_SECONDS = 60
#: 去重标记前缀（meta 表里的键：``announce:<届 id>:<阶段>:<组>:<轮>``）
MARK_PREFIX = "announce:"

#: 本次进程里见过的本站对外地址（由带请求的动作记下来）。**故意只放内存**，见模块说明。
_SITE_BASE = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005


def remember_site(base: str) -> None:
    """记下本站对外地址（结算等带请求的动作会调它）。"""
    global _SITE_BASE
    text = str(base or "").strip().rstrip("/")
    if text:
        _SITE_BASE = text


def site_base() -> str:
    """当前知道的本站对外地址（空 = 还不知道，这时只发文字）。"""
    return _SITE_BASE


# --------------------------------------------------------------------------- #
# 「一轮」怎么算
# --------------------------------------------------------------------------- #
def bucket_key(rnd: Round) -> str:
    """这一场属于哪一轮：``阶段:组:轮次``。

    * 小组赛：按「组 + 轮次」——``A 组第 1 轮`` 的几场都打完，才算这一轮打完；
    * 淘汰赛：按「阶段 + 轮次」——胜者组第 1 轮 / 败者组第 2 轮 / 总决赛各算一轮；
    * 积分制：局没有轮次概念（都是第 1 轮），于是**整届打完**才算一轮。
    """
    group = ""
    if rnd.stage == "group":
        parts = str(rnd.code or "").split("-")
        group = parts[1] if len(parts) >= 2 and parts[0] == "G" else ""
    return f"{rnd.stage}:{group}:{int(rnd.bracket_round or 1)}"


def buckets(rounds: list[Round]) -> dict[str, list[Round]]:
    """把赛程按「轮」归拢（保持赛程里的先后顺序）。"""
    out: dict[str, list[Round]] = {}
    for rnd in rounds:
        out.setdefault(bucket_key(rnd), []).append(rnd)
    return out


def bucket_label(rounds: list[Round]) -> str:
    """这一轮叫什么：``A 组 · 第 1 轮`` / ``半决赛`` / ``总决赛``。"""
    label = str(rounds[0].label or rounds[0].code or "").strip()
    parts = [part for part in label.split(" · ") if part]
    # 「第 1 场」这一截属于**场**而不属于**轮**，播报时去掉
    if len(parts) > 1 and re.fullmatch(r"第 \d+ 场", parts[-1]):
        parts = parts[:-1]
    return " · ".join(parts) or str(rounds[0].stage)


def finished(rounds: list[Round]) -> bool:
    """这一轮是不是**全都打完了**（每场都有明确胜者 / 平局）。"""
    return bool(rounds) and all(r.status == "done" and r.winner for r in rounds)


def mark_of(event_id: str, key: str) -> str:
    """去重标记的 meta 键。"""
    return f"{MARK_PREFIX}{event_id}:{key}"


async def due(cfg: Any, event_id: str = "") -> list[dict[str, Any]]:
    """现在该播报哪几轮：已打完、且还没播报过（按赛程顺序）。"""
    target = event_id or store.current_id
    out: list[dict[str, Any]] = []
    for key, rounds in buckets(list(cfg.rounds)).items():
        if not finished(rounds):
            continue
        if (await store.meta(mark_of(target, key))).strip():
            continue
        out.append({"key": key, "label": bucket_label(rounds), "codes": [r.code for r in rounds]})
    return out


async def forget(cfg: Any, ref: str, event_id: str = "") -> bool:
    """把 ``ref`` 那一场的「已播报」标记清掉（重置一场时调它）。

    清掉之后这一轮再打完时会**重新播报一次**：重置本来就是为了改数据，
    改完不给个新结果反而奇怪。
    """
    rnd = next(
        (r for r in cfg.rounds if (r.code or str(r.index)) == str(ref or "").strip()), None
    )
    if rnd is None:
        return False
    key = mark_of(event_id or store.current_id, bucket_key(rnd))
    if not (await store.meta(key)).strip():
        return False
    await store.set_meta(key, "")
    return True


# --------------------------------------------------------------------------- #
# 播报
# --------------------------------------------------------------------------- #
def _header(cfg: Any, item: dict[str, Any], done: int, total: int) -> str:
    """有图时跟的那两行（图里已经有逐场比分与对阵，正文只报进度）。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    return f"【NTE 比赛】{name} · {item['label']} 已打完\n已赛 {done} / {total} 场 · 逐场结果见图"


async def _post(
    cfg: Any, state: dict[str, Any], item: dict[str, Any], settings: dict[str, Any], site: str
) -> dict[str, Any]:
    """发一条播报：能画结果图就带图，否则退回**完整文字**（信息一条不少）。"""
    rounds = state.get("rounds") or []
    done = sum(1 for rnd in rounds if rnd.get("status") == "done")
    header = _header(cfg, item, done, len(rounds))
    if site and settings.get("imageCards", True) and card.available():
        info = await card.card_for_event(
            cfg, store.current_id, state, site=site, kind="result"
        )
        if info:
            blob = info.get("bytes") or card.card_bytes(str(info.get("hash") or ""))
            sent = await qqbot.send_image(
                f"{site}{info['url']}",
                settings=settings,
                blob=blob,
                filename=f"result-{info.get('hash')}.png",
            )
            if sent.get("ok"):
                tail = await qqbot.send_parts([header], settings=settings)
                return {
                    "ok": bool(tail.get("ok")),
                    "detail": tail.get("detail") or "",
                    "image": True,
                    "text": header,
                }
            log.warning("结果图没发出去，改发完整文字 | %s", sent.get("detail"))
    parts = qqbot.split_message(
        qqbot.to_plain_text(qqbot.build_result_message(cfg, state)),
        int(settings.get("maxChars") or 1200),
    )
    result = await qqbot.send_parts(parts, settings=settings)
    return {
        "ok": bool(result.get("ok")),
        "detail": result.get("detail") or "",
        "image": False,
        "text": "\n".join(parts),
    }


async def tick(cfg: Any = None, *, site: str = "", event_id: str = "") -> list[dict[str, Any]]:
    """巡检一次，返回这次**真的播报出去**的那几轮（测试与日志用）。

    一条都没发是常态（没开开关 / 没有刚打完的轮 / 已经播报过都不算异常）。
    """
    settings = store.qqbot_settings()
    if not settings.get("enabled") or not settings.get("autoResultEnabled", True):
        return []
    current = cfg if cfg is not None else store.snapshot()
    pending = await due(current, event_id)
    if not pending:
        return []
    base = str(site or _SITE_BASE or "").rstrip("/")
    state = logic.build_state(current)
    sent: list[dict[str, Any]] = []
    for item in pending:
        info = await _post(current, state, item, settings, base)
        if not info.get("ok"):
            # 不记标记：下一轮巡检还会再试（机器人恢复后照样播报得上）
            log.warning(
                "自动播报失败（稍后重试）| 轮=%s | %s", item["label"], info.get("detail")
            )
            continue
        await store.set_meta(mark_of(event_id or store.current_id, item["key"]), _now())
        log.warning(
            "已自动播报比赛结果 | 届=%s | 轮=%s | 图片=%s",
            event_id or store.current_id,
            item["label"],
            "有" if info.get("image") else "无",
        )
        sent.append({**item, "image": bool(info.get("image")), "text": info.get("text") or ""})
    return sent


async def after_settle(*, site: str = "") -> None:
    """结算之后立刻试播一次（**fire-and-forget**，由录分接口起个任务就跑）。

    绝不抛异常：它挂在录分请求后面，出错也不能影响录分本身；漏掉的那次由巡检兜。
    """
    remember_site(site)
    try:
        await tick(site=site)
    except asyncio.CancelledError:
        raise
    except Exception:  # 附加动作出错不该把录分请求带崩
        log.warning("自动播报出错（已忽略，巡检会重试）", exc_info=True)


async def loop() -> None:
    """常驻巡检：每隔 :data:`TICK_SECONDS` 秒看一眼有没有刚打完、还没播报的轮。"""
    log.info("自动播报已启动 | 每 %d 秒巡检一次", TICK_SECONDS)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 巡检不能因为一次异常就停摆（下一轮继续）
            log.warning("自动播报巡检出错（已忽略，下一轮继续）", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
