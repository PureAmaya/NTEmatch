"""结算逻辑：胜负判定与多轮次（大比分）。

这是全站最不能出错的一块——判错了，赛程、排行、冠军全跟着错，而且是**静默**错。
所以这里的每条断言都直接对应一条对外承诺的规则：

* 一场可以记任意多轮（不再有 BO 上限），**赢的轮数就是大比分**；
* 各轮成绩合计就是这场比赛的「总成绩」（积分榜与均分按它算）；
* 不填轮次时，``score`` 就是本场成绩本身，照旧判定。
"""

from __future__ import annotations

from app import logic, metrics, tournament
from app.models import Round, Rules, SetScore, Side, Team


# --------------------------------------------------------------------------- #
# 打完自动结束这一届（两套赛制的口径不一样）
# --------------------------------------------------------------------------- #
def test_tournament_closes_once_the_champion_is_decided(make_config):
    """锦标赛制：总决赛有了胜者 → 本届自动标记「已结束」，并补上结束时间。"""
    cfg = make_config(teams=2)
    cfg.event.status = "active"
    cfg.event.end_time = ""
    cfg.rounds[0].stage = "gf"
    cfg.rounds[0].status = "done"
    cfg.rounds[0].winner = "A"
    out = logic.close_on_champion(cfg.dump())
    assert out["event"]["status"] == "closed"
    assert out["event"]["endTime"], "结束时间要顺手补上（赛后「用时」与「已结束」都靠它）"


def test_league_closes_only_after_every_round_is_played(make_config):
    """积分制：没有「总决赛」这一场，**每场都打完**才算结束。

    这是用户报的那个 bug：以前只认锦标赛制，于是积分制打完了永远停在「进行中」，
    管理员得手动再点一次「标记结束」。
    """
    cfg = make_config(teams=2, fmt="league")
    cfg.event.status = "active"
    cfg.rounds[0].status = "done"
    cfg.rounds[0].winner = "A"
    assert "积分制" in logic.season_finished(cfg), "只有一场时打完就该算结束"

    # 还有没打的对局 → 不算结束，也不许关（否则赛程打到一半就被封存）
    pending = cfg.rounds[0].model_copy(update={"code": "L-2", "index": 2, "status": "pending", "winner": ""})
    cfg.rounds = [cfg.rounds[0], pending]
    assert logic.season_finished(cfg) == ""
    assert logic.close_on_champion(cfg.dump())["event"]["status"] == "active"


async def test_startup_sweep_closes_events_that_were_already_finished():
    """启动时补记：**在自动结束生效之前就打完了**的届，重启一次就该是「已结束」。

    用户报的正是这个：页面上写着「已结束 · 赛程 10/10 场」，而届状态下拉里还是
    「进行中」——自动结束挂在「最后一笔结果落库那一刻」，早打完的老数据永远等不到。
    """
    from app import db
    from app.store import store

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("补记用例届")
    mine = store.current_id
    try:
        await store.update(
            {
                "event": {"status": "active"},
                "rules": {"format": "league", "totalRounds": 1},
                "players": [{"id": "p01", "name": "甲"}],
                "participants": ["p01"],
                "participantsSet": True,
                "rounds": [
                    {
                        "index": 1,
                        "code": "L-1",
                        "stage": "league",
                        "label": "第 1 局",
                        "status": "done",
                        "winner": "A",
                        "sides": [{"playerIds": ["p01"], "score": 1}, {"playerIds": ["p01"], "score": 2}],
                    }
                ],
            }
        )
        assert (await store.read_event(mine)).event.status == "active", "模拟「早打完的老数据」"

        assert mine in await store.close_finished_events()
        after = await store.read_event(mine)
        assert after.event.status == "closed" and after.event.end_time
        assert mine not in await store.close_finished_events(), "已经结束了就不再动它（幂等）"
    finally:
        if previous:
            await store.switch_event(previous)
        await store.delete_event(mine)


def test_closing_is_idempotent_and_the_end_time_is_kept(make_config):
    """已经结束的届原样返回：不重复写结束时间，也不会把管理员填的时间改掉。"""
    cfg = make_config(teams=2)
    cfg.event.status = "closed"
    cfg.event.end_time = "2026-10-06T23:30"
    assert logic.close_on_champion(cfg.dump())["event"]["endTime"] == "2026-10-06T23:30"


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


# --------------------------------------------------------------------------- #
# 小组赛 → 淘汰赛：谁跟谁打
#
# 这一组盯的是「出线之后第一轮碰上谁」：名次算对了，配对配错了照样是错。
# --------------------------------------------------------------------------- #
def _played_tournament(team_count: int, per_match: int = 2, kind: str = "integer"):
    """造一届、把小组赛全部打完（每场按出场顺序给成绩，第一方最优）。"""
    teams = [
        Team(id=f"t{i}", label=f"{i} 队", short=f"{i}", player_ids=[f"p{i}"])
        for i in range(1, team_count + 1)
    ]
    rules = Rules(
        teams_per_match=per_match,
        value_type="time" if kind == "time" else "integer",
        value_label="用时" if kind == "time" else "得分",
        better="low" if kind == "time" else "high",
    )
    rounds, _warnings, summary = tournament.build_tournament(teams, rules)
    sc = rules.scoring
    for rnd in rounds:
        if rnd.stage != "group":
            continue
        for idx, side in enumerate(rnd.sides):
            side.score = (600_000 + idx * 1000) if kind == "time" else (10 - idx)
        rnd.winner = tournament.judge_round(rnd, allow_draw=False, scoring=sc)
        rnd.status = "done"
    return teams, rounds, sc, summary


def _legacy_pairing(teams, rounds, scoring, size):
    """按**老算法**（总排名直接当种子）排一遍对阵。

    用来造「升级之前库里就是这些对阵」的样子：新版改成交叉配对之后，
    已经打下来的届必须**照旧**，不能回溯改写。
    """
    tables = tournament.group_tables(teams, rounds, scoring)
    seeds = tournament.overall_ranking(tables, scoring)[:size]
    return tournament.resolve_rounds(rounds, seeds, teams)


def test_knockout_first_round_does_not_park_one_group_against_itself():
    """出线后首轮**不能让同组两队相遇**（能避开就必须避开）。

    「总排名」是「先列各组第 1 名、再列第 2 名」，而首轮配对是「1 对 8、3 对 6」——
    直接拿总排名填对阵表，3 个小组时 3 号与 6 号同为 C 组，8 强首轮就自己人打自己人；
    两个小组各出线 4 队时，A 组第 1 甚至会碰上本组第 3。
    """
    for count, per_match in ((6, 2), (8, 2), (9, 2), (10, 4), (12, 4)):
        for kind in ("integer", "time"):
            teams, rounds, sc, _summary = _played_tournament(count, per_match, kind)
            group_of = {t.id: t.group for t in teams}
            resolved = tournament.resolve_tournament(teams, rounds, sc)
            first = [r for r in resolved if r.stage == "wb" and r.bracket_round == 1]
            assert first, f"{count} 队应当有淘汰赛首轮"
            for rnd in first:
                left, right = (side.team_id for side in rnd.sides)
                assert left and right
                assert group_of[left] != group_of[right], (
                    f"{count} 队 / 同场 {per_match} / {kind}：{rnd.code} 让 "
                    f"{group_of[left]} 组自己人打起来了"
                )


def test_top_two_seeds_can_only_meet_in_the_final():
    """1 号与 2 号种子分处上下半区：只可能在决赛相遇。"""
    teams, rounds, sc, summary = _played_tournament(9, 2)
    seeds = tournament.advance_seeds(teams, rounds, sc)
    assert len(seeds) == summary["size"] and len(set(seeds)) == len(seeds)
    resolved = tournament.resolve_tournament(teams, rounds, sc)
    slots = {
        side.team_id: rnd.slot
        for rnd in resolved
        if rnd.stage == "wb" and rnd.bracket_round == 1
        for side in rnd.sides
    }
    boundary = len(seeds) // 4  # 首轮场次的前一半 = 上半区
    assert (slots[seeds[0]] <= boundary) != (slots[seeds[1]] <= boundary)


def test_seed_slots_say_where_the_team_came_from():
    """席位文案写**真正的出处**（「A 组第 2」），不能写「小组赛第 3 名」。

    交叉配对之后种子号 ≠ 小组赛总排名，按种子号写名次就是骗人
    （「第 3 名」点进去却是 A 组第 2）。
    """
    teams, rounds, sc, _summary = _played_tournament(9, 2)
    tables = tournament.group_tables(teams, rounds, sc)
    where = {r["teamId"]: (r["group"], r["rank"]) for rows in tables.values() for r in rows}
    resolved = tournament.resolve_tournament(teams, rounds, sc)
    for rnd in resolved:
        if rnd.stage != "wb" or rnd.bracket_round != 1:
            continue
        for side in rnd.sides:
            group, rank = where[side.team_id]
            assert side.source == f"{group} 组第 {rank}"
    # 没有小组赛（队伍太少）时退回「N 号种子」——那时种子号就等于队伍顺序
    assert tournament.source_text("seed:3") == "3 号种子"


def test_knockout_that_already_started_keeps_its_pairing():
    """**算法升级不许回溯改写已经打下来的比赛**。

    这一版把种子算法从「总排名直接当种子」改成了交叉配对。已经开打的届必须保持原样：
    否则一次普通写入（每次写入都会重算淘汰赛阵容）就会把已录的淘汰赛成绩作废
    ——对阵变了按规矩要重打，那可是已经打过的比赛。
    """
    teams, rounds, sc, summary = _played_tournament(9, 2)
    stored = _legacy_pairing(teams, rounds, sc, summary["size"])
    played = next(r for r in stored if r.code == "WB-1-2")
    played.status, played.winner = "done", "A"
    played.sides[0].score, played.sides[1].score = 3, 2
    played.sides[0].rank, played.sides[1].rank = 1, 2

    assert tournament.knockout_started(stored), "已经打完一场 → 判定为「开打」"
    legacy_seeds = tournament.overall_ranking(
        tournament.group_tables(teams, stored, sc), sc
    )[: summary["size"]]
    assert tournament.advance_seeds(teams, stored, sc) == legacy_seeds, "冻结要沿用老的名次顺序"

    after = tournament.resolve_tournament(teams, stored, sc)
    same = next(r for r in after if r.code == "WB-1-2")
    assert [side.team_id for side in same.sides] == [side.team_id for side in played.sides]
    assert (same.status, same.winner) == ("done", "A"), "已录的成绩不能被作废"
    assert (same.sides[0].score, same.sides[1].score) == (3, 2)


def test_knockout_not_started_gets_the_fixed_pairing():
    """还没开打：交叉配对照旧生效（这正是这次要修的那件事），一场都不该丢。"""
    teams, rounds, sc, summary = _played_tournament(9, 2)
    stored = _legacy_pairing(teams, rounds, sc, summary["size"])
    assert not tournament.knockout_started(stored)

    after = tournament.resolve_tournament(teams, stored, sc)
    group_of = {t.id: t.group for t in teams}
    first = [r for r in after if r.stage == "wb" and r.bracket_round == 1]
    assert first
    for rnd in first:
        left, right = (side.team_id for side in rnd.sides)
        assert group_of[left] != group_of[right], f"{rnd.code} 又让同组两队碰上了"


def test_resetting_the_knockout_unfreezes_the_pairing():
    """把淘汰赛退回未开始：冻结解除，重新按新算法排——想换算法不必重建整届。"""
    teams, rounds, sc, summary = _played_tournament(9, 2)
    stored = _legacy_pairing(teams, rounds, sc, summary["size"])
    played = next(r for r in stored if r.code == "WB-1-2")
    played.status, played.winner = "done", "A"

    assert tournament.knockout_started(stored)
    tournament.reset_round_result(played)
    assert not tournament.knockout_started(stored), "重置之后这一届不再算「开打」"
    assert tournament.advance_seeds(teams, stored, sc) == tournament.bracket_seeds(
        tournament.group_tables(teams, stored, sc), summary["size"], sc
    ), "冻结解除后按新算法（交叉配对）排"


# --------------------------------------------------------------------------- #
# 不写成绩 = 没有成绩 = 垫底
# --------------------------------------------------------------------------- #
def test_blank_sides_share_the_last_place_not_a_middle_tie():
    """多队同场里几队都没成绩：他们并列**最后一名**，不是「并列第 2」。

    名次分按并列的那一位算（4 队 = 4/3/2/1）：3 队没跑完却按「并列第 2」计，
    等于和跑完拿了第 2 的队伍拿一样多的分——漏填反倒占了便宜。
    """
    sc = metrics.Scoring.resolve(value_type="time", better="low")
    rnd = Round(
        code="G-A-1-1",
        stage="group",
        sides=[Side(team_id=f"t{i}") for i in range(1, 5)],
    )
    for side, ms in zip(rnd.sides, [25000, 0, 0, 0]):  # 只有 A 跑完，其余三方没成绩
        side.score = ms
    assert tournament.judge_round(rnd, allow_draw=False, scoring=sc) == "A"
    assert [side.rank for side in rnd.sides] == [1, 4, 4, 4]
    assert [tournament.placement_points(4, side.rank) for side in rnd.sides] == [4, 1, 1, 1]


def test_a_real_zero_is_a_score_but_blank_is_not():
    """有成绩的 0 分与「没填」必须分开：低胜下 0 分是最好的成绩，没填的一方永远垫底。

    这正是「不写成绩按垫底」最容易出事的地方——把漏填当成 0 分，它会直接判第 1。
    """
    sc = metrics.Scoring.resolve(value_type="integer", better="low")
    rnd = Round(
        code="G-A-1-1",
        stage="group",
        sides=[Side(team_id=f"t{i}") for i in range(1, 5)],
    )
    for side, value in zip(rnd.sides, [25, metrics.MISSING, 0, 18]):  # B 留空、C 真 0 分
        side.score = value
    assert tournament.judge_round(rnd, allow_draw=False, scoring=sc) == "C", "0 分是最好的成绩"
    assert [side.rank for side in rnd.sides] == [3, 4, 1, 2], "留空的一方垫底"
    assert metrics.as_scoring(sc).format(rnd.sides[1].score) == "—"


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
