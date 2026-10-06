"""计分口径走完整条链路：改配置 → 录入成绩 → 看状态。

单元测试盯的是函数，这里盯的是「管理员这样操作时会发生什么」：计分类型 / 标签 /
判断标准有没有真的落库、录入接口有没有把它用上、下发给前端的状态（含排行与规则
文案）有没有跟着变。少任何一环，界面上就会出现「预览说 A 快、结算说 B 赢」
这种自相矛盾。
"""

from __future__ import annotations

from app import metrics
from app.models import Player, Round, Side
from app.store import store


async def _seed_league_round(
    *, value_type: str = "time", better: str = "low", legacy: bool = False
) -> str:
    """铺一场两方对局（各一名选手），返回对局编号。

    ``legacy=True`` 时只写**旧口径的 metric**（模拟这个字段出现之前的库）。
    直接用 store 写底层配置：这一步只是搭场景，不在被测范围内。
    """
    play = Round(
        code="L-1",
        label="第 1 局",
        stage="league",
        sides=[Side(key="A", player_ids=["m1"]), Side(key="B", player_ids=["m2"])],
    )
    rules: dict[str, object] = {"format": "league", "minRankPlayed": 1}
    if legacy:
        # 清空新字段、只留旧口径：这才是「这个字段出现之前的库」的样子
        rules.update({"valueType": "", "valueLabel": "", "better": "", "metric": "score"})
    else:
        rules.update({"valueType": value_type, "valueLabel": "", "better": better, "metric": ""})
    await store.update(
        {
            "rules": rules,
            "teams": [],
            "players": [Player(id="m1", name="甲").dump(), Player(id="m2", name="乙").dump()],
            "participants": ["m1", "m2"],
            "rounds": [play.dump()],
        },
        actor="test",
    )
    return play.code


async def test_config_round_trips_scoring(admin_client):
    """PUT /api/config 里的三件套要真的生效并读得回来。"""
    res = await admin_client.put(
        "/api/config",
        json={"rules": {"valueType": "time", "valueLabel": "用时", "better": "low"}},
    )
    assert res.status_code == 200
    rules = store.snapshot().rules
    assert (rules.value_type, rules.value_label, rules.better) == ("time", "用时", "low")
    assert rules.metric == "time", "旧口径名要跟着同步（老版本读得回来）"

    # 不认识的值一律回落（模型层挡住，不往下传垃圾）
    await admin_client.put(
        "/api/config", json={"rules": {"valueType": "nonsense", "better": "nonsense"}}
    )
    rules = store.snapshot().rules
    assert (rules.value_type, rules.better) == ("integer", "high")


async def test_label_can_be_custom(admin_client):
    """计分标签可以自定义（预设之外照收）。"""
    await admin_client.put("/api/config", json={"rules": {"valueLabel": "击杀数"}})
    assert store.snapshot().rules.scoring.label_text == "击杀数"


async def test_scoring_reaches_state_and_rulebook(admin_client):
    """计分口径必须体现在下发给前端的状态与规则文案里（观众看到的得和判罚一致）。"""
    await admin_client.put(
        "/api/config",
        json={"rules": {"valueType": "time", "valueLabel": "用时", "better": "low"}},
    )
    state = (await admin_client.get("/api/state")).json()

    assert state["rules"]["valueType"] == "time"
    assert state["scoring"]["valueType"] == "time"
    assert state["scoring"]["valueLabel"] == "用时"
    assert state["scoring"]["better"] == "low"
    assert state["scoring"]["betterLabel"] == "数值低胜"

    text = " ".join(
        item for section in state["rulebook"]["sections"] for item in section["items"]
    )
    assert "数值低胜" in text
    assert "用时" in text


async def test_low_wins_result_is_judged_by_speed(admin_client):
    """录入接口真的按判断标准判定：更慢的一方不能因为「数字更大」而赢。"""
    code = await _seed_league_round(value_type="time", better="low")

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
    assert settled.winner == "B", "数值低胜下更快的一方（B）必须获胜"
    assert [side.rank for side in settled.sides] == [2, 1]

    # 积分榜也跟着走：赢的一方在榜上，且带上了总用时
    state = (await admin_client.get("/api/state")).json()
    rows = {row["playerId"]: row for row in state["standings"]["players"]}
    assert rows["m2"]["win"] == 1 and rows["m2"]["rank"] == 1
    assert rows["m2"]["spent"] == metrics.parse_time("1:59.500")


async def test_rounds_are_settled_into_a_series_score(admin_client):
    """录入多轮：接口回来的大比分与总成绩要自动算好，落库也要是同一份。"""
    code = await _seed_league_round(value_type="integer", better="high")

    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [
                {"a": 25, "b": 20},
                {"a": 20, "b": 25},
                {"a": 25, "b": 18},
            ],
            "sides": [{"key": "A", "score": 0}, {"key": "B", "score": 0}],
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["winner"] == "A"
    ranking = {item["key"]: item for item in body["ranking"]}
    assert (ranking["A"]["score"], ranking["B"]["score"]) == (2, 1), "大比分 = 赢的轮数"
    assert (ranking["A"]["points"], ranking["B"]["points"]) == (70, 63), "总成绩 = 各轮合计"

    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert len(settled.sets) == 3
    assert (settled.sides[0].score, settled.sides[1].score) == (2, 1)


async def test_legacy_score_event_is_untouched(admin_client):
    """老比赛（只设过 metric=score）必须还是「分高者胜」——这是无损平移的底线。"""
    code = await _seed_league_round(legacy=True)
    rules = store.snapshot().rules
    assert (rules.value_type, rules.value_label, rules.better) == ("integer", "得分", "high")

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


async def test_decimal_scores_survive_the_round_trip(admin_client):
    """小数型：千分之一存值原样往返，显示时去掉多余的 0。"""
    code = await _seed_league_round(value_type="decimal", better="high")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [{"a": metrics.parse_decimal("8.75"), "b": metrics.parse_decimal("8.5")}],
            "sides": [{"key": "A", "score": 0}, {"key": "B", "score": 0}],
        },
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.sets[0].a == 8750
    sc = store.snapshot().rules.scoring
    assert sc.format(settled.sets[0].a) == "8.75"
    assert sc.format(settled.sets[0].b) == "8.5"


async def test_zero_is_recorded_as_a_real_score(admin_client):
    """0 分是成绩：接口原样存 0（不能变成「没填」），0:5 依旧判 B 胜。"""
    code = await _seed_league_round(value_type="integer", better="high")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={"sets": [], "sides": [{"key": "A", "score": 0}, {"key": "B", "score": 5}]},
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert (settled.sides[0].score, settled.sides[1].score) == (0, 5)
    assert settled.winner == "B"

    state = (await admin_client.get("/api/state")).json()
    rnd = next(item for item in state["rounds"] if item["code"] == code)
    assert rnd["sides"][0]["score"] == 0, "0 是合法读数，不能在链路里变成「没有成绩」"


async def test_zero_wins_when_low_wins(admin_client):
    """数值低胜 + 自然数：0 是最好的成绩（罚时 0 / 杆数 0），它就该赢。"""
    code = await _seed_league_round(value_type="integer", better="low")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={"sets": [], "sides": [{"key": "A", "score": 0}, {"key": "B", "score": 3}]},
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "A"


async def test_missing_is_not_the_same_as_zero(admin_client):
    """「没填」走 -1 哨兵，不能和 0 分混为一谈：数值低胜下 0 分是会赢的。"""
    code = await _seed_league_round(value_type="integer", better="low")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={"sets": [], "sides": [{"key": "A", "score": -1}, {"key": "B", "score": 3}]},
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.sides[0].score == -1
    assert settled.winner == "B", "没有成绩的一方不能靠「0 最好」这个歧义获胜"
    assert store.snapshot().rules.scoring.format(settled.sides[0].score) == "—"
