"""录入比分那一刻的自动播报：**一场一条，只讲这一场**。

（早先那版是把整届结果图发出去——图里全是之前打过的场次，等于刷屏，已经改掉。）

三条底线：

* **一场只播一次**（标记记在 meta，重启不重发），但这一场被**重置**之后，
  改完再录分要能重新播一次；
* **还没结果的场次不播**（有胜者才算）；
* **开关关掉 / 群推送关掉，一条都不发**。

这些用例跑在**自己新建的一届**上（会往当前届里塞赛程），用完删掉，不污染别的用例。
"""

from __future__ import annotations

import asyncio

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
        # 看起来像「刚录完分却不播报」。所以删届之前先把这一届的标记清掉。
        for rnd in store.snapshot().rounds:
            await store.set_meta(announce.mark_of(mine, rnd.code or str(rnd.index)), "")
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


def _match(
    code: str,
    label: str,
    *,
    status: str = "done",
    winner: str = "A",
    round_no: int = 1,
    slot: int = 1,
    stage: str = "group",
    score_a: int = 1,
    score_b: int = 0,
    started_at: str = "",
    finished_at: str = "",
) -> dict:
    return {
        "code": code,
        "stage": stage,
        "label": label,
        "bracketRound": round_no,
        "slot": slot,
        "status": status,
        "winner": winner,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "sides": [
            {"playerIds": ["p01"], "score": score_a},
            {"playerIds": ["p02"], "score": score_b},
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


@pytest.fixture
def captured(monkeypatch):
    """把发出去的消息攒起来（``send_parts`` 是唯一出口）。"""
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
    return sent


async def test_settling_a_match_pushes_this_match_right_away(admin_client, captured):
    """**录分那一刻**就发出去，不用等别的场次（这条盯的是接口到发送的接线）。"""
    await _set_rounds(
        [
            {
                "code": "L-1",
                "stage": "league",
                "label": "第 1 局",
                "bracketRound": 1,
                "slot": 1,
                "status": "pending",
                "winner": "",
                "sides": [
                    {"playerIds": ["p01"], "score": 0},
                    {"playerIds": ["p02"], "score": 0},
                ],
            }
        ]
    )
    res = await admin_client.post(
        "/api/rounds/L-1/result",
        json={"sets": [], "sides": [{"key": "A", "score": 3}, {"key": "B", "score": 1}]},
    )
    assert res.status_code == 200, res.text
    # 播报挂在结算请求后面（fire-and-forget），等它跑完再断言
    for _ in range(60):
        if captured:
            break
        await asyncio.sleep(0.02)
    assert captured, "录完分没有把这一场播报出去"
    assert "第 1 局" in captured[0] and "甲 3:1 vs 乙" in captured[0] and "胜方：甲" in captured[0]


# --------------------------------------------------------------------------- #
# 「这一场有结果了吗」（纯函数）
# --------------------------------------------------------------------------- #
def test_settled_needs_a_winner():
    """有胜者才算「有结果」：只把状态点成「已结束」不算（那种场次还没比分）。"""
    done = Round.model_validate(_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场"))
    live = Round.model_validate(_match("G-A-1-2", "A 组 · 第 2 轮 · 第 1 场", status="live", winner=""))
    open_end = Round.model_validate(
        _match("G-A-1-3", "A 组 · 第 3 轮 · 第 1 场", status="done", winner="")
    )
    assert announce.settled(done) is True
    assert announce.settled(live) is False
    assert announce.settled(open_end) is False
    assert announce.mark_of("e002", "G-A-1-1") == "announce:e002:G-A-1-1"


# --------------------------------------------------------------------------- #
# 播报内容与去重
# --------------------------------------------------------------------------- #
async def test_posts_once_per_match_and_only_about_that_match(captured):
    """一场一条；每条只讲自己那一场（**不带**之前打过的场次）。"""
    await _set_rounds(
        [
            _match(
                "G-A-1-1",
                "A 组 · 第 1 轮 · 第 1 场",
                score_a=3,
                score_b=1,
                started_at="2026-10-06T21:05",
                finished_at="2026-10-06T21:20",
            ),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", status="pending", winner="", slot=2),
        ]
    )

    first = await announce.tick()
    assert [item["ref"] for item in first] == ["G-A-1-1"]
    assert len(captured) == 1
    text = captured[0]
    assert "自动播报用例届 · 比赛结果" in text
    assert "A 组 · 第 1 轮 · 第 1 场" in text
    assert "甲 3:1 vs 乙" in text and "胜方：甲" in text
    assert "时间：2026年10月6日 21:05 → 21:20" in text
    assert "G-A-1-2" not in text and "第 2 场" not in text, "别的场次一个字都不该出现"
    assert "逐场比分" not in text and "冠军" not in text, "整届结果图那套内容不在自动播报里"

    assert await announce.tick() == [], "同一场只播一次"
    assert len(captured) == 1

    # 第 2 场录完分：只播第 2 场，第 1 场不会跟着重播
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", score_a=3, score_b=1),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", slot=2, score_a=0, score_b=2, winner="B"),
        ]
    )
    again = await announce.tick()
    assert [item["ref"] for item in again] == ["G-A-1-2"]
    assert len(captured) == 2
    assert "胜方：乙" in captured[1] and "甲 3:1 vs 乙" not in captured[1]


async def test_pending_matches_are_not_announced(captured):
    """还没结果的场次一条都不发。"""
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", status="live", winner=""),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", status="pending", winner="", slot=2),
        ]
    )
    assert await announce.tick() == []
    assert captured == []


async def test_resetting_a_match_allows_another_announce(captured):
    """把这一场重置 → 去掉标记 → 重新录分会**再播一次**（改过数据就该有新结果）。"""
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert len(await announce.tick()) == 1
    assert await announce.tick() == []

    cfg = store.snapshot()
    assert await announce.forget(cfg, "G-A-1-1") is True
    assert len(await announce.tick()) == 1, "重置之后重新录分要能再播一次"
    assert await announce.forget(cfg, "G-A-1-1") is True


async def test_switched_off_sends_nothing(captured):
    """关掉「打完后自动播报」：录完分也一条都不发。"""
    await store.set_qqbot({"autoResultEnabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    assert captured == []


async def test_push_off_sends_nothing(captured):
    """群推送总开关关着时同样不发（自动播报不能绕过它）。"""
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    assert captured == []


async def test_failed_post_is_retried_next_tick(monkeypatch):
    """发失败**不记标记**：下一轮巡检还要再试（机器人刚重启时最常见）。"""
    ok = {"value": False}
    sent: list[str] = []

    async def flaky(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        if not ok["value"]:
            return {
                "ok": False,
                "status": 502,
                "detail": "boom",
                "sent": 0,
                "total": 1,
                "umo": umo,
                "at": "",
            }
        sent.extend(parts)
        return {"ok": True, "status": 200, "detail": "", "sent": 1, "total": 1, "umo": umo, "at": ""}

    monkeypatch.setattr(qqbot, "send_parts", flaky)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.tick() == []
    ok["value"] = True
    assert len(await announce.tick()) == 1, "上次没发出去的那一场这次要补上"
    assert sent and "甲 1:0 vs 乙" in sent[0]
