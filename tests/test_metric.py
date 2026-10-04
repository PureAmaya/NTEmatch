"""比法（计分制 / 用时制）：方向只在一处定义，这里把它的两侧都钉住。

两件事最要紧：

1. **计分制是历史行为**——所有断言都必须与改造前一字不差，否则老比赛名次会变；
2. **0 在用时制里是「未完赛」而不是「最快」**——这是整个抽象里唯一一个
   静默且致命的坑：判错了，没跑完的人会拿第一，而且没人看得出来。
"""

from __future__ import annotations

import pytest

from app import league, metrics, tournament
from app.models import Player, Round, Side


# --------------------------------------------------------------------------- #
# 用时文本 ↔ 毫秒
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("1:23.456", 83456),
        ("1:23.45", 83450),
        ("1:23", 83000),
        ("83.456", 83456),
        ("83", 83000),
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


# --------------------------------------------------------------------------- #
# 排序键：计分制必须与历史行为完全等价
# --------------------------------------------------------------------------- #
def test_order_key_matches_legacy_score_ordering():
    """计分制的排序键就是历史上的 ``(-score, -points)``，含并列。"""
    values = [(3, 0), (3, 5), (0, 0), (0, 5), (1, 1), (2, 2), (0, 20), (5, 0)]
    legacy = sorted(values, key=lambda item: (-item[0], -item[1]))
    ours = sorted(values, key=lambda item: metrics.order_key(item[0], item[1], "score"))
    assert ours == legacy


def test_order_key_time_metric_is_reversed():
    """用时制下小的在前——但 0（未完赛）永远垫底，哪怕它最小。"""
    values = [(83456, 0), (82100, 0), (0, 0), (0, 5)]
    order = sorted(values, key=lambda item: metrics.order_key(item[0], item[1], "time"))
    assert order[0] == (82100, 0)
    assert order[-1][0] == 0


def test_round_total_follows_entry_style():
    """一场的「总成绩」：填了各局就是各局合计，否则就是 score（与前端同一约定）。"""
    assert metrics.round_total(score=83456, points=0, has_sets=True) == 0
    assert metrics.round_total(score=83456, points=165000, has_sets=True) == 165000
    assert metrics.round_total(score=83456, points=0, has_sets=False) == 83456


# --------------------------------------------------------------------------- #
# 胜负判定
# --------------------------------------------------------------------------- #
def test_score_metric_still_higher_wins(make_config):
    """计分制：分高者胜（改造前后必须一致）。"""
    rnd = make_config().rounds[0]
    rnd.sides[0].score = 3
    rnd.sides[1].score = 1
    assert tournament.judge_round(rnd, metric="score") == "A"


def test_time_metric_faster_wins(make_config):
    """用时制：用时短者胜——同一个比分，方向要反过来。"""
    rnd = make_config(metric="time").rounds[0]
    rnd.sides[0].score = metrics.parse_time("1:23.456")
    rnd.sides[1].score = metrics.parse_time("1:23.100")
    assert tournament.judge_round(rnd, metric="time") == "B"


def test_time_metric_zero_never_wins(make_config):
    """0 = 未完赛：哪怕它「最小」，也不能判成第一名。"""
    rnd = make_config(metric="time").rounds[0]
    rnd.sides[0].score = 0
    rnd.sides[1].score = metrics.parse_time("9:59.999")
    assert tournament.judge_round(rnd, metric="time") == "B"


def test_time_metric_set_by_set(make_config):
    """多小局：每局用时短者赢一局，局分仍是「赢的局数」，总成绩是各局合计。"""
    cfg = make_config(
        best_of=3,
        metric="time",
        sets=[(83456, 84100), (85200, 84900), (82600, 83000)],
    )
    rnd = cfg.rounds[0]
    assert tournament.judge_round(rnd, metric="time") == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 1)
    assert rnd.sides[0].points == 83456 + 85200 + 82600


def test_time_metric_multi_side_ranks_by_speed(make_config):
    """3 队同场：名次按用时升序，未完赛的队垫底。"""
    cfg = make_config(teams=3, metric="time")
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 90000
    rnd.sides[1].score = 88000
    rnd.sides[2].score = 0
    assert tournament.judge_round(rnd, metric="time") == "B"
    assert [side.rank for side in rnd.sides] == [2, 1, 3]


def test_time_metric_forfeit_still_loses(make_config):
    """弃权在两种比法下都是垫底（弃权方记 0，但不能因此「最快」）。"""
    rnd = make_config(metric="time").rounds[0]
    rnd.sides[0].forfeit = True
    rnd.sides[0].score = 0
    rnd.sides[1].score = 900000
    assert tournament.judge_round(rnd, metric="time") == "B"


# --------------------------------------------------------------------------- #
# 小组赛表
# --------------------------------------------------------------------------- #
def test_table_sort_key_time_metric_prefers_more_finished():
    """用时制排序键：完成场次必须排在总用时前面。

    否则「一场没跑完（0 毫秒）」会以「总用时最短」的身份拿到小组第一。
    """
    dnf = {"placement": 5, "finished": 0, "spent": 0, "name": "甲"}
    slow = {"placement": 5, "finished": 2, "spent": 900000, "name": "乙"}
    assert tournament.table_sort_key(dnf, "time") > tournament.table_sort_key(slow, "time")


def _league_round(code: str, a_ids: list[str], b_ids: list[str], a: int, b: int) -> Round:
    """造一场积分制对局（A 方 / B 方各自的选手与成绩）。"""
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
    rnd.winner = "A" if metrics.value_key(a, "time") < metrics.value_key(b, "time") else "B"
    rnd.sides[0].rank, rnd.sides[1].rank = (1, 2) if rnd.winner == "A" else (2, 1)
    return rnd


def test_group_table_time_metric_prefers_faster_total(make_config):
    """小组赛平手时比总用时：甲跑得快，所以名次靠前（计分制会按净胜分给出另一个答案）。"""
    cfg = make_config(teams=2, metric="time")
    first = _league_round("R1", ["p1"], ["p2"], 100000, 1000)   # 乙快得多
    second = _league_round("R2", ["p2"], ["p1"], 0, 5000)       # 甲未完赛，乙 5 秒
    first.stage = second.stage = "group"
    first.sides[0].team_id, first.sides[1].team_id = "t1", "t2"
    second.sides[0].team_id, second.sides[1].team_id = "t2", "t1"
    cfg.rounds = [first, second]

    rows = {row["teamId"]: row for row in tournament.group_tables(cfg.teams, cfg.rounds, "time")["A"]}
    # 名次分：t1 = 2 + 1，t2 = 1 + 2 —— 完全打平，只剩「完成场次 → 总用时」
    assert rows["t1"]["placement"] == rows["t2"]["placement"] == 3
    assert rows["t1"]["finished"] == 2 and rows["t2"]["finished"] == 1
    assert rows["t1"]["rank"] == 1, "两场都完赛的队伍，名次应在退赛一场的队伍之前"


def test_league_time_metric_breaks_tie_by_finish_and_time():
    """积分制平手时：用时制看「完成场次 → 总用时」，而不是净胜分。

    这是唯一能让「净胜分」与「速度」给出相反答案的场景（两边都各退赛一场），
    也正是必须按比法分支的原因。
    """
    from app.defaults import default_config
    from app.models import Config

    cfg = Config.model_validate(default_config())
    cfg.rules.format = "league"
    cfg.rules.metric = "time"
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
    # 净胜分与总用时指向相反的方向：这正是「必须按比法分支」的证据
    assert rows["p1"]["diff"] > rows["p3"]["diff"]
    assert rows["p3"]["spent"] < rows["p1"]["spent"]
    assert rows["p3"]["rank"] < rows["p1"]["rank"], "用时制应按总用时（含完成场次）排名"
