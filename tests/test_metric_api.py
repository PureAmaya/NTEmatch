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


async def _seed_multi_side_heat() -> str:
    """铺一场 **4 队同场**的小组赛（时间型：用时最短的是第 1）。

    胜方可能是 C / D —— 这正是「只认 A / B」那套假设会崩的地方：4 队那场由 C 赢时，
    落库会被判非法（``Input should be '', 'A', 'B' or 'DRAW'``，见 app/models.py 的 WinnerCode）。
    """
    heat = Round(
        code="G-A-1-1",
        label="A 组 · 第 1 轮 · 第 1 场",
        stage="group",
        bracket_round=1,
        slot=1,
        sides=[
            Side(key=key, team_id=f"t{i}", player_ids=[f"m{i}"])
            for i, key in enumerate(("A", "B", "C", "D"), start=1)
        ],
    )
    await store.update(
        {
            "rules": {
                "format": "tournament",
                "valueType": "time",
                "valueLabel": "用时",
                "better": "low",
                "metric": "",
            },
            "teams": [
                {"id": f"t{i}", "name": f"{i} 队", "short": f"{i}队", "playerIds": [f"m{i}"], "group": "A"}
                for i in range(1, 5)
            ],
            "players": [Player(id=f"m{i}", name=f"选手{i}").dump() for i in range(1, 5)],
            "participants": [f"m{i}" for i in range(1, 5)],
            "rounds": [heat.dump()],
        },
        actor="test",
    )
    return heat.code


async def test_multi_side_winner_can_be_c(admin_client):
    """4 队同场、用时最短的是 C：结算与落库都要认 C。"""
    code = await _seed_multi_side_heat()
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "sides": [
                {"key": "A", "score": metrics.parse_time("1:30.000")},
                {"key": "B", "score": metrics.parse_time("1:25.000")},
                {"key": "C", "score": metrics.parse_time("1:20.000")},
                {"key": "D", "score": metrics.parse_time("1:35.000")},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["winner"] == "C", "用时最短的 C 该是第 1"
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "C", "胜方编号要原样落库（C / D 也是合法值）"
    assert [side.rank for side in settled.sides] == [3, 2, 1, 4]


async def test_multi_side_explicit_winner_accepts_d(admin_client):
    """人工指定第 1 名是 D（几方成绩一样时由管理员定）：同样要存得下去。"""
    code = await _seed_multi_side_heat()
    same = metrics.parse_time("1:30.000")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "winner": "D",
            "sides": [{"key": key, "score": same} for key in ("A", "B", "C", "D")],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["winner"] == "D"
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "D" and settled.sides[3].rank == 1


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


# --------------------------------------------------------------------------- #
# 不写成绩 = 没有成绩（垫底）：三种口径都要允许
# --------------------------------------------------------------------------- #
async def test_omitted_score_means_no_result_not_zero(admin_client):
    """整方不填成绩字段 = 没有成绩（垫底），**不是 0 分**。

    这个默认值很要命：数值低胜下 0 是最好的成绩，漏填的一方会被判成第 1。
    """
    code = await _seed_league_round(value_type="integer", better="low")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        # A 整个省略 score 字段（直接调接口的脚本最容易这么写）
        json={"sets": [], "sides": [{"key": "A"}, {"key": "B", "score": 18}]},
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.sides[0].score == metrics.MISSING, "不写 = 没有成绩的哨兵，不是 0"
    assert settled.winner == "B", "数值低胜下漏填的一方绝不能被当成 0 分判第 1"
    assert [side.rank for side in settled.sides] == [2, 1]


async def test_blank_subscore_is_accepted_as_zero(admin_client):
    """小分 / 细则分留空要照收，按 0 记。

    前端留空发的就是哨兵（数值口径 -1、时间口径 0）：以前数值口径下这一格留空
    会被接口按「负分」拒收，而时间口径反而没事——同一种操作两种结果。
    """
    code = await _seed_league_round(value_type="integer", better="high")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "sides": [{"key": "A", "score": 25, "points": -1}, {"key": "B", "score": 18, "points": -1}],
        },
    )
    assert res.status_code == 200, res.text
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert settled.winner == "A"
    assert [side.points for side in settled.sides] == [0, 0], "留空的小分按 0 记，不是哨兵"


async def test_nobody_entered_anything_says_so(admin_client):
    """谁都没写成绩：结算不了（没有可比的成绩），提示要说清是「谁都没登记」。"""
    code = await _seed_league_round(value_type="integer", better="high")
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={"sets": [], "sides": [{"key": "A", "score": -1}, {"key": "B", "score": -1}]},
    )
    assert res.status_code == 400
    # 结算错误是从 store 的回调里抛出来的，响应体是 error 字段（不是 detail），两种都认
    assert "谁都没登记成绩" in str(res.json())


async def test_blank_side_in_a_multi_team_heat_ranks_last(admin_client):
    """4 队同场里有一方没跑完：留空 → 没有成绩 → 垫底（名次分最低、完成场次 0）。"""
    code = await _seed_multi_side_heat()  # 时间口径、4 队
    res = await admin_client.post(
        f"/api/rounds/{code}/result",
        json={
            "sets": [],
            "sides": [
                {"key": "A", "score": metrics.parse_time("1:30.000")},
                {"key": "B", "score": 0},  # 没跑完（时间型的 0 = 没有成绩）
                {"key": "C", "score": metrics.parse_time("1:20.000")},
                {"key": "D", "score": metrics.parse_time("1:35.000")},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["winner"] == "C", "有成绩里最快的 C 是第 1"
    settled = next(r for r in store.snapshot().rounds if r.code == code)
    assert [side.rank for side in settled.sides] == [2, 4, 1, 3], "没成绩的 B 垫底"

    state = (await admin_client.get("/api/state")).json()
    rows = {
        str(row.get("teamId") or (row.get("team") or {}).get("id")): row
        for group in state["groups"]
        for row in group["rows"]
    }
    assert rows["t2"]["rank"] == 4 and rows["t2"]["placement"] == 1, "没成绩的名次分最低"
    assert rows["t2"]["finished"] == 0, "没成绩不算「完成一场」"
