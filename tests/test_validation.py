"""配置提示（``logic.validate_config``）与规则文案（``logic.rulebook``）。

校验**不阻断保存**，它的价值在于「把对不上的地方说出来」：某一轮只填了一方的
成绩、已分出胜负却有一方没有成绩、参赛人数组不成队……这些如果不说，
只会在比赛当天变成一场争吵。
"""

from __future__ import annotations

from app import logic, tournament
from app.defaults import default_config
from app.models import Config, Player, Team


def test_round_with_one_side_blank_is_reported(make_config):
    """某一轮只有一方有成绩：那一轮不会有胜负，得提醒核对。"""
    issues = logic.validate_config(make_config(sets=[(25, 20), (25, 0)]))
    assert any("只填了一方" in line for line in issues)


def test_normal_rounds_are_quiet(make_config):
    """正常的 2:1 不该有任何轮次相关提示（否则提示会被无视）。"""
    issues = logic.validate_config(make_config(sets=[(25, 20), (20, 25), (25, 18)]))
    assert not any("只填了一方" in line for line in issues)


def test_round_count_is_never_flagged(make_config):
    """轮数自由：记多少轮都不报警（不再有 BO 上限这回事）。"""
    issues = logic.validate_config(
        make_config(sets=[(25, 20), (20, 25), (25, 18), (25, 10), (21, 25)])
    )
    assert not any("轮" in line and "超过" in line for line in issues)


def test_empty_roster_is_reported(make_config):
    """全新一届一个人都没有时，必须有人明说「先去加选手」。"""
    issues = logic.validate_config(make_config())
    assert any("还没有录入任何" in line for line in issues)


def test_time_type_flags_blank_result(make_config):
    """时间型下 0 = 未完赛：已分出胜负却有一方没成绩，要提醒确认（多半是漏填）。"""
    cfg = make_config(value_type="time", better="low")
    rnd = cfg.rounds[0]
    rnd.status = "done"
    rnd.winner = "A"
    rnd.sides[0].score = 83456
    rnd.sides[1].score = 0
    issues = logic.validate_config(cfg)
    assert any("没有用时" in line for line in issues)


def test_time_type_ignores_forfeit_blank(make_config):
    """弃权方本来就记 0，不该被当成「漏填用时」——否则每次弃权都冒一条废提示。"""
    cfg = make_config(value_type="time", better="low")
    rnd = cfg.rounds[0]
    rnd.status = "done"
    rnd.winner = "B"
    rnd.sides[0].forfeit = True
    rnd.sides[0].score = 0
    rnd.sides[1].score = 900000
    issues = logic.validate_config(cfg)
    assert not any("没有用时" in line for line in issues)


def test_empty_participants_means_everyone(make_config):
    """参与名单留空 = 全员参与——这是既有语义，主页引导与人数校验都建立在它上面。"""
    cfg = make_config()
    cfg.players = [Player(id="p1", name="甲"), Player(id="p2", name="乙")]
    assert [p.id for p in logic.joined_players(cfg)] == ["p1", "p2"]


def test_too_few_players_is_reported(make_config):
    """勾了 2 个人，但 2v2 至少要 4 个——最常见的「跑不起来」。"""
    cfg = make_config()
    cfg.players = [Player(id=f"p{i}", name=f"选手{i}") for i in range(1, 3)]
    cfg.participants = [p.id for p in cfg.players]
    issues = logic.validate_config(cfg)
    assert any("不足 4 人" in line for line in issues)


def test_rulebook_states_the_scoring_terms(make_config):
    """规则文案必须写明「什么数值、谁赢、怎么记轮次」，否则观众看到的规则和判罚对不上。"""
    book = logic.rulebook(make_config(value_type="time", value_label="用时", better="low"))
    items = [item for section in book["sections"] for item in section["items"]]
    text = " ".join(items)
    assert "时间" in text and "用时" in text and "数值低胜" in text
    assert "轮" in text
    facts = book["facts"]
    assert (facts["valueType"], facts["better"]) == ("time", "low")
    assert facts["betterLabel"] == "数值低胜"
    assert facts["valueLabel"] == "用时"


def test_rulebook_uses_the_custom_label(make_config):
    """自定义标签要出现在规则文案里（Admin 选了「击杀数」就得说「击杀数」）。"""
    book = logic.rulebook(make_config(value_label="击杀数"))
    items = [item for section in book["sections"] for item in section["items"]]
    assert any("击杀数" in item for item in items)


def _detail_config(*, per_match: int, players: int, fmt: str = "tournament") -> Config:
    """造一份「有队伍、有分组、有赛程」的配置：规则文案的具体数字全靠它推。"""
    base = default_config()
    cfg = Config.model_validate(
        {
            **base,
            "rules": {
                **base["rules"],
                "format": fmt,
                "teamSize": 2,
                "teamsPerMatch": per_match,
                "groupCount": 2,
            },
        }
    )
    cfg.players = [Player(id=f"p{i}", name=f"选手{i}") for i in range(1, players + 1)]
    cfg.participants = []
    if fmt == "tournament":
        teams, _ = tournament.auto_form_teams(cfg.players, 2, seed=7)
        cfg.teams = tournament.assign_groups(teams, 2)
        cfg.rounds = tournament.build_group_rounds(cfg.teams, per_match)
    return cfg


def test_schedule_keeps_the_groups_set_on_the_team_board():
    """**组队台里手工分的组必须照用**。

    以前 ``build_tournament`` 无条件再按顺序轮转分一遍，于是「在组队台改了分组 → 保存 →
    生成赛程」得到的是旧分组——看起来就是改动没生效。这里用一份**刻意与算法顺序不同**
    的分组（前 3 支排 B 组）钉住它。
    """
    base = default_config()
    cfg = Config.model_validate(
        {**base, "rules": {**base["rules"], "format": "tournament", "groupCount": 2}}
    )
    teams = [Team(id=f"t{i:02d}", label=f"{i} 队") for i in range(1, 7)]
    for index, team in enumerate(teams):
        team.group = "B" if index < 3 else "A"

    rounds, _warnings, _summary = tournament.build_tournament(teams, cfg.rules)
    by_id = {t.id: t for t in teams}
    seen: dict[str, set[str]] = {}
    for rnd in rounds:
        if rnd.stage != "group":
            continue
        key = (rnd.label or "").split(" · ")[0].replace(" 组", "")
        for side in rnd.sides:
            assert by_id[side.team_id].group == key, "分组被重新排过了"
            seen.setdefault(side.team_id, set()).add(key)
    assert seen and all(len(groups) == 1 for groups in seen.values()), "一支队只能在一个组里"


def test_unassigned_teams_still_get_groups():
    """自动组队出来的队伍（一个组都没分）仍然要自动分组——不能因为上面那条就不分了。"""
    base = default_config()
    cfg = Config.model_validate(
        {**base, "rules": {**base["rules"], "format": "tournament", "groupCount": 2}}
    )
    teams = [Team(id=f"t{i:02d}", label=f"{i} 队") for i in range(1, 7)]
    rounds, _warnings, _summary = tournament.build_tournament(teams, cfg.rules)
    assert {t.group for t in teams} == {"A", "B"}
    by_id = {t.id: t for t in teams}
    for rnd in rounds:
        if rnd.stage != "group":
            continue
        key = (rnd.label or "").split(" · ")[0].replace(" 组", "")
        assert all(by_id[side.team_id].group == key for side in rnd.sides)


def test_rulebook_is_specific_about_the_tournament():
    """规则要**具体**：分组、每队场次、名次分表、出线名额、淘汰赛结构都得写出来。"""
    cfg = _detail_config(per_match=2, players=8)  # 4 队 / 2 组 → 组内单循环
    book = logic.rulebook(cfg)
    text = " ".join(item for section in book["sections"] for item in section["items"])
    facts = book["facts"]

    assert "单循环" in text, "2 队对 2 队必须写明是组内单循环"
    assert "每队 1 场" in text, "每队打几场要按实际赛程算出来"
    assert "第 1 名 2 分" in text and "第 2 名 1 分" in text, "2 队同场的名次分要写全"
    assert "出线" in text and "淘汰" in text, "小组赛怎么出线 / 谁被淘汰不能含糊"
    assert facts["groupSizes"] == "A 组 2 队、B 组 2 队"
    assert facts["shape"] == "组 vs 组"
    assert facts["perTeamMatches"] == "每队 1 场"


def test_rulebook_covers_multi_team_heats():
    """3 / 4 队同场时规则要换一套说法（名次分与排法都不同）。"""
    three = logic.rulebook(_detail_config(per_match=3, players=12))
    text = " ".join(item for section in three["sections"] for item in section["items"])
    assert "3 队同场" in text
    assert "第 1 名 3 分、第 2 名 2 分、第 3 名 1 分" in text
    assert three["facts"]["shape"] == "3 队同场"

    four = logic.rulebook(_detail_config(per_match=4, players=16))
    four_text = " ".join(item for section in four["sections"] for item in section["items"])
    assert "4 队同场" in four_text
    assert "第 1 名 4 分" in four_text and "第 4 名 1 分" in four_text


def test_rulebook_follows_the_format_switch():
    """规则随赛制自动变：积分制说「不淘汰」，锦标赛制必须给出晋级与淘汰路径。"""
    league = logic.rulebook(_detail_config(per_match=2, players=8, fmt="league"))
    league_text = " ".join(item for section in league["sections"] for item in section["items"])
    assert "积分制" in league_text and "不淘汰" in league_text
    assert "淘汰赛" not in league_text

    tour = logic.rulebook(_detail_config(per_match=2, players=8))
    tour_text = " ".join(item for section in tour["sections"] for item in section["items"])
    assert "淘汰赛" in tour_text
    assert "双败" in tour_text or "单败" in tour_text


def test_rulebook_says_what_happens_before_grouping():
    """还没组队时也要给话：说清下一步做什么，而不是留一段空白。"""
    cfg = _detail_config(per_match=2, players=8)
    cfg.teams = []
    cfg.rounds = []
    book = logic.rulebook(cfg)
    text = " ".join(item for section in book["sections"] for item in section["items"])
    assert "还没有队伍" in text
    assert "生成赛程" in text
