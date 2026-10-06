"""结算逻辑：胜负判定与多轮次（大比分）。

这是全站最不能出错的一块——判错了，赛程、排行、冠军全跟着错，而且是**静默**错。
所以这里的每条断言都直接对应一条对外承诺的规则：

* 一场可以记任意多轮（不再有 BO 上限），**赢的轮数就是大比分**；
* 各轮成绩合计就是这场比赛的「总成绩」（积分榜与均分按它算）；
* 不填轮次时，``score`` 就是本场成绩本身，照旧判定。
"""

from __future__ import annotations

from app import tournament
from app.models import SetScore


def test_three_rounds_by_round_wins(make_config):
    """三轮：大比分 = 各轮胜负计数，总成绩 = 各轮成绩合计。"""
    cfg = make_config(sets=[(25, 20), (20, 25), (25, 18)])
    rnd = cfg.rounds[0]

    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 1)
    assert (rnd.sides[0].points, rnd.sides[1].points) == (70, 63)


def test_sweep_is_still_wins(make_config):
    """2:0 提前结束（后面那轮不必打）也算赢。"""
    rnd = make_config(sets=[(25, 20), (25, 18)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 0)


def test_round_count_is_free(make_config):
    """轮数不再受 BO 限制：四轮就按四轮数，谁赢得多谁赢。"""
    rnd = make_config(sets=[(25, 20), (20, 25), (25, 22), (25, 19)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (3, 1)


def test_split_rounds_are_broken_by_total(make_config):
    """大比分打平时比总成绩（各轮合计）——这是「轮数是计数、成绩是成绩」的必然结果。

    数值高胜下就是「总得分高的赢」，与净胜分同一个方向。
    """
    rnd = make_config(sets=[(25, 20), (20, 25), (19, 25), (25, 20)]).rounds[0]
    assert tournament.judge_round(rnd) == "B"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 2)
    assert (rnd.sides[0].points, rnd.sides[1].points) == (89, 90)


def test_everything_tied_needs_a_decider(make_config):
    """轮数与总成绩都一样：判不出来，交回管理员指定。"""
    rnd = make_config(sets=[(25, 20), (20, 25)]).rounds[0]
    assert tournament.judge_round(rnd) == ""
    assert (rnd.sides[0].score, rnd.sides[1].score) == (1, 1)
    assert rnd.sides[0].points == rnd.sides[1].points == 45


def test_five_rounds(make_config):
    rnd = make_config(sets=[(25, 20), (20, 25), (25, 22), (25, 19), (21, 25)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (3, 2)


def test_big_score_without_rounds_still_works(make_config):
    """老习惯：只填本场成绩而不填轮次，照旧判定。"""
    cfg = make_config()
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 3
    rnd.sides[1].score = 1
    assert tournament.judge_round(rnd) == "A"


def test_one_round_is_the_same_as_a_plain_result(make_config):
    """只记一轮时，大比分是 1:0，总成绩就是那一轮的成绩。"""
    rnd = make_config(sets=[(25, 20)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (1, 0)
    assert (rnd.sides[0].points, rnd.sides[1].points) == (25, 20)


def test_round_with_a_blank_side_goes_to_the_other(make_config):
    """某轮只有一方有成绩：0 = 没有成绩，那一轮归有成绩的一方（校验面板会提醒核对）。"""
    rnd = make_config(sets=[(25, 0), (12, 25)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (1, 1), "第二轮 B 赢回来"
    assert rnd.sides[0].points == 37 and rnd.sides[1].points == 25


def test_forfeit_loses(make_config):
    """弃权方垫底，对方不战而胜（人数不齐时唯一的出路）。"""
    rnd = make_config().rounds[0]
    rnd.sides[0].forfeit = True
    assert tournament.judge_round(rnd) == "B"


def test_shared_first_place_needs_decider(make_config):
    """多队同场并列第一时交回管理员指定，而不是随便挑一个。"""
    rnd = make_config(teams=3).rounds[0]
    for side in rnd.sides:
        side.score = 5
    assert tournament.judge_round(rnd) == ""


def test_rank_follows_score_for_multi_side(make_config):
    """3 队同场：名次按得分降序自动推导（名次分是后续排名的依据）。"""
    rnd = make_config(teams=3).rounds[0]
    rnd.sides[0].score = 11
    rnd.sides[1].score = 15
    rnd.sides[2].score = 13
    tournament.judge_round(rnd)
    assert [side.rank for side in rnd.sides] == [3, 1, 2]


def test_rounds_reset_by_new_entry(make_config):
    """重新录入时按新轮次重算，不会把上一次的大比分留在场上。"""
    rnd = make_config(sets=[(25, 20), (25, 20)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert rnd.sides[0].score == 2

    rnd.sets = [SetScore(a=20, b=25), SetScore(a=20, b=25)]
    assert tournament.judge_round(rnd) == "B"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (0, 2)
