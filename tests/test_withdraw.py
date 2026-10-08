"""退赛：本届里**剩下还没打的对局一并判负**——真实竞赛规程的做法。

「退赛」与「一场弃权」是两件事，规程里退赛意味着：

* **已打完的比赛结果有效**：不追溯、不清零（与「已结束的比赛只读」是同一条纪律）；
* **未完成的对局全部判负**，对手按胜计（W.O.），退赛者名次垫底；
* **淘汰赛只按签表槽位向前推进**：不重抽、不重排——别人不会因为有人退赛而换对手。

服务端就是 :func:`app.tournament.withdraw_team`（反复「判 → 重新推导」，直到这个队身上
再没有未赛对局）。「不重排」那条性质已经由 ``test_settlement.py`` 那组用例钉住
（``test_forfeit_does_not_reshape_an_already_generated_bracket``：签表一经产生，判罚不许
换人），这里不再重复造一份签表，只钉退赛**自己**那几条：

* 只判「还没打的」，已打完的一个字段都不动；
* 是**批量**：他在本届里所有未赛场次都要判掉，不是只判点到的那一场；
* 「已经打完」按**他自己那一席**算——多队同场里别人先出成绩，他照样得判负；
* 认不出队伍（空 ``team_id`` / 查无此队）时原样返回，不当成错误——接口据此退化成
  「只判这一场」（见 ``main.api_round_walkover``）。
"""

from __future__ import annotations

from app import db, metrics, tournament
from app.models import Round, Side
from app.store import store


def _rnd(code: str, index: int, pairs, *, status: str = "pending", winner: str = "") -> Round:
    """造一场比赛；``pairs`` 是 ``[(席位字母, 队名, 队伍 ID)]``。

    字母只是给人看的：``Side`` 上**没有** ``key`` 字段，A/B/C/D 由**出场顺序**推出来
    （见 :meth:`app.models.Round.key_of`），所以这里顺手核对一下别写反。
    """
    sides = []
    for position, (letter, label, tid) in enumerate(pairs):
        assert letter == chr(ord("A") + position), f"{code}: 字母与位置对不上"
        sides.append(Side(label=label, team_id=tid))
    return Round(
        code=code,
        index=index,
        label=code,
        stage="group",
        status=status,
        winner=winner,
        sides=sides,
    )


def test_withdraw_loses_the_rest_but_keeps_what_is_already_played(make_config):
    """还没打的判负；已经打完的**一个字段都不动**（结果有效，没有追溯这回事）。"""
    cfg = make_config(teams=3)
    played = _rnd("G-1", 1, [("A", "1 队", "t1"), ("B", "2 队", "t2")], status="done", winner="A")
    played.side_by_key("A").score = 3
    played.side_by_key("A").rank = 1
    played.side_by_key("B").score = 1
    played.side_by_key("B").rank = 2
    pending = _rnd("G-2", 2, [("A", "2 队", "t2"), ("B", "3 队", "t3")])
    before = played.model_dump()

    rounds, judged = tournament.withdraw_team(
        cfg.teams, [played, pending], cfg.rules.scoring, "t2", "队伍解散"
    )

    assert judged == ["G-2"], "只判还没打的那些"
    assert rounds[0].model_dump() == before, "已打完的那场不许被改动（比分、名次、留痕都不动）"
    out = rounds[1]
    forfeit, win = out.side_by_key("A"), out.side_by_key("B")
    assert forfeit.forfeit is True, "退赛方要留下弃权痕迹"
    assert forfeit.score == metrics.MISSING, "「没有成绩」而不是 0 分（数值型的 0 是合法读数）"
    assert (forfeit.rank, win.rank) == (2, 1), "退赛方垫底、对手第一"
    assert (out.status, out.winner) == ("done", "B"), "对手按胜计，这一场直接结算"
    assert "弃权" in out.note and "队伍解散" in out.note and "晋级" in out.note, out.note


def test_withdraw_judges_every_remaining_match_of_that_team(make_config):
    """退赛是**批量**：他在本届里每一场还没打的都要判掉，不是只判点到的那一场。"""
    cfg = make_config(teams=3)
    done = _rnd("G-1", 1, [("A", "1 队", "t1"), ("B", "3 队", "t3")], status="done", winner="A")
    done.side_by_key("A").score = 2
    done.side_by_key("A").rank = 1
    done.side_by_key("B").score = 0
    done.side_by_key("B").rank = 2
    round_a = _rnd("G-2", 2, [("A", "2 队", "t2"), ("B", "3 队", "t3")])
    round_b = _rnd("G-3", 3, [("A", "1 队", "t1"), ("B", "2 队", "t2")])
    round_c = _rnd("G-4", 4, [("A", "1 队", "t1"), ("B", "3 队", "t3")])
    before = done.model_dump()

    rounds, judged = tournament.withdraw_team(
        cfg.teams, [done, round_a, round_b, round_c], cfg.rules.scoring, "t2", "弃赛"
    )

    assert sorted(judged) == ["G-2", "G-3"], judged
    assert rounds[0].model_dump() == before, "没碰到他的那一场不许被动到"
    assert rounds[3].status == "pending" and not rounds[3].side_by_key("B").forfeit, (
        "他没参加的那一场照旧"
    )
    for rnd in (rounds[1], rounds[2]):
        # 退赛方不一定都在 A 位，胜者取「对手那一席」的字母
        out = next(side for side in rnd.sides if side.team_id == "t2")
        win = next(side for side in rnd.sides if side.team_id != "t2")
        assert out.forfeit is True, f"{rnd.code} 缺弃权留痕"
        assert (rnd.status, rnd.winner) == ("done", rnd.key_of(win)), (
            f"{rnd.code} 该判给他的对手"
        )


def test_withdraw_from_a_multi_team_match_only_takes_him_out(make_config):
    """3~4 队同场：退赛方移出名次竞争（垫底），**其余队伍继续把这场打完**。"""
    cfg = make_config(teams=3)
    rnd = _rnd("G-1", 1, [("A", "1 队", "t1"), ("B", "2 队", "t2"), ("C", "3 队", "t3")])

    rounds, judged = tournament.withdraw_team(cfg.teams, [rnd], cfg.rules.scoring, "t2", "弃赛")

    assert judged == ["G-1"]
    out = rounds[0]
    assert out.status == "pending" and out.winner == "", "这一场还没打完：不能被结算掉"
    assert out.side_by_key("B").forfeit is True
    assert out.side_by_key("B").rank == 3, "退赛方垫底"
    assert "继续" in out.note, out.note


def test_a_team_that_already_played_that_match_is_not_judged_twice(make_config):
    """「已经打完」按**他自己那一席**算：多队同场里别人先出成绩，退赛的人照样得判负。"""
    cfg = make_config(teams=3)
    rnd = _rnd("G-1", 1, [("A", "1 队", "t1"), ("B", "2 队", "t2"), ("C", "3 队", "t3")])
    rnd.side_by_key("A").score = 25          # 别人先交了成绩
    rnd.side_by_key("A").rank = 1

    rounds, judged = tournament.withdraw_team(cfg.teams, [rnd], cfg.rules.scoring, "t2", "弃赛")

    assert judged == ["G-1"], "别人打完不算他打完"
    assert rounds[0].side_by_key("B").forfeit is True
    assert rounds[0].side_by_key("A").score == 25, "别人已交的成绩不许被动"


def test_withdraw_without_a_team_is_a_no_op(make_config):
    """认不出队伍时**原样返回**：接口据此退化成「只判这一场」，不当成错误。"""
    cfg = make_config(teams=2)
    rnd = _rnd("G-1", 1, [("A", "1 队", "t1"), ("B", "2 队", "t2")])
    before = rnd.model_dump()

    for team_id in ("", "t9"):
        rounds, judged = tournament.withdraw_team(cfg.teams, [rnd], cfg.rules.scoring, team_id)
        assert judged == [], f"{team_id!r} 不该判任何一场"
        assert rounds[0].model_dump() == before, f"{team_id!r} 不该改动任何字段"


async def test_api_withdraw_end_to_end_keeps_finished_matches_untouched(admin_client):
    """端到端：`scope="withdraw"` 从接口一路判进库里，**已打完的那场一个字段都不动**。

    后半句是这条规则的底线：判一批新场次时，绝不能顺手碰已经结束的比赛——那边另有一道
    只读闸门守着（见 ``tests/test_frozen_rounds.py``），这里从接口这一侧再确认一次。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("退赛端到端用例届")
    mine = store.current_id
    def same_pair():
        """三场都是同一对队伍：A = 2 队（要退赛的那支），B = 1 队。"""
        return [
            {"key": "A", "label": "2 队", "teamId": "t2"},
            {"key": "B", "label": "1 队", "teamId": "t1"},
        ]
    try:
        await store.update(
            {
                "rules": {"format": "tournament"},
                "players": [
                    {"id": "p1", "name": "甲"},
                    {"id": "p2", "name": "乙"},
                    {"id": "p3", "name": "丙"},
                    {"id": "p4", "name": "丁"},
                ],
                "participants": ["p1", "p2", "p3", "p4"],
                "teams": [
                    {"id": "t1", "label": "1 队", "playerIds": ["p1", "p2"]},
                    {"id": "t2", "label": "2 队", "playerIds": ["p3", "p4"]},
                ],
                "rounds": [
                    {
                        "index": 1,
                        "code": "G-1",
                        "stage": "group",
                        "label": "已打完的那场",
                        "status": "done",
                        "winner": "B",
                        "sides": [
                            {"key": "A", "label": "2 队", "teamId": "t2", "score": 1, "rank": 2},
                            {"key": "B", "label": "1 队", "teamId": "t1", "score": 2, "rank": 1},
                        ],
                    },
                    {"index": 2, "code": "G-2", "stage": "group", "label": "待打 1", "sides": same_pair()},
                    {"index": 3, "code": "G-3", "stage": "group", "label": "待打 2", "sides": same_pair()},
                ],
            },
            actor="test",
        )
        finished_before = next(r for r in store.snapshot().rounds if r.code == "G-1").model_dump()

        res = await admin_client.post(
            "/api/rounds/G-2/walkover",
            json={"side": "A", "reason": "队伍解散", "scope": "withdraw"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["scope"] == "withdraw", "接口要如实回报走的是退赛"
        assert sorted(body["judged"]) == ["G-2", "G-3"], body["judged"]

        rounds = store.snapshot().rounds
        finished = next(r for r in rounds if r.code == "G-1")
        assert finished.model_dump() == finished_before, "退赛把已打完的那场动到了"
        for code in ("G-2", "G-3"):
            rnd = next(r for r in rounds if r.code == code)
            assert (rnd.status, rnd.winner) == ("done", "B"), f"{code} 该判给他的对手"
            assert rnd.side_by_key("A").forfeit is True, f"{code} 缺弃权留痕"
            assert "队伍解散" in rnd.note
    finally:
        if previous and previous != mine:
            await store.switch_event(previous)
        await store.delete_event(mine)
