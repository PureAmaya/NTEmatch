"""对局级替补与「显式空名单」。

这两件事都关乎**页面显示的人**与**积分榜统计的人**是否一致：替补直接改写对局阵容，
所以统计天然跟着走；而名单为空必须真的为空，不能被当成「未指定」而变回全员。

最容易在后续改动里被破坏的四条（本文件逐条钉住）：

* 范围判定（仅当前比赛 / 本场及之后 / 全场）；
* 已结算的对局**永不改写**；
* 替补已在该场阵容里时跳过（不能出现「同一个人两边都是他」）；
* 同一位选手在同一范围只有一处替补（改人即覆盖，最终只留最后一次）。
"""

from __future__ import annotations

import pytest

from app import db, logic, subs
from app.models import Config, Player, Substitution
from app.store import store


def _round(index: int, code: str, status: str, a: list[str], b: list[str]) -> dict:
    return {
        "index": index,
        "code": code,
        "status": status,
        "stage": "league",
        "label": f"第 {index} 局",
        "sides": [{"playerIds": list(a)}, {"playerIds": list(b)}],
    }


def _data() -> dict:
    """三局积分制：L-1 未开赛、L-2 已结算、L-3 未开赛；p6 是场外替补。"""
    return {
        "rules": {"format": "league", "teamSize": 2},
        "players": [Player(id=f"p{i}", name=f"选手{i}").dump() for i in range(1, 7)],
        "rounds": [
            _round(1, "L-1", "pending", ["p1", "p2"], ["p3", "p4"]),
            _round(2, "L-2", "done", ["p1", "p3"], ["p2", "p4"]),
            _round(3, "L-3", "pending", ["p1", "p4"], ["p2", "p3"]),
        ],
        "substitutions": [],
    }


def _lineup(data: dict, code: str) -> list[list[str]]:
    rnd = next(r for r in data["rounds"] if r["code"] == code)
    return [list(side["playerIds"]) for side in subs.raw_sides(rnd)]


def _sub(**kwargs) -> Substitution:
    params = {"from_id": "p1", "to_id": "p6", "scope": "round", "anchor": "L-1"}
    params.update(kwargs)
    return Substitution(**params)


# --------------------------------------------------------------------------- #
# 范围
# --------------------------------------------------------------------------- #
def test_scope_round_touches_only_that_match():
    data = _data()
    info = subs.apply(data, _sub(scope="round", anchor="L-1"))
    assert info["changed"] == ["L-1"]
    assert _lineup(data, "L-1") == [["p6", "p2"], ["p3", "p4"]]
    assert _lineup(data, "L-3") == [["p1", "p4"], ["p2", "p3"]]


def test_scope_rest_covers_anchor_and_later():
    data = _data()
    info = subs.apply(data, _sub(scope="rest", anchor="L-3"))
    assert info["changed"] == ["L-3"]
    assert _lineup(data, "L-3") == [["p6", "p4"], ["p2", "p3"]]


def test_scope_event_covers_every_match():
    data = _data()
    info = subs.apply(data, _sub(scope="event", anchor=""))
    assert info["changed"] == ["L-1", "L-3"]
    assert info["locked"] == ["L-2"]


def test_rest_from_an_earlier_anchor_reaches_later_matches():
    data = _data()
    info = subs.apply(data, _sub(scope="rest", anchor="L-1"))
    assert info["changed"] == ["L-1", "L-3"]


# --------------------------------------------------------------------------- #
# 已结算 / 撞人 / 没上场
# --------------------------------------------------------------------------- #
def test_settled_matches_are_never_rewritten():
    data = _data()
    before = _lineup(data, "L-2")
    info = subs.apply(data, _sub(scope="event", anchor=""))
    assert "L-2" in info["locked"]
    assert _lineup(data, "L-2") == before


def test_substitute_already_on_court_is_skipped():
    """p2 就在 L-1 的对面：换上去会出现「同一个人两边都是他」，该场必须跳过。"""
    data = _data()
    info = subs.apply(data, _sub(to_id="p2", scope="round", anchor="L-1"))
    assert info["conflict"] == ["L-1"]
    assert info["changed"] == []
    assert _lineup(data, "L-1") == [["p1", "p2"], ["p3", "p4"]]


def test_player_who_never_plays_is_left_alone():
    """轮换制里有人轮空是常态：他不在阵容中就不动那一场（不算改动、也不算异常）。"""
    data = _data()
    info = subs.apply(data, _sub(from_id="p6", to_id="p1", scope="round", anchor="L-1"))
    assert info == {"changed": [], "locked": [], "conflict": []}


def test_unknown_anchor_covers_nothing():
    data = _data()
    assert subs.apply(data, _sub(scope="rest", anchor="L-9"))["changed"] == []


# --------------------------------------------------------------------------- #
# 撤销
# --------------------------------------------------------------------------- #
def test_revert_puts_the_original_back():
    data = _data()
    span = _sub(scope="event", anchor="")
    subs.apply(data, span)
    info = subs.revert(data, span)
    assert info["changed"] == ["L-1", "L-3"]
    assert _lineup(data, "L-1") == [["p1", "p2"], ["p3", "p4"]]
    assert _lineup(data, "L-3") == [["p1", "p4"], ["p2", "p3"]]


def test_revert_leaves_settled_matches_alone():
    data = _data()
    span = _sub(scope="event", anchor="")
    subs.apply(data, span)
    info = subs.revert(data, span)
    assert info["locked"] == ["L-2"]


# --------------------------------------------------------------------------- #
# 覆盖与编号
# --------------------------------------------------------------------------- #
def test_same_slot_matches_the_same_player_and_scope():
    sub = _sub(scope="rest", anchor="L-1")
    assert subs.same_slot(sub, from_id="p1", scope="rest", anchor="L-1")
    assert not subs.same_slot(sub, from_id="p1", scope="event", anchor="")
    assert not subs.same_slot(sub, from_id="p2", scope="rest", anchor="L-1")


def test_new_id_skips_numbers_already_in_use():
    assert subs.new_id([]) == "s001"
    assert subs.new_id([Substitution(id="s001"), Substitution(id="s003")]) == "s004"


def test_broken_records_are_dropped():
    """脏数据（缺半个、自己换自己）直接丢掉，不让它拖垮整份配置。"""
    loaded = subs.load(
        {"substitutions": [{"id": "s001", "fromId": "p1", "toId": ""}, {"fromId": "p2", "toId": "p2"}]}
    )
    assert loaded == []


# --------------------------------------------------------------------------- #
# 对外结构
# --------------------------------------------------------------------------- #
def test_round_view_exposes_the_substitution():
    """赛程里要能同时说出「原来是谁」和「现在是谁」。"""
    data = _data()
    recorded = _sub(id="s001", scope="round", anchor="L-1")
    subs.apply(data, recorded)
    data["substitutions"] = [recorded.dump()]
    cfg = Config.model_validate(data)
    view = logic.round_view(cfg, cfg.rounds[0])
    assert [p["id"] for p in view["sides"][0]["players"]] == ["p6", "p2"]
    item = view["substitutions"][0]
    assert (item["fromName"], item["toName"]) == ("选手1", "选手6")
    assert item["scopeLabel"] == "仅当前比赛"


def test_round_view_hides_substitutions_from_other_matches():
    data = _data()
    recorded = _sub(id="s001", scope="round", anchor="L-1")
    subs.apply(data, recorded)
    data["substitutions"] = [recorded.dump()]
    cfg = Config.model_validate(data)
    assert logic.round_view(cfg, cfg.rounds[2])["substitutions"] == []


def test_original_lineup_reverses_the_records():
    """「原来是谁」靠记录反推：改人与取消都建立在它上面。"""
    data = _data()
    recorded = _sub(id="s001", scope="round", anchor="L-1")
    subs.apply(data, recorded)
    data["substitutions"] = [recorded.dump()]
    rnd = next(r for r in data["rounds"] if r["code"] == "L-1")
    assert subs.original_lineup(data, rnd)[:2] == ["p1", "p2"]


# --------------------------------------------------------------------------- #
# 参与名单：显式空名单 != 未指定
# --------------------------------------------------------------------------- #
def _cfg(participants: list[str], explicit: bool) -> Config:
    return Config.model_validate(
        {
            "players": [Player(id="p1", name="甲").dump(), Player(id="p2", name="乙").dump()],
            "participants": participants,
            "participantsSet": explicit,
        }
    )


def test_unset_roster_means_everyone():
    cfg = _cfg([], explicit=False)
    assert [p.id for p in logic.joined_players(cfg)] == ["p1", "p2"]
    assert logic.has_custom_roster(cfg) is False


def test_empty_but_explicit_roster_means_nobody():
    """全不选后保存必须真的没有参与者——这是「保存后又变回全选」的修复点。"""
    cfg = _cfg([], explicit=True)
    assert logic.joined_players(cfg) == []
    assert logic.has_custom_roster(cfg) is True


def test_legacy_non_empty_roster_counts_as_explicit():
    """老库只有 participants、没有 participantsSet：非空就是显式指定的。"""
    cfg = _cfg(["p1"], explicit=False)
    assert [p.id for p in logic.joined_players(cfg)] == ["p1"]


# --------------------------------------------------------------------------- #
# 接口层：改人只留最后一次、取消还原
# --------------------------------------------------------------------------- #
@pytest.fixture
async def league_rounds():
    """**自己建一届**跑（三局积分制 + 6 位选手），用完整届删掉。

    以前是在共用那一届上「临时改一下、用完还原」——现在**已结束的比赛只读**：还原那一步
    会把用例期间打过的对局删掉 / 改掉，等于让成绩凭空消失，会被闸门当场拦下（业务上
    也该拦）。所以换成自己的一届：清理就是删掉整届，不碰任何已有成绩。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("替补用例届")
    mine = store.current_id
    data = _data()
    await store.update(
        {
            "rules": {"format": "league", "teamSize": 2},
            "players": data["players"],
            "participants": [p["id"] for p in data["players"]],
            "participantsSet": True,
            "rounds": data["rounds"],
            "substitutions": [],
        },
        actor="test",
    )
    try:
        yield
    finally:
        if previous and previous != mine:
            await store.switch_event(previous)
        await store.delete_event(mine)


async def test_api_substitute_changes_the_lineup(admin_client, league_rounds):
    res = await admin_client.post(
        "/api/rounds/L-1/substitute", json={"fromId": "p1", "toId": "p6", "scope": "round"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changedRounds"] == ["L-1"]
    assert body["scopeLabel"] == "仅当前比赛"
    assert "p6" in store.snapshot().rounds[0].sides[0].player_ids


async def test_api_change_the_substitute_keeps_only_the_last(admin_client, league_rounds):
    """同一选手 + 同一范围再指定一次 = 改人：记录不新增，面板上只显示最后一位。"""
    first = await admin_client.post(
        "/api/rounds/L-1/substitute", json={"fromId": "p1", "toId": "p6", "scope": "round"}
    )
    assert first.status_code == 200, first.text
    sub_id = first.json()["id"]

    second = await admin_client.post(
        "/api/rounds/L-1/substitute", json={"fromId": "p1", "toId": "p5", "scope": "round"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == sub_id

    cfg = store.snapshot()
    assert [s.to_id for s in cfg.substitutions] == ["p5"]
    assert cfg.rounds[0].sides[0].player_ids == ["p5", "p2"]


async def test_api_cancel_puts_the_original_back(admin_client, league_rounds):
    created = await admin_client.post(
        "/api/rounds/L-1/substitute", json={"fromId": "p1", "toId": "p6", "scope": "event"}
    )
    assert created.status_code == 200, created.text
    sub_id = created.json()["id"]
    assert "L-2" in created.json()["lockedRounds"]   # 已结算的那场不改写

    res = await admin_client.post(f"/api/substitutions/{sub_id}/cancel")
    assert res.status_code == 200, res.text
    cfg = store.snapshot()
    assert cfg.substitutions == []
    assert cfg.rounds[0].sides[0].player_ids == ["p1", "p2"]
    assert cfg.rounds[2].sides[0].player_ids == ["p1", "p4"]


async def test_api_substitute_rejects_a_player_not_on_court(admin_client, league_rounds):
    res = await admin_client.post(
        "/api/rounds/L-1/substitute", json={"fromId": "p6", "toId": "p5", "scope": "round"}
    )
    assert res.status_code == 400
    assert "阵容" in res.json()["error"]


async def test_api_participants_can_be_emptied(admin_client, league_rounds):
    """全不选保存：接口回 0 人且 explicit=true，不会再被当成「未指定 = 全员参与」。"""
    res = await admin_client.post("/api/participants", json={"playerIds": []})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 0
    assert body["explicit"] is True
    assert body["state"]["participantsSet"] is True
    assert body["state"]["participants"] == []
