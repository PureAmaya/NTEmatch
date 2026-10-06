"""打完后自动播报：**每打完一轮**，往群里发一次比赛结果。

三条底线：

* **一轮只播一次**（标记记在 meta，重启也不重发），但这一轮里的某场被**重置**之后，
  改完再打完要能重新播一次；
* **没打完就不播**（同一轮里还有一场在打，就不该先剧透）；
* **开关关掉一条都不发**。

这些用例跑在**自己新建的一届**上（会往当前届里塞赛程），用完删掉，不污染别的用例。
"""

from __future__ import annotations

import pytest

from app import announce, db, qqbot
from app.models import Round
from app.store import store


@pytest.fixture(autouse=True)
async def _own_event():
    """自己建一届再跑（与「参赛名单」那组同一个理由：别动别人正在用的一届）。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("自动播报用例届")
    mine = store.current_id
    try:
        yield
    finally:
        # 「已播报」标记是**全局**的：虽然键里带着届 id，但**届 id 会被复用**
        # （删掉 e002 之后新建又拿到 e002），不清干净下一条用例就会继承上一条的标记，
        # 看起来像「刚打完却不播报」。所以删届之前先把这一届的标记清掉。
        for key in announce.buckets(list(store.snapshot().rounds)):
            await store.set_meta(announce.mark_of(mine, key), "")
        if previous and previous != mine:
            await store.switch_event(previous)
        await store.delete_event(mine)


@pytest.fixture(autouse=True)
async def _push_on():
    """开着群推送（否则 tick 直接返回），用完恢复原样。"""
    saved = dict(store.qqbot_settings())
    await store.set_qqbot(
        {"enabled": True, "baseUrl": "http://astrbot.test", "apiKey": "abk_test", "umo": "123456"},
        actor="test",
        internal=True,
    )
    try:
        yield
    finally:
        await store.set_qqbot(saved, actor="test", internal=True)


@pytest.fixture(autouse=True)
def _forget_site():
    """本站对外地址是**进程内记忆**，用例之间要清掉（否则上一条会影响下一条发不发图）。"""
    announce.remember_site("")
    yield
    announce.remember_site("")


def _match(
    code: str,
    label: str,
    *,
    status: str = "done",
    winner: str = "A",
    round_no: int = 1,
    slot: int = 1,
    stage: str = "group",
) -> dict:
    return {
        "code": code,
        "stage": stage,
        "label": label,
        "bracketRound": round_no,
        "slot": slot,
        "status": status,
        "winner": winner,
        "sides": [
            {"playerIds": ["p01"], "score": 1},
            {"playerIds": ["p02"], "score": 0},
        ],
    }


async def _set_rounds(rows: list[dict]) -> None:
    """写进当前届：``index`` 是全局序号（要唯一），这里按顺序补上。"""
    for position, row in enumerate(rows, start=1):
        row["index"] = position
    await store.update(
        {
            "players": [
                {"id": "p01", "name": "甲", "qq": "10001"},
                {"id": "p02", "name": "乙", "qq": "10002"},
            ],
            "participants": [],
            "rounds": rows,
        }
    )


# --------------------------------------------------------------------------- #
# 「一轮」怎么算（纯函数）
# --------------------------------------------------------------------------- #
def test_bucket_key_groups_by_stage_and_round():
    """同一组同一轮算一轮；淘汰赛按阶段 + 轮次；积分制的局没有轮次概念。"""
    group = Round.model_validate(_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场"))
    assert announce.bucket_key(group) == "group:A:1"
    assert announce.bucket_label([group]) == "A 组 · 第 1 轮"
    wb = Round.model_validate(
        _match("WB-1-1", "半决赛 · 第 1 场", stage="wb", round_no=2, slot=1)
    )
    assert announce.bucket_label([wb]) == "半决赛"
    assert announce.bucket_key(wb) == "wb::2"


def test_finished_requires_every_match():
    """一轮「打完」= 这轮每场都有结果（还有一场没打就不算）。"""
    done = Round.model_validate(_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场"))
    live = Round.model_validate(
        _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", status="live", winner="", slot=2)
    )
    assert announce.finished([done]) is True
    assert announce.finished([done, live]) is False
    assert announce.finished([]) is False


# --------------------------------------------------------------------------- #
# 播报与去重
# --------------------------------------------------------------------------- #
async def test_tick_posts_the_finished_round_once(monkeypatch):
    """一轮打完 → 播一次；再 tick 不重复；下一轮打完再播一次。"""
    sent: list[str] = []

    async def fake_parts(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        sent.extend(parts)
        return {
            "ok": True,
            "status": 200,
            "detail": "",
            "sent": len(parts),
            "total": len(parts),
            "umo": umo,
            "at": "",
        }

    monkeypatch.setattr(qqbot, "send_parts", fake_parts)
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", slot=1),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", slot=2),
            _match(
                "G-A-2-1",
                "A 组 · 第 2 轮 · 第 1 场",
                status="pending",
                winner="",
                round_no=2,
                slot=1,
            ),
        ]
    )

    first = await announce.tick()
    assert [item["label"] for item in first] == ["A 组 · 第 1 轮"]
    assert first[0]["image"] is False, "不知道本站地址时只发文字"
    assert sent and "已赛" in sent[0], "文字里要有进度与结果"

    assert await announce.tick() == [], "同一轮只播一次"
    assert len(sent) == 1

    # 第 2 轮打完：再播一次（第 1 轮不会跟着重播）
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", slot=1),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", slot=2),
            _match("G-A-2-1", "A 组 · 第 2 轮 · 第 1 场", round_no=2, slot=1),
        ]
    )
    again = await announce.tick()
    assert [item["label"] for item in again] == ["A 组 · 第 2 轮"]


async def test_resetting_a_round_allows_another_announce(monkeypatch):
    """把这一轮里的某场重置 → 去掉标记 → 重新打完会**再播一次**（改过数据就该有新结果）。"""
    sent: list[str] = []

    async def fake_parts(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        sent.extend(parts)
        return {"ok": True, "status": 200, "detail": "", "sent": 1, "total": 1, "umo": umo, "at": ""}

    monkeypatch.setattr(qqbot, "send_parts", fake_parts)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert len(await announce.tick()) == 1
    assert await announce.tick() == []

    cfg = store.snapshot()
    assert await announce.forget(cfg, "G-A-1-1") is True
    assert len(await announce.tick()) == 1, "重置之后重新打完要能再播一次"


async def test_switched_off_sends_nothing(monkeypatch):
    """关掉「打完后自动播报」：一轮打完也一条都不发。"""
    sent: list[str] = []

    async def fake_parts(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        sent.extend(parts)
        return {"ok": True, "status": 200, "detail": "", "sent": 1, "total": 1, "umo": umo, "at": ""}

    monkeypatch.setattr(qqbot, "send_parts", fake_parts)
    await store.set_qqbot({"autoResultEnabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    assert sent == []


async def test_push_off_sends_nothing(monkeypatch):
    """群推送总开关关着时同样不发（自动播报不能绕过它）。"""
    sent: list[str] = []

    async def fake_parts(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        sent.extend(parts)
        return {"ok": True, "status": 200, "detail": "", "sent": 1, "total": 1, "umo": umo, "at": ""}

    monkeypatch.setattr(qqbot, "send_parts", fake_parts)
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    assert sent == []


async def test_failed_post_is_retried_next_tick(monkeypatch):
    """发失败**不记标记**：下一轮巡检还要再试（机器人刚重启时最常见）。"""
    ok = {"value": False}

    async def flaky(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        if not ok["value"]:
            return {"ok": False, "status": 502, "detail": "boom", "sent": 0, "total": 1, "umo": umo, "at": ""}
        return {"ok": True, "status": 200, "detail": "", "sent": 1, "total": 1, "umo": umo, "at": ""}

    monkeypatch.setattr(qqbot, "send_parts", flaky)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    ok["value"] = True
    assert len(await announce.tick()) == 1, "上次失败的那一轮这次要补上"
