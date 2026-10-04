"""比法走完整条链路：改配置 → 录入成绩 → 看状态。

单元测试盯的是函数，这里盯的是「管理员这样操作时会发生什么」：新字段
（``rules.metric``）有没有真的落库、录入接口有没有把它用上、下发给前端的
状态（含排行与规则文案）有没有跟着变。少任何一环，界面上就会出现
「预览说 A 快、结算说 B 赢」这种自相矛盾。
"""

from __future__ import annotations

from app import metrics
from app.store import store


async def _seed_league_time_event() -> str:
    """铺一场用时制的积分制对局（各一名选手），返回对局编号。

    直接用 store 写底层配置：这一步只是搭场景，不在被测范围内。
    """
    from app.models import Player, Round, Side

    play = Round(
        code="L-1",
        label="第 1 局",
        stage="league",
        sides=[Side(key="A", player_ids=["m1"]), Side(key="B", player_ids=["m2"])],
    )
    await store.update(
        {
            "rules": {"format": "league", "metric": "time", "minRankPlayed": 1},
            "teams": [],
            "players": [Player(id="m1", name="甲").dump(), Player(id="m2", name="乙").dump()],
            "participants": ["m1", "m2"],
            "rounds": [play.dump()],
        },
        actor="test",
    )
    return play.code


async def test_config_round_trips_metric(admin_client):
    """PUT /api/config 里的 metric 要真的生效并读得回来。"""
    res = await admin_client.put("/api/config", json={"rules": {"metric": "time"}})
    assert res.status_code == 200
    assert store.snapshot().rules.metric == "time"

    # 不认识的值一律回落计分制（模型层挡住，不往下传垃圾）
    await admin_client.put("/api/config", json={"rules": {"metric": "nonsense"}})
    assert store.snapshot().rules.metric == "score"


async def test_time_metric_reaches_state_and_rulebook(admin_client):
    """用时制必须体现在下发给前端的状态与规则文案里（观众看到的得和判罚一致）。"""
    await admin_client.put("/api/config", json={"rules": {"metric": "time"}})
    state = (await admin_client.get("/api/state")).json()

    assert state["rules"]["metric"] == "time"
    facts = state["rulebook"]["facts"]
    assert facts["metric"] == "time"
    assert facts["timeBased"] is True
    assert facts["metricLabel"] == "用时制"
    text = " ".join(
        item for section in state["rulebook"]["sections"] for item in section["items"]
    )
    assert "用时短的一方获胜" in text or "用时短者胜" in text


async def test_time_metric_result_is_judged_by_speed(admin_client):
    """录入接口真的按比法判定：更慢的一方不能因为「数字更大」而赢。"""
    code = await _seed_league_time_event()

    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "sides": [
                {"key": "A", "score": metrics.parse_time("2:00.000"), "points": 120000},
                {"key": "B", "score": metrics.parse_time("1:59.500"), "points": 119500},
            ],
        },
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "B", "用时制下更快的一方（B）必须获胜"
    assert [side.rank for side in settled.sides] == [2, 1]

    # 积分榜也跟着走：赢的一方在榜上，且带上了总用时
    state = (await admin_client.get("/api/state")).json()
    rows = {row["playerId"]: row for row in state["standings"]["players"]}
    assert rows["m2"]["win"] == 1 and rows["m2"]["rank"] == 1
    assert rows["m2"]["spent"] == metrics.parse_time("1:59.500")


async def test_score_metric_is_untouched_by_the_new_field(admin_client):
    """老比赛（没设过 metric）必须还是「分高者胜」——这是无损平移的底线。"""
    code = await _seed_league_time_event()
    await store.update({"rules": {"metric": "score"}}, actor="test")

    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "sides": [
                {"key": "A", "score": 15, "points": 0},
                {"key": "B", "score": 21, "points": 0},
            ],
        },
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "B"
