"""结算逻辑：胜负判定与系列赛（BO）。

这是全站最不能出错的一块——判错了，赛程、排行、冠军全跟着错，而且是**静默**错。
所以这里的每条断言都直接对应一条对外承诺的规则。
"""

from __future__ import annotations

from app import tournament


def test_best_of_three_by_set_wins(make_config):
    """三局两胜：局分 = 各局胜负计数，总得分 = 各局小分合计。"""
    cfg = make_config(best_of=3, sets=[(25, 20), (20, 25), (25, 18)])
    rnd = cfg.rounds[0]

    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 1)
    assert (rnd.sides[0].points, rnd.sides[1].points) == (70, 63)


def test_best_of_three_sweep(make_config):
    """2:0 提前结束（BO3 后面那局不必打）也算赢。"""
    rnd = make_config(best_of=3, sets=[(25, 20), (25, 18)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (2, 0)


def test_best_of_five(make_config):
    """五局三胜同样只是「赢的局多者胜」，不需要另一套结算。"""
    rnd = make_config(best_of=5, sets=[(25, 20), (20, 25), (25, 22), (25, 19)]).rounds[0]
    assert tournament.judge_round(rnd) == "A"
    assert (rnd.sides[0].score, rnd.sides[1].score) == (3, 1)


def test_big_score_without_sets_still_works(make_config):
    """老习惯：只填大比分而不填各局小分，照旧判定。"""
    cfg = make_config()
    rnd = cfg.rounds[0]
    rnd.sides[0].score = 3
    rnd.sides[1].score = 1
    assert tournament.judge_round(rnd) == "A"


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
