"""录入比分那一刻的自动播报：**一场一条，只讲这一场，只在录分那一刻**。

（早先那版是把整届结果图发出去——图里全是之前打过的场次，等于刷屏，已经改掉。
后来那版又留着个 60 秒巡检，会把「有结果、但库里没有播报标记」的场次一次全发出去：
升级一次（标记键格式变过）、重启一次、打开一次网站，整届打过的成绩就重播一遍。
所以现在**没有任何后台巡检**，只有录分 / 判弃权那一刻发这一场。）

四条底线：

* **打开网站 / 重启都不补发历史**（录分只发刚录的那一场）；
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
    """开着群推送（否则一条都发不出去），用完恢复原样。"""
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


def _ok_sender(sent: list[str]):
    """一个「发得出去」的假出口（把正文攒下来，方便断言）。"""

    async def sender(parts, *, settings=None, umo="", mentions=None, mention_text=""):
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

    return sender


@pytest.fixture
def captured(monkeypatch):
    """把发出去的消息攒起来（``send_parts`` 是唯一出口）。"""
    sent: list[str] = []
    monkeypatch.setattr(qqbot, "send_parts", _ok_sender(sent))
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


async def test_opening_the_site_never_replays_finished_matches(admin_client, captured):
    """**打开网站 / 重启不会补发历史**：库里已打完的场次一场都不许重发。

    这就是用户报的那个 bug：那些「有结果、但库里没有播报标记」的场次
    （标记键格式升级过、或录分时机器人还关着）被一次后台巡检扫出来，
    整届打过的成绩一条条发进群。现在没有任何后台巡检——录分只发刚录的那一场。
    """
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", score_a=3, score_b=1),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", slot=2, score_a=0, score_b=2, winner="B"),
            _match("G-A-2-1", "A 组 · 第 2 轮 · 第 1 场", round_no=2, slot=3),
            _match("G-A-2-2", "A 组 · 第 2 轮 · 第 2 场", round_no=2, slot=4),
        ]
    )
    # 「打开网站」会做的事：拉一次状态；多人同时打开 / 断线重连会再来一次
    await admin_client.get("/api/state")
    await admin_client.get("/api/state")
    assert captured == [], "打开网站把历史成绩重发了一遍"

    # 只有**刚录的这一场**会发：另外三场哪怕有结果、没标记，也不在这一次里
    cfg = store.snapshot()
    sent = await announce.after_settle("G-A-2-2", cfg)
    assert sent and sent["ref"] == "G-A-2-2"
    assert len(captured) == 1
    assert "第 2 轮 · 第 2 场" in captured[0]
    assert "第 1 轮" not in captured[0], "别的场次一个字都不该出现"


def test_no_background_scanner_at_all():
    """不许再有「扫一遍所有有结果的场次」这种入口——历史被重发就是它干的。

    补发的诱惑很大（「漏发一条怎么办」），但那会把整届成绩重刷进群；
    要人工补就用「推送到群 → 比赛结果」，那条是**手动**的。
    """
    assert not hasattr(announce, "tick"), "巡检入口又回来了"
    assert not hasattr(announce, "loop"), "巡检任务又回来了"


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

    first = await announce.after_settle("G-A-1-1")
    assert first and first["ref"] == "G-A-1-1"
    assert len(captured) == 1
    text = captured[0]
    assert "自动播报用例届 · 比赛结果" in text
    assert "A 组 · 第 1 轮 · 第 1 场" in text
    assert "甲 3:1 vs 乙" in text and "胜方：甲" in text
    assert "时间：2026年10月6日 21:05 → 21:20" in text
    assert "G-A-1-2" not in text and "第 2 场" not in text, "别的场次一个字都不该出现"
    assert "逐场比分" not in text and "冠军" not in text, "整届结果图那套内容不在自动播报里"

    assert await announce.after_settle("G-A-1-1") is None, "同一场只播一次"
    assert len(captured) == 1

    # 第 2 场录完分：只播第 2 场，第 1 场不会跟着重播
    await _set_rounds(
        [
            _match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场", score_a=3, score_b=1),
            _match("G-A-1-2", "A 组 · 第 1 轮 · 第 2 场", slot=2, score_a=0, score_b=2, winner="B"),
        ]
    )
    again = await announce.after_settle("G-A-1-2")
    assert again and again["ref"] == "G-A-1-2"
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
    assert await announce.after_settle("G-A-1-1") is None
    assert await announce.after_settle("G-A-1-2") is None
    assert await announce.after_settle("不存在的场次") is None
    assert captured == []


async def test_resetting_a_match_allows_another_announce(captured):
    """把这一场重置 → 去掉标记 → 重新录分会**再播一次**（改过数据就该有新结果）。"""
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.after_settle("G-A-1-1") is not None
    assert await announce.after_settle("G-A-1-1") is None

    cfg = store.snapshot()
    assert await announce.forget(cfg, "G-A-1-1") is True
    assert await announce.after_settle("G-A-1-1") is not None, "重置之后重新录分要能再播一次"
    assert await announce.forget(cfg, "G-A-1-1") is True


async def test_switched_off_sends_nothing(captured):
    """关掉「打完后自动播报」：录完分也一条都不发。"""
    await store.set_qqbot({"autoResultEnabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.after_settle("G-A-1-1") is None
    assert captured == []


async def test_push_off_sends_nothing(captured):
    """群推送总开关关着时同样不发（自动播报不能绕过它）。"""
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.after_settle("G-A-1-1") is None
    assert captured == []


async def test_failed_post_is_retried_in_place_then_given_up(monkeypatch, captured):
    """发失败：**当场**再试两次；仍失败就作罢（不写标记、不留给后台补发）。"""
    monkeypatch.setattr(announce, "RETRY_DELAYS", (0, 0))  # 别在测试里白等 8 秒
    attempts = {"n": 0}

    async def flaky(parts, *, settings=None, umo="", mentions=None, mention_text=""):
        attempts["n"] += 1
        return {
            "ok": False,
            "status": 502,
            "detail": "boom",
            "sent": 0,
            "total": 1,
            "umo": umo,
            "at": "",
        }

    monkeypatch.setattr(qqbot, "send_parts", flaky)
    await _set_rounds([_match("G-A-1-1", "A 组 · 第 1 轮 · 第 1 场")])
    assert await announce.after_settle("G-A-1-1") is None
    assert attempts["n"] == 3, "首试 + 两次重试，都在录分那一刻里"
    mark = announce.mark_of(store.current_id, "G-A-1-1")
    assert (await store.meta(mark)).strip() == "", "发失败不记标记"

    # 机器人恢复了：这场再录一次分（改比分 / 重置后重录）就能发出去
    monkeypatch.setattr(qqbot, "send_parts", _ok_sender(captured))
    assert await announce.after_settle("G-A-1-1") is not None
    assert captured and "甲 1:0 vs 乙" in captured[0]
