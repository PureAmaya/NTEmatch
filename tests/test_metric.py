"""计分口径（类型 / 标签 / 判断标准）：全站只有一处定义，这里把它的每一侧都钉住。

四件事最要紧：

1. **老数据必须无损**：``metric`` 只有 ``score`` / ``time`` 两个值时，升级后的类型、
   标签、方向与数值必须与改造前一字不差，否则老比赛的名次会变；
2. **三种类型各存各的整数**：自然数原值、小数千分之一、时间毫秒——都不许用浮点；
3. **0 在数值低胜里是「没有成绩」而不是「最快」**——静默且致命的坑；
4. **填了轮次时比分是计数**（赢的轮数），任何类型下都按整数比。
"""

from __future__ import annotations

import pytest

from app import league, metrics, tournament
from app.defaults import default_config
from app.models import Config, Player, Round, Side


# --------------------------------------------------------------------------- #
# 老数据 → 新口径：无损
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("metric", "value_type", "label", "better"),
    [
        ("score", metrics.INTEGER, "得分", metrics.HIGH),
        ("time", metrics.TIME, "用时", metrics.LOW),
        ("", metrics.INTEGER, "得分", metrics.HIGH),          # 更老的库连 metric 都没有
        ("乱写", metrics.INTEGER, "得分", metrics.HIGH),
    ],
)
def test_legacy_metric_maps_losslessly(metric, value_type, label, better):
    """旧的 ``metric`` 一个字段拆成三件套，方向与默认标签都要与改造前一致。"""
    sc = metrics.Scoring.resolve(metric=metric)
    assert (sc.value_type, sc.label_text, sc.better) == (value_type, label, better)


def test_rules_keeps_the_legacy_metric_in_sync():
    """模型仍回填旧口径名（老版本读得回来），但业务只认 scoring。"""
    rules = Config.model_validate(
        {**default_config(), "rules": {"valueType": "time", "better": "low"}}
    ).rules
    assert rules.scoring.value_type == metrics.TIME
    assert rules.metric == "time"
    assert metrics.legacy_metric(rules.value_type) == "time"


def test_time_type_defaults_to_low_wins():
    """只选了「时间」没选方向时，默认是「数值低胜」（越快越好）。"""
    assert metrics.Scoring.resolve(value_type="time").better == metrics.LOW
    assert metrics.Scoring.resolve(value_type="integer").better == metrics.HIGH


def test_label_falls_back_to_type_default():
    assert metrics.Scoring.resolve(value_type="decimal").label_text == "评分"
    assert metrics.Scoring.resolve(value_type="time").label_text == "用时"
    assert metrics.Scoring.resolve(value_type="integer").label_text == "得分"
    assert metrics.Scoring.resolve(value_type="integer", label="击杀数").label_text == "击杀数"


# --------------------------------------------------------------------------- #
# 三种类型的数值 ↔ 文本
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("1:23.456", 83456),
        ("1:23.45", 83450),
        ("1:23", 83000),
        ("83.456", 83456),
        ("0.001", 1),
        ("1'23.456", 83456),          # 常见的另一种写法
        ("1:23\"456", 83456),         # 秒与毫秒之间用引号
        ("1:02:03.400", 3723400),     # 超过一小时
        ("DNF", 0),                   # 未完赛
        ("退赛", 0),
        ("-", 0),
    ],
)
def test_parse_time_accepts_common_formats(text, expected):
    assert metrics.parse_time(text) == expected


def test_parse_time_refuses_garbage():
    """解析不了就报错——静默当成 0 会让人以为「记上了」，实际记成了未完赛。"""
    with pytest.raises(ValueError):
        metrics.parse_time("abc")


@pytest.mark.parametrize("ms", [1, 830, 83450, 83456, 60000, 3723400])
def test_format_time_roundtrips(ms):
    """显示再解析回来必须是同一个毫秒数（否则改一次录入就掉精度）。"""
    assert metrics.parse_time(metrics.format_time(ms)) == ms


def test_format_time_keeps_decimals_honest():
    """整十毫秒显示两位、否则三位：0.01 秒精度的项目看着干净，0.001 的也不掉精度。"""
    assert metrics.format_time(1250) == "1.25"
    assert metrics.format_time(1256) == "1.256"
    assert metrics.format_time(83450) == "1:23.45"
    assert metrics.format_time(60000) == "1:00.00"
    assert metrics.format_time(0) == "—"


def test_hours_minutes_seconds_roundtrip():
    """「时 / 分 / 秒」三个输入框与毫秒的互转（时间型的录入控件靠它）。"""
    assert metrics.hours_minutes_seconds(3_723_400) == (1, 2, 3.4)
    assert metrics.parse_hms(1, 2, 3.4) == 3_723_400
    assert metrics.parse_hms("", "", "") == 0
    assert metrics.parse_hms(0, 0, 83.456) == 83456
    with pytest.raises(ValueError):
        metrics.parse_hms("abc", 0, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8.75", 8750),
        ("8", 8000),
        ("8.7555", 8756),
        ("0.001", 1),
        # 0 是合法读数（「没填」走 MISSING），退赛与空一律是「没有成绩」
        ("0", 0),
        ("0.000", 0),
        ("DNF", metrics.MISSING),
        ("", metrics.MISSING),
    ],
)
def test_parse_decimal_uses_thousandths(text, expected):
    """小数一律放大成**千分之一**存整数——浮点的相等判断撑不住「并列」。"""
    assert metrics.parse_decimal(text) == expected


def test_format_decimal_trims_trailing_zeros():
    assert metrics.format_decimal(8750) == "8.75"
    assert metrics.format_decimal(8000) == "8"
    assert metrics.format_decimal(8756) == "8.756"
    # 数值型里 0 是合法读数（只有时间型的 0 才是「没有成绩」）
    assert metrics.format_decimal(0) == "0"
    assert metrics.format_time(0) == "—"


def test_integer_parse_truncates_like_before():
    """自然数的解析与历史上的 ``int(float(raw))`` 一致（小数输入截断）。"""
    assert metrics.parse_integer("12.9") == 12
    assert metrics.parse_integer("0") == 0
    # 空不再是 0：0 是合法读数，把「没填」记成 0 分就是凭空造了一个成绩
    assert metrics.parse_integer("") == metrics.MISSING
    with pytest.raises(ValueError):
        metrics.parse_integer("abc")


# --------------------------------------------------------------------------- #
# 排序键：数值高胜必须与历史行为完全等价
# --------------------------------------------------------------------------- #
def test_high_wins_order_key_matches_legacy_score_ordering():
    """数值高胜的排序键就是历史上的 ``(-score, -points)``，含并列。"""
    sc = metrics.Scoring(value_type=metrics.INTEGER, better=metrics.HIGH)
    values = [(3, 0), (3, 5), (0, 0), (0, 5), (1, 1), (2, 2), (0, 20), (5, 0)]
    legacy = sorted(values, key=lambda item: (-item[0], -item[1]))
    ours = sorted(values, key=lambda item: sc.order_key(item[0], item[1]))
    assert ours == legacy


def test_low_wins_order_key_is_reversed():
    """数值低胜：小的在前——但 0（没有成绩）永远垫底，哪怕它最小。"""
    sc = metrics.Scoring(value_type=metrics.TIME, better=metrics.LOW)
    values = [(83456, 0), (82100, 0), (0, 0), (0, 5)]
    order = sorted(values, key=lambda item: sc.order_key(item[0], item[1]))
    assert order[0] == (82100, 0)
    assert order[-1][0] == 0


def test_round_total_follows_entry_style():
    """一场的「总成绩」：填了轮次就是各轮合计，否则就是 score（与前端同一约定）。"""
    sc = metrics.Scoring(value_type=metrics.TIME, better=metrics.LOW)
    assert sc.round_total(83456, 0, True) == 0
    assert sc.round_total(83456, 165000, True) == 165000
    assert sc.round_total(83456, 0, False) == 83456


# --------------------------------------------------------------------------- #
# 0 是合法读数（数值型）；只有时间型的 0 才是「没有成绩」
# --------------------------------------------------------------------------- #
def test_zero_is_a_result_for_numbers_but_not_for_time():
    """评委真的会打 0 分，速通不可能 0 毫秒跑完——「没有成绩」不能只有 0 一种写法。"""
    number = metrics.Scoring(value_type=metrics.INTEGER)
    decimal = metrics.Scoring(value_type=metrics.DECIMAL)
    clock = metrics.Scoring(value_type=metrics.TIME)

    assert number.has_result(0) is True
    assert decimal.has_result(0) is True
    assert clock.has_result(0) is False

    # MISSING / 空在三种类型下都是「没有成绩」
    for sc in (number, decimal, clock):
        assert sc.has_result(metrics.MISSING) is False
        assert sc.has_result(None) is False

    assert number.missing_value() == metrics.MISSING
    assert decimal.missing_value() == metrics.MISSING
    assert clock.missing_value() == 0


def test_zero_displays_as_zero_but_missing_as_dash():
    number = metrics.Scoring(value_type=metrics.INTEGER)
    decimal = metrics.Scoring(value_type=metrics.DECIMAL)
    clock = metrics.Scoring(value_type=metrics.TIME)

    assert number.format(0) == "0"
    assert decimal.format(0) == "0"
    assert number.format(metrics.MISSING) == "—"
    assert decimal.format(metrics.MISSING) == "—"
    assert clock.format(0) == "—"
    # 计数（赢的轮数）里的 -1 是异常值，显示成「—」而不是「-1」
    assert clock.format_score(metrics.MISSING, counted=True) == "—"


def test_zero_honours_the_direction_but_missing_never_does():
    """数值低胜里 0 是最好的成绩（罚时 / 杆数）；「没有成绩」永远垫底。"""
    low = metrics.Scoring(value_type=metrics.INTEGER, better=metrics.LOW)
    high = metrics.Scoring(value_type=metrics.INTEGER, better=metrics.HIGH)

    assert low.order_key(0, 0) < low.order_key(5, 0)
    assert high.order_key(0, 0) > high.order_key(5, 0)
    assert low.order_key(metrics.MISSING, 0) > low.order_key(5, 0)
    assert high.order_key(metrics.MISSING, 0) > high.order_key(0, 0)


def test_zero_is_a_result_but_not_an_entry_trace():
    """``has_entered`` 问的是「动过没有」：0 分既可能是真打出来的，也可能是从没打过。"""
    number = metrics.Scoring(value_type=metrics.INTEGER)
    clock = metrics.Scoring(value_type=metrics.TIME)

    assert number.has_result(0) is True
    assert number.has_entered(0) is False
    assert number.has_entered(7) is True
    assert clock.has_entered(0) is False
    assert clock.has_entered(83456) is True


def test_has_total_follows_entry_style():
    """本场有没有成绩：填了轮次看各轮合计，没填轮次看本场成绩（0 分也算）。"""
    number = metrics.Scoring(value_type=metrics.INTEGER)
    clock = metrics.Scoring(value_type=metrics.TIME)

    assert number.has_total(0, 0) is True
    assert clock.has_total(0, 0) is False
    assert number.has_total(0, 30, has_rounds=True) is True
    assert number.has_total(0, 0, has_rounds=True) is False


def test_format_score_keeps_counts_as_integers():
    """填了轮次时 ``score`` 是「赢的轮数」，时间型也不能把它格式化成时间。"""
    sc = metrics.Scoring(value_type=metrics.TIME, better=metrics.LOW)
    assert sc.format_score(2, counted=True) == "2"
    assert sc.format_score(83456, counted=False) == "1:23.456"


# --------------------------------------------------------------------------- #
# 胜负判定
# --------------------------------------------------------------------------- #
def test_high_wins_still_higher_wins(make_config):
    """数值高胜：分高者胜（改造前后必须一致）。"""
    rnd = make_config().rounds[0]
    rnd.sides[0].score = 3
    rnd.sides[1].score = 1
    assert tournament.judge_round(rnd, scoring="integer") == "A"


def test_low_wins_faster_wins(make_config):
    """数值低胜：成绩小者胜——同一个分数，方向要反过来。"""
    rnd = make_config(value_type="time", better="low").rounds[0]
    rnd.sides[0].score = metrics.parse_time("1:23.456")
    rnd.sides[1].score = metrics.parse_time("1:23.100")
    assert tournament.judge_round(rnd, scoring=metrics.Scoring(value_type="time", better="low")) == "B"


def test_low_wins_zero_never_wins(make_config):
    """0 = 没有成绩：哪怕它「最小」，也不能判成第一名。"""
    rnd = make_config(value_type="time", better="low").rounds[0]
    rnd.sides[0].score = 0
    rnd.sides[1].score = metrics.parse_time("9:59.999")
    assert tournament.judge_round(rnd, scoring="time") == "B"


def test_zero_can_win_when_low_wins_for_numbers(make_config):
    """数值低胜 + 数值型：0 是合法成绩，就该让它赢（罚时 0 秒 / 杆数 0）。"""
    cfg = make_config(value_type="integer", better="low")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 0
    rnd.sides[1].score = 3
    assert tournament.judge_round(rnd, scoring=cfg.rules.scoring) == "A"


def test_missing_never_wins(make_config):
    """没有成绩（MISSING）在任何方向下都垫底——这是 0 与「没填」分开的目的。"""
    cfg = make_config(value_type="integer", better="low")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = metrics.MISSING
    rnd.sides[1].score = 3
    assert tournament.judge_round(rnd, scoring=cfg.rules.scoring) == "B"


def test_zero_zero_numbers_is_a_real_draw(make_config):
    """数值型的 0:0 是一场真正的平局（允许平局时必须判成 DRAW）。"""
    cfg = make_config(value_type="integer")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 0
    rnd.sides[1].score = 0
    assert (
        tournament.judge_round(rnd, allow_draw=True, scoring=cfg.rules.scoring) == "DRAW"
    )


def test_nothing_entered_is_not_a_draw(make_config):
    """两方都没填时不能判平局：那只是「还没录」，位置与 0:0 完全不同。"""
    cfg = make_config(value_type="integer")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = metrics.MISSING
    rnd.sides[1].score = metrics.MISSING
    assert tournament.judge_round(rnd, allow_draw=True, scoring=cfg.rules.scoring) == ""


def test_direction_is_independent_from_type(make_config):
    """时间 + 数值高胜是合法组合：类型只管解析显示，方向只管谁赢。"""
    rnd = make_config(value_type="time", better="high").rounds[0]
    rnd.sides[0].score = 83000
    rnd.sides[1].score = 82000
    assert tournament.judge_round(rnd, scoring=metrics.Scoring(value_type="time", better="high")) == "A"


def test_rounds_by_round_for_low_wins(make_config):
    """多轮：每轮成绩小者赢一轮，大比分仍是「赢的轮数」，总成绩是各轮合计。"""
    cfg = make_config(
        value_type="time",
        better="low",
        sets=[(83456, 84100), (85200, 84900), (82600, 83000)],
    )
    rnd = cfg.rounds[0]
    assert tournament.judge_round(rnd, scoring=cfg.rules.scoring) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 1)
    assert rnd.sides[0].points == 83456 + 85200 + 82600


def test_decimal_rounds_compare_by_value(make_config):
    """小数型：8.75 > 8.5，按千分之一的整数比。"""
    cfg = make_config(
        value_type="decimal",
        value_label="评分",
        sets=[(metrics.parse_decimal("8.75"), metrics.parse_decimal("8.5"))],
    )
    rnd = cfg.rounds[0]
    assert tournament.judge_round(rnd, scoring=cfg.rules.scoring) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (1, 0)


def test_multi_side_ranks_by_speed(make_config):
    """3 队同场：名次按成绩升序，没有成绩的队垫底。"""
    cfg = make_config(teams=3, value_type="time", better="low")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 90000
    rnd.sides[1].score = 88000
    rnd.sides[2].score = 0
    assert tournament.judge_round(rnd, scoring=cfg.rules.scoring) == "B"
    assert [side.rank for side in rnd.sides] == [2, 1, 3]


def test_forfeit_still_loses(make_config):
    """弃权在任何口径下都是垫底（弃权方记 0，但不能因此「最快」）。"""
    rnd = make_config(value_type="time", better="low").rounds[0]
    rnd.sides[0].forfeit = True
    rnd.sides[0].score = 0
    rnd.sides[1].score = 900000
    assert tournament.judge_round(rnd, scoring="time") == "B"


# --------------------------------------------------------------------------- #
# 小组赛表
# --------------------------------------------------------------------------- #
def test_table_sort_key_low_wins_prefers_more_finished():
    """数值低胜的排序键：完成场次必须排在总成绩前面。

    否则「一场没跑完（0 毫秒）」会以「总成绩最小」的身份拿到小组第一。
    """
    dnf = {"placement": 5, "finished": 0, "spent": 0, "name": "甲"}
    slow = {"placement": 5, "finished": 2, "spent": 900000, "name": "乙"}
    assert tournament.table_sort_key(dnf, "time") > tournament.table_sort_key(slow, "time")


def _league_round(code: str, a_ids: list[str], b_ids: list[str], a: int, b: int) -> Round:
    """造一场积分制对局（A 方 / B 方各自的选手与成绩）。"""
    sc = metrics.Scoring(value_type="time", better="low")
    rnd = Round(
        code=code,
        label=code,
        stage="league",
        sides=[Side(key="A", player_ids=a_ids), Side(key="B", player_ids=b_ids)],
        sets=[],
    )
    rnd.sides[0].score = a
    rnd.sides[1].score = b
    rnd.status = "done"
    rnd.winner = "A" if sc.sort_key(a) < sc.sort_key(b) else "B"
    rnd.sides[0].rank, rnd.sides[1].rank = (1, 2) if rnd.winner == "A" else (2, 1)
    return rnd


def test_group_table_low_wins_prefers_faster_total(make_config):
    """小组赛平手时比总成绩：甲跑得快，所以名次靠前（数值高胜会给出另一个答案）。"""
    cfg = make_config(teams=2, value_type="time", better="low")
    first = _league_round("R1", ["p1"], ["p2"], 100000, 1000)   # 乙快得多
    second = _league_round("R2", ["p2"], ["p1"], 0, 5000)       # 甲未完赛，乙 5 秒
    first.stage = second.stage = "group"
    first.sides[0].team_id, first.sides[1].team_id = "t1", "t2"
    second.sides[0].team_id, second.sides[1].team_id = "t2", "t1"
    cfg.rounds = [first, second]

    rows = {
        row["teamId"]: row
        for row in tournament.group_tables(cfg.teams, cfg.rounds, cfg.rules.scoring)["A"]
    }
    # 名次分：t1 = 2 + 1，t2 = 1 + 2 —— 完全打平，只剩「完成场次 → 总成绩」
    assert rows["t1"]["placement"] == rows["t2"]["placement"] == 3
    assert rows["t1"]["finished"] == 2 and rows["t2"]["finished"] == 1
    assert rows["t1"]["rank"] == 1, "两场都完赛的队伍，名次应在退赛一场的队伍之前"


def test_league_low_wins_breaks_tie_by_finish_and_total():
    """积分制平手时：数值低胜看「完成场次 → 总成绩」，而不是净胜分。

    这是唯一能让「净胜分」与「速度」给出相反答案的场景（两边都各退赛一场），
    也正是必须按判断标准分支的原因。
    """
    cfg = Config.model_validate(default_config())
    cfg.rules.format = "league"
    cfg.rules.value_type = "time"
    cfg.rules.better = "low"
    cfg.rules.min_rank_played = 1
    cfg.players = [Player(id=f"p{i}", name=f"选手{i}") for i in range(1, 5)]
    cfg.participants = [p.id for p in cfg.players]
    cfg.rounds = [
        # 第一轮：甲乙这一组很慢、丙丁退赛 —— 甲乙赢，净胜分大赚 +100 秒
        _league_round("R1", ["p1", "p2"], ["p3", "p4"], 100000, 0),
        # 第二轮：反过来，丙丁 90 秒、甲乙退赛 —— 丙丁赢，净胜分大亏
        _league_round("R2", ["p3", "p4"], ["p1", "p2"], 90000, 0),
    ]

    rows = {row["playerId"]: row for row in league.compute_standings(cfg)["players"]}

    # 两组各赢一场（各 3 分、平均 1.5）、都完赛一场、都退赛一场
    assert rows["p1"]["points"] == rows["p3"]["points"] == 3
    assert rows["p1"]["played"] == rows["p3"]["played"] == 2
    # 净胜分与总成绩指向相反的方向：这正是「必须按判断标准分支」的证据
    assert rows["p1"]["diff"] > rows["p3"]["diff"]
    assert rows["p3"]["spent"] < rows["p1"]["spent"]
    assert rows["p3"]["rank"] < rows["p1"]["rank"], "数值低胜应按总成绩（含完成场次）排名"
