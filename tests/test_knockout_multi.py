"""淘汰赛多队同场（偏好队伍数）+ 取消队伍缩写的迁移。

对应需求：

* 淘汰赛每场同场队伍数可设偏好（2 = 标准 1v1；3/4 = 多队同场取第 1 名晋级），
  轮次按「当前队伍数尽量拆成每场不超过偏好数的若干场、每场至少 2 队」递进；
* 队伍不再有 ``short`` 字段，旧数据的缩写并入队名。
"""

from __future__ import annotations

from app import metrics, tournament
from app.models import Player, Rules, Team
from app.store import merge_short_names_in_event


def _teams(count: int) -> list[Team]:
    return [
        Team(id=f"t{i:02d}", name=f"{i} 队", player_ids=[f"p{i}"])
        for i in range(1, count + 1)
    ]


def _play_tournament(
    team_count: int, per_match: int, loser_bracket: bool
) -> tuple[list, Team | None, dict]:
    """造一届锦标赛并把所有对局按「第一方胜」打完，返回 (对局, 冠军, 概要)。"""
    teams = _teams(team_count)
    tournament.assign_groups(teams, max(1, team_count // 3))
    rules = Rules(
        teams_per_match=2,
        knockout_teams_per_match=per_match,
        loser_bracket=loser_bracket,
    )
    rounds, _warnings, summary = tournament.build_tournament(teams, rules)
    sc = metrics.INTEGER
    for rnd in rounds:
        if rnd.stage == "group" and rnd.status != "done":
            for i, side in enumerate(rnd.sides):
                side.score = 10 if i == 0 else i
            rnd.winner = tournament.judge_round(rnd, scoring=sc)
            rnd.status = "done"
    for _ in range(500):
        rounds = tournament.resolve_tournament(teams, rounds, sc)
        nxt = next(
            (
                r
                for r in rounds
                if r.stage in ("wb", "lb", "gf")
                and r.status != "done"
                and all(s.team_id for s in r.sides)
            ),
            None,
        )
        if nxt is None:
            break
        for i, side in enumerate(nxt.sides):
            side.score = 10 if i == 0 else i
        nxt.winner = tournament.judge_round(nxt, scoring=sc)
        nxt.status = "done"
    rounds = tournament.resolve_tournament(teams, rounds, sc)
    return rounds, tournament.champion_of(teams, rounds), summary


def test_heat_sizes_never_below_two():
    """每场至少 2 队；余数摊到前排，场数尽量少。"""
    assert tournament.knockout_heat_sizes(8, 3) == [3, 3, 2]
    assert tournament.knockout_heat_sizes(8, 4) == [4, 4]
    assert tournament.knockout_heat_sizes(5, 3) == [3, 2]
    assert tournament.knockout_heat_sizes(4, 3) == [2, 2]  # 3+1 不成立，退成 2+2
    assert tournament.knockout_heat_sizes(2, 4) == [2]
    assert all(min(tournament.knockout_heat_sizes(n, k)) >= 2 for n in range(2, 20) for k in (2, 3, 4))


def test_multi_team_knockout_runs_to_a_champion():
    """多队同场的双败 / 单败都能从小组赛一路推进到冠军，且每场 2~4 队、无空席。"""
    for per_match in (3, 4):
        for loser in (True, False):
            rounds, champion, summary = _play_tournament(12, per_match, loser)
            assert champion is not None, (per_match, loser)
            ko = [r for r in rounds if r.stage in ("wb", "lb", "gf")]
            assert ko, (per_match, loser)
            for rnd in ko:
                assert 2 <= len(rnd.sides) <= 4, (rnd.code, len(rnd.sides))
                assert all(s.team_id for s in rnd.sides), rnd.code
            # 规模反推应与生成时一致（多队同场不能退化成「场次 × 2」）
            assert tournament.size_from_rounds(rounds) == summary["size"], (per_match, loser)


def test_single_elim_multi_team_final_keeps_full_size():
    """单败且决赛即唯一一场（队伍数 <= 偏好）时，规模反推不能塌成 2。"""
    rounds, champion, summary = _play_tournament(4, 4, False)
    assert summary["size"] == 4
    assert tournament.size_from_rounds(rounds) == 4
    final = next(r for r in rounds if r.stage == "gf")
    assert len(final.sides) == 4
    assert champion is not None


def test_merge_short_names_fills_name_and_relabels_rounds():
    """旧数据的缩写：队名为空时补齐队名，并把对阵里存的旧缩写标签换成队名。"""
    data = {
        "teams": [
            {"id": "t01", "name": "", "short": "猫粮"},
            {"id": "t02", "name": "夜刃", "short": "YY"},
        ],
        "rounds": [
            {
                "code": "G-A-1-1",
                "sides": [
                    {"teamId": "t01", "label": "猫粮"},
                    {"teamId": "t02", "label": "YY"},
                ],
            }
        ],
    }
    out = merge_short_names_in_event(data)
    assert out["teams"][0]["name"] == "猫粮"
    assert "short" not in out["teams"][0]
    assert out["teams"][1]["name"] == "夜刃"  # 已有队名保留
    assert "short" not in out["teams"][1]
    assert out["rounds"][0]["sides"][0]["label"] == "猫粮"
    assert out["rounds"][0]["sides"][1]["label"] == "夜刃"


def test_random_teams_have_no_short_field():
    """随机组队产生的队伍不带缩写字段（字段已取消）。"""
    players = [Player(id=f"p{i}", name=f"选手{i}") for i in range(1, 5)]
    teams, _warnings = tournament.auto_form_teams(players, 2, seed=1)
    for team in teams:
        assert "short" not in team.model_dump(by_alias=True)
        assert team.label == team.name
