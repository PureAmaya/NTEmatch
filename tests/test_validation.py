"""配置提示（``logic.validate_config``）与规则文案（``logic.rulebook``）。

校验**不阻断保存**，它的价值在于「把对不上的地方说出来」：录入的局数和赛制
不匹配、系列赛已经分出胜负还接着记、参赛人数组不成队……这些如果不说，
只会在比赛当天变成一场争吵。
"""

from __future__ import annotations

from app import logic
from app.models import Player


def test_series_over_best_of_is_reported(make_config):
    """BO3 却记了 4 局：多出来的局不计入胜负，得提醒核对。"""
    issues = logic.validate_config(make_config(best_of=3, sets=[(25, 20), (20, 25), (25, 18), (25, 10)]))
    assert any("超过 BO3" in line for line in issues)


def test_sets_after_decision_are_reported(make_config):
    """2:0 已经赢了，第三局还记着——顺序有意义，所以能精确指出。"""
    issues = logic.validate_config(make_config(best_of=3, sets=[(25, 20), (25, 18), (10, 25)]))
    assert any("先到 2 局" in line for line in issues)


def test_normal_series_is_quiet(make_config):
    """正常的 2:1 不该有任何系列赛相关提示（否则提示会被无视）。"""
    issues = logic.validate_config(make_config(best_of=3, sets=[(25, 20), (20, 25), (25, 18)]))
    assert not any("超过 BO" in line or "先到 2 局" in line for line in issues)


def test_multi_set_with_bo1_is_reported(make_config):
    """赛制是一局定胜负却记了多局：提醒可能本该设成 BO3。"""
    issues = logic.validate_config(make_config(best_of=1, sets=[(25, 20), (20, 25), (25, 18)]))
    assert any("一局定胜负（BO1）" in line for line in issues)


def test_empty_roster_is_reported(make_config):
    """全新一届一个人都没有时，必须有人明说「先去加选手」。"""
    issues = logic.validate_config(make_config())
    assert any("还没有录入任何" in line for line in issues)


def test_time_metric_flags_blank_result(make_config):
    """用时制下 0 = 未完赛：已分出胜负却有一方没成绩，要提醒确认（多半是漏填）。"""
    cfg = make_config(metric="time")
    rnd = cfg.rounds[0]
    rnd.status = "done"
    rnd.winner = "A"
    rnd.sides[0].score = 83456
    rnd.sides[1].score = 0
    issues = logic.validate_config(cfg)
    assert any("没有用时" in line for line in issues)


def test_time_metric_ignores_forfeit_blank(make_config):
    """弃权方本来就记 0，不该被当成「漏填用时」——否则每次弃权都冒一条废提示。"""
    cfg = make_config(metric="time")
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


def test_rulebook_mentions_series(make_config):
    """规则文案里必须写明几局几胜，否则观众看到的规则和实际判罚对不上。"""
    book = logic.rulebook(make_config(best_of=3))
    items = [item for section in book["sections"] for item in section["items"]]
    assert any("三局两胜" in item for item in items)
    assert book["facts"]["bestOf"] == 3
    assert book["facts"]["series"] == "三局两胜"


def test_rulebook_quiet_for_bo1(make_config):
    """一局定胜负不是「系列赛」，不该多出一段废话。"""
    book = logic.rulebook(make_config(best_of=1))
    items = [item for section in book["sections"] for item in section["items"]]
    assert not any("系列赛" in item for item in items)
    assert book["facts"]["series"] == ""
