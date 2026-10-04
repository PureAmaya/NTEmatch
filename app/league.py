"""积分制赛制：动态轮换分组 + 补赛 + 均分排名。

   参与选手 ──每局自动 2v2──▶ 逐局阵容（可手动换人 / 补位）
     └─ 每局独立结算：胜 +points_win，负 +points_lose，平 +points_draw
         └─ 按**均分（总得分 ÷ 场次）**降序排名，满 min_rank_played 场才参与名次

与锦标赛制（:mod:`app.tournament`）并存，由 ``rules.format`` 选择。
本模块为纯函数集合，不涉及 IO 与 Web 框架，便于单独测试。
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from itertools import combinations
from typing import Any

from . import metrics
from .logging_conf import get_logger
from .models import Config, Player, Round, Side, SideKey

log = get_logger("league")

# 启发式权重：越大越优先避免
_W_REPEAT_PARTNER = 14.0   # 重复搭档（2v2 轮换最需要避免）
_W_REPEAT_GROUP = 22.0     # 完全相同的四人组合重复
_W_REPEAT_OPPONENT = 3.0   # 重复对手
_W_REST_STREAK = 9.0       # 连续轮空
_W_APPEARANCE = 1.2        # 出场次数不均衡

_RESTARTS = 12             # 多起点搜索次数
_TRIALS = 200              # 每局候选名单抽样次数


# --------------------------------------------------------------------------- #
# 选手池（同样尊重本届参与名单）
# --------------------------------------------------------------------------- #
def joined_players(cfg: Config) -> list[Player]:
    chosen = {pid for pid in cfg.participants if pid}
    return [p for p in cfg.players if not chosen or p.id in chosen]


def active_pool(cfg: Config) -> list[Player]:
    """本届参与 + 启用 + 有名称的选手（**含替补**：补赛时优先照顾出场少的人）。"""
    return [p for p in joined_players(cfg) if p.active and p.name]


def rotation_pool(cfg: Config) -> list[Player]:
    """参与自动轮换的选手池。

    优先使用正式选手；人数不足以凑满一场时才启用替补，
    替补始终可以随后通过管理端手动换上。
    """
    actives = active_pool(cfg)
    mains = [p for p in actives if not p.substitute]
    subs = [p for p in actives if p.substitute]
    need = max(2, cfg.rules.team_size * 2)
    if cfg.rules.include_substitutes and len(mains) < need:
        pool = mains + subs
        log.debug("正式选手不足(%d<%d)，启用全部替补，池大小=%d", len(mains), need, len(pool))
        return pool
    return mains or subs


def round_label(index: int, total: int) -> str:
    return f"第 {index} 局" if total <= 0 else f"第 {index} / {total} 局"


def _make_round(index: int, side_a: list[Player], side_b: list[Player], total: int, note: str = "") -> Round:
    return Round(
        index=index,
        code=f"L-{index}",
        stage="league",
        bracket_round=1,
        slot=index,
        label=round_label(index, total),
        sides=[Side(player_ids=[p.id for p in side_a]), Side(player_ids=[p.id for p in side_b])],
        note=note,
    )


# --------------------------------------------------------------------------- #
# 自动分组
# --------------------------------------------------------------------------- #
def _partner_keys(ids: list[str]) -> list[tuple[str, str]]:
    ordered = sorted(ids)
    return [(ordered[i], ordered[j]) for i in range(len(ordered)) for j in range(i + 1, len(ordered))]


def _opponent_keys(a_ids: list[str], b_ids: list[str]) -> list[tuple[str, str]]:
    return [(x, y) if x < y else (y, x) for x in a_ids for y in b_ids]


def _weighted_sample(
    pool: list[Player], weights: list[float], count: int, rng: random.Random
) -> list[Player]:
    """按权重不放回抽样。"""
    items = list(pool)
    wts = list(weights)
    picked: list[Player] = []
    for _ in range(min(count, len(items))):
        total = sum(wts)
        target = rng.random() * total
        acc = 0.0
        idx = len(items) - 1
        for i, weight in enumerate(wts):
            acc += weight
            if acc >= target:
                idx = i
                break
        picked.append(items.pop(idx))
        wts.pop(idx)
    return picked


def _pick_candidates(
    base: list[Player],
    need: int,
    hist: dict[str, Counter],
    fair: bool,
    rng: random.Random,
    trials: int,
) -> Iterator[list[Player]]:
    """产生候选出场名单：出场少 / 轮空久的选手权重更高。"""
    if len(base) <= need:
        yield list(base)
        return
    for _ in range(trials):
        weights: list[float] = []
        for player in base:
            weight = 1.0 / (1.0 + 0.6 * hist["appear"][player.id])
            if fair:
                weight += 0.5 * hist["rest"][player.id]
            weights.append(max(weight, 0.02))
        yield _weighted_sample(base, weights, need, rng)


def _split_cost(
    side_a: list[Player], side_b: list[Player], ids_key: tuple[str, ...], hist: dict[str, Counter]
) -> float:
    cost = 0.0
    for group in (side_a, side_b):
        for key in _partner_keys([p.id for p in group]):
            cost += hist["partner"][key] * _W_REPEAT_PARTNER
    for key in _opponent_keys([p.id for p in side_a], [p.id for p in side_b]):
        cost += hist["opponent"][key] * _W_REPEAT_OPPONENT
    cost += hist["group"][ids_key] * _W_REPEAT_GROUP
    return cost


def _best_split(
    picked: list[Player], team_size: int, hist: dict[str, Counter]
) -> tuple[list[Player], list[Player]]:
    """在给定出场名单内枚举分队方式，取重复代价最小的一种。"""
    ids = [p.id for p in picked]
    ids_key = tuple(sorted(ids))
    anchor, rest = ids[0], ids[1:]
    best: tuple[float, list[Player], list[Player]] | None = None
    for combo in combinations(rest, team_size - 1):
        a_ids = {anchor, *combo}
        side_a = [p for p in picked if p.id in a_ids]
        side_b = [p for p in picked if p.id not in a_ids]
        if len(side_b) != team_size:
            continue
        cost = _split_cost(side_a, side_b, ids_key, hist)
        if best is None or cost < best[0] - 1e-9:
            best = (cost, side_a, side_b)
    assert best is not None
    return best[1], best[2]


def _build_rotation_rounds(
    players: list[Player], team_size: int, total_rounds: int, rng: random.Random, fair: bool
) -> tuple[list[Round], float]:
    """单次随机搜索：逐局贪心，返回 (赛程, 总代价)。"""
    need = team_size * 2
    base = list(players)
    rng.shuffle(base)
    hist: dict[str, Counter] = {
        "partner": Counter(),
        "opponent": Counter(),
        "group": Counter(),
        "rest": Counter(),
        "appear": Counter(),
    }

    rounds: list[Round] = []
    total_cost = 0.0
    trials = _TRIALS if len(base) > need else 1

    for r in range(total_rounds):
        best: tuple[float, list[Player], list[Player], list[Player]] | None = None
        for picked in _pick_candidates(base, need, hist, fair, rng, trials):
            side_a, side_b = _best_split(picked, team_size, hist)
            picked_ids = {p.id for p in picked}
            resting = [p for p in base if p.id not in picked_ids]
            cost = _split_cost(side_a, side_b, tuple(sorted(picked_ids)), hist)
            if fair:
                for player in resting:
                    cost += hist["rest"][player.id] * _W_REST_STREAK
                for player in side_a + side_b:
                    cost += hist["appear"][player.id] * _W_APPEARANCE
            if best is None or cost < best[0] - 1e-9:
                best = (cost, side_a, side_b, resting)
        assert best is not None
        cost, side_a, side_b, resting = best
        total_cost += cost

        for group in (side_a, side_b):
            for key in _partner_keys([p.id for p in group]):
                hist["partner"][key] += 1
        for key in _opponent_keys([p.id for p in side_a], [p.id for p in side_b]):
            hist["opponent"][key] += 1
        hist["group"][tuple(sorted(p.id for p in side_a + side_b))] += 1
        for player in side_a + side_b:
            hist["appear"][player.id] += 1
            hist["rest"][player.id] = 0
        for player in resting:
            hist["rest"][player.id] += 1

        rounds.append(
            _make_round(
                r + 1,
                side_a,
                side_b,
                total_rounds,
                note=("轮空: " + "、".join(p.display_name for p in resting)) if resting else "",
            )
        )
    return rounds, total_cost


def _max_rest_streak(players: list[Player], rounds: list[Round]) -> int:
    streak = {p.id: 0 for p in players}
    ids = list(streak)
    worst = 0
    for rnd in rounds:
        playing = set(rnd.side_a.player_ids) | set(rnd.side_b.player_ids)
        for pid in ids:
            if pid in playing:
                streak[pid] = 0
            else:
                streak[pid] += 1
                worst = max(worst, streak[pid])
    return worst


def generate_rotation_rounds(
    players: list[Player],
    team_size: int,
    total_rounds: int,
    seed: int | None = None,
    fair: bool = True,
) -> tuple[list[Round], list[str]]:
    """动态轮换赛程（多起点随机搜索）。

    每局从选手池中选出 ``2 * team_size`` 人上场（2v2 即 4 人），其余轮空。
    目标：避免重复搭档、避免重复四人组合、尽量少重复对手，并均衡出场与轮空。
    """
    need = team_size * 2
    if len(players) < need:
        raise ValueError(f"可用选手 {len(players)} 人，少于单场所需的 {need} 人")

    base_seed = seed if seed is not None else 20261003
    best: tuple[float, list[Round]] | None = None
    for restart in range(_RESTARTS):
        rng = random.Random(base_seed + restart * 7919)
        rounds, cost = _build_rotation_rounds(players, team_size, total_rounds, rng, fair)
        if best is None or cost < best[0] - 1e-9:
            best = (cost, rounds)
    assert best is not None
    rounds = best[1]

    quality = quality_of_rounds(rounds)
    warnings: list[str] = []
    if quality["partnerRepeats"]:
        warnings.append(f"存在 {quality['partnerRepeats']} 组重复搭档，建议增加选手或减少总轮次。")
    if quality["groupRepeats"]:
        warnings.append(f"存在 {quality['groupRepeats']} 次四人组合重复。")
    if quality["opponentRepeats"]:
        warnings.append(f"存在 {quality['opponentRepeats']} 组重复对手（可接受，仅供参考）。")
    if _max_rest_streak(players, rounds) >= 2:
        warnings.append("存在选手连续两局以上轮空，可增加总轮次或扩大报名池。")
    log.info(
        "赛程生成完毕 | 模式=动态轮换 | 选手池=%d | 局数=%d | seed=%s | 重复搭档=%d | 重复对手=%d",
        len(players),
        total_rounds,
        seed,
        quality["partnerRepeats"],
        quality["opponentRepeats"],
    )
    return rounds, warnings


def generate_fixed_rounds(
    teams: list[dict[str, Any]], total_rounds: int, seed: int | None = None
) -> tuple[list[Round], list[str]]:
    """固定队伍赛程：对已配置的队伍用轮转法（circle method）排对阵。"""
    warnings: list[str] = []
    valid = [t for t in teams if len(t.get("playerIds") or []) >= 2]
    if len(valid) < 2:
        raise ValueError("固定队伍模式至少需要 2 支已配置成员的队伍")

    rng = random.Random(seed if seed is not None else 20261003)
    pool = list(valid)
    rng.shuffle(pool)
    count = len(pool)
    rotation = pool[1:]
    rounds: list[Round] = []
    for r in range(total_rounds):
        top = pool[0]
        bottom = rotation[count - 2] if count >= 2 else top
        if r % 2 == 1:
            top, bottom = bottom, top
        if top["id"] == bottom["id"]:
            warnings.append("队伍数量为奇数时无法生成不同对手的组合，已跳过重复对局。")
            continue
        idx = len(rounds) + 1
        rounds.append(
            Round(
                index=idx,
                code=f"L-{idx}",
                stage="league",
                bracket_round=1,
                slot=idx,
                label=round_label(idx, total_rounds),
                sides=[
                    Side(
                        team_id=top["id"],
                        player_ids=list(top.get("playerIds") or []),
                        label=top.get("short") or top.get("name") or "",
                    ),
                    Side(
                        team_id=bottom["id"],
                        player_ids=list(bottom.get("playerIds") or []),
                        label=bottom.get("short") or bottom.get("name") or "",
                    ),
                ],
            )
        )
        rotation = rotation[1:] + rotation[:1]
    if not rounds:
        raise ValueError("未能生成任何对局，请检查队伍成员配置")
    log.info("赛程生成完毕 | 模式=固定队伍 | 队伍=%d | 局数=%d", count, len(rounds))
    return rounds, warnings


def generate_schedule(
    cfg: Config, mode: str = "rotate", total_rounds: int | None = None, seed: int | None = None
) -> tuple[list[Round], list[str]]:
    """积分制赛程生成入口。mode: ``rotate``（动态轮换）或 ``fixed``（固定队伍）。"""
    rounds_count = int(total_rounds or cfg.rules.total_rounds or 5)
    rounds_count = max(1, min(rounds_count, 50))
    if mode == "fixed":
        allowed = {p.id for p in joined_players(cfg)}
        teams: list[dict[str, Any]] = []
        for team in cfg.teams:
            data = team.dump()
            data["playerIds"] = [pid for pid in data.get("playerIds", []) if pid in allowed]
            teams.append(data)
        return generate_fixed_rounds(teams, rounds_count, seed)
    return generate_rotation_rounds(
        rotation_pool(cfg), cfg.rules.team_size, rounds_count, seed, cfg.rules.fair_rotation
    )


def quality_of_rounds(rounds: Iterable[Round]) -> dict[str, int]:
    """统计赛程质量：重复搭档 / 重复对手 / 重复四人组合的数量。"""
    partner: Counter = Counter()
    opponent: Counter = Counter()
    group: Counter = Counter()
    for rnd in rounds:
        a_ids, b_ids = rnd.side_a.player_ids, rnd.side_b.player_ids
        for ids in (a_ids, b_ids):
            for key in _partner_keys(ids):
                partner[key] += 1
        for key in _opponent_keys(a_ids, b_ids):
            opponent[key] += 1
        group[tuple(sorted(a_ids + b_ids))] += 1
    return {
        "partnerRepeats": sum(1 for v in partner.values() if v > 1),
        "opponentRepeats": sum(1 for v in opponent.values() if v > 1),
        "groupRepeats": sum(1 for v in group.values() if v > 1),
        "partners": len(partner),
        "opponents": len(opponent),
        "groups": len(group),
    }


def schedule_quality(cfg: Config) -> dict[str, int]:
    """当前赛程的质量统计（供前端展示「是否重复组队」）。"""
    return quality_of_rounds(cfg.rounds)


# --------------------------------------------------------------------------- #
# 追加补赛：让出场偏少的选手补足场次（队友随机）
# --------------------------------------------------------------------------- #
def history_from_rounds(rounds: Iterable[Round]) -> dict[str, Counter]:
    """由已有对局重建历史计数，使补赛继续遵守「避免重复搭档」的约束。"""
    hist: dict[str, Counter] = {
        "partner": Counter(),
        "opponent": Counter(),
        "group": Counter(),
        "rest": Counter(),
        "appear": Counter(),
    }
    for rnd in rounds:
        a_ids, b_ids = rnd.side_a.player_ids, rnd.side_b.player_ids
        for group_ids in (a_ids, b_ids):
            for key in _partner_keys(group_ids):
                hist["partner"][key] += 1
        for key in _opponent_keys(a_ids, b_ids):
            hist["opponent"][key] += 1
        hist["group"][tuple(sorted(a_ids + b_ids))] += 1
        for pid in a_ids + b_ids:
            hist["appear"][pid] += 1
    return hist


def append_rounds(cfg: Config, count: int = 1, seed: int | None = None) -> tuple[list[Round], list[str]]:
    """为出场次数最少的选手追加补赛。

    只返回**新增**的对局：既有比分与状态完全不受影响；
    候选按出场次数升序挑选（同次数内部随机），队友在候选中随机分配，
    并尽量避免与已有赛程重复搭档。
    """
    team_size = max(1, cfg.rules.team_size)
    need = team_size * 2
    pool = active_pool(cfg)
    if len(pool) < need:
        raise ValueError(f"可用选手 {len(pool)} 人，少于单场所需的 {need} 人")

    rng = random.Random(seed if seed is not None else random.randrange(1_000_000))
    hist = history_from_rounds(cfg.rounds)
    base_index = len(cfg.rounds)

    rounds: list[Round] = []
    for step in range(count):
        ranked = sorted(pool, key=lambda p: (hist["appear"][p.id], rng.random()))
        picked = ranked[:need]
        side_a, side_b = _best_split(picked, team_size, hist)
        idx = base_index + step + 1
        rounds.append(
            _make_round(idx, side_a, side_b, base_index + count, note="补赛 · 优先安排出场较少的选手")
        )
        for group_ids in ([p.id for p in side_a], [p.id for p in side_b]):
            for key in _partner_keys(group_ids):
                hist["partner"][key] += 1
        for key in _opponent_keys([p.id for p in side_a], [p.id for p in side_b]):
            hist["opponent"][key] += 1
        hist["group"][tuple(sorted(p.id for p in side_a + side_b))] += 1
        for player in side_a + side_b:
            hist["appear"][player.id] += 1

    counts = [hist["appear"][p.id] for p in pool]
    warnings: list[str] = []
    if counts:
        lo, hi = min(counts), max(counts)
        if hi - lo > 1:
            warnings.append(f"补赛后出场次数仍相差 {hi - lo} 场（{lo}–{hi}），可继续追加补赛。")
        else:
            warnings.append("补赛后各选手出场次数已基本持平。")
    log.info(
        "已追加补赛 | 新增局数=%d | 选手池=%d | seed=%s | 出场区间=%s",
        len(rounds),
        len(pool),
        seed,
        (min(counts), max(counts)) if counts else None,
    )
    return rounds, warnings


# --------------------------------------------------------------------------- #
# 按参与名单重排未开赛对局
# --------------------------------------------------------------------------- #
def _accumulate_history(hist: dict[str, Counter], rnd: Round) -> None:
    a_ids, b_ids = rnd.side_a.player_ids, rnd.side_b.player_ids
    for group_ids in (a_ids, b_ids):
        for key in _partner_keys(group_ids):
            hist["partner"][key] += 1
    for key in _opponent_keys(a_ids, b_ids):
        hist["opponent"][key] += 1
    hist["group"][tuple(sorted(a_ids + b_ids))] += 1
    for pid in a_ids + b_ids:
        hist["appear"][pid] += 1


def _fill_candidate(
    pool: list[Player],
    order: dict[str, int],
    used: set[str],
    mates: list[str],
    others: list[str],
    hist: dict[str, Counter],
) -> str | None:
    """为空位挑一名选手：出场少优先，其次尽量不重复搭档、不重复对手。"""
    best: tuple[tuple[int, int, int, int], str] | None = None
    for player in pool:
        pid = player.id
        if pid in used:
            continue
        partner_cost = sum(hist["partner"][tuple(sorted((pid, mate)))] for mate in mates)
        opponent_cost = sum(hist["opponent"][tuple(sorted((pid, other)))] for other in others)
        rank = (hist["appear"][pid], partner_cost, opponent_cost, order.get(pid, 0))
        if best is None or rank < best[0]:
            best = (rank, pid)
    return best[1] if best else None


def reconcile_rounds(cfg: Config) -> tuple[list[Round], list[str]]:
    """按当前参与名单重排**未结算**的对局。

    * 已完成或已锁定的对局原样保留（历史与比分不可改）；
    * 其余对局移除不在参与名单内的选手，空位从参与池补人，始终保持 ``team_size`` 对 ``team_size``；
    * 补人优先照顾出场次数少的选手，并尽量避免重复搭档与重复对手（排序确定，无随机）。
    """
    team_size = max(1, cfg.rules.team_size)
    need = team_size * 2
    pool = active_pool(cfg)
    if not pool:
        raise ValueError("本届还没有可用选手，请先录入选手并勾选参与人员")
    if len(pool) < need:
        raise ValueError(f"本届参与名单中可用选手 {len(pool)} 人，少于单场所需的 {need} 人")

    allowed = {p.id for p in pool}
    order = {p.id: idx for idx, p in enumerate(pool)}
    hist = history_from_rounds([r for r in cfg.rounds if r.status == "done" or r.locked])

    rounds: list[Round] = []
    removed = 0
    added = 0
    for rnd in sorted(cfg.rounds, key=lambda r: r.index):
        if rnd.status == "done" or rnd.locked:
            rounds.append(rnd)
            _accumulate_history(hist, rnd)
            continue

        kept: dict[SideKey, list[str]] = {"A": [], "B": []}
        used: set[str] = set()
        for key in ("A", "B"):
            side = rnd.side_a if key == "A" else rnd.side_b
            for pid in side.player_ids:
                if pid not in allowed or pid in used or len(kept[key]) >= team_size:
                    removed += 1
                else:
                    kept[key].append(pid)
                    used.add(pid)

        for key in ("A", "B"):
            other: SideKey = "B" if key == "A" else "A"
            while len(kept[key]) < team_size:
                pid = _fill_candidate(pool, order, used, kept[key], kept[other], hist)
                if pid is None:
                    break
                kept[key].append(pid)
                used.add(pid)
                added += 1

        rebuilt = rnd.model_copy(deep=True)
        rebuilt.side_a = rnd.side_a.model_copy(update={"player_ids": list(kept["A"])})
        rebuilt.side_b = rnd.side_b.model_copy(update={"player_ids": list(kept["B"])})
        if rebuilt.note.startswith("轮空"):
            resting = [p for p in pool if p.id not in used]
            rebuilt.note = ("轮空: " + "、".join(p.display_name for p in resting)) if resting else ""
        rounds.append(rebuilt)
        _accumulate_history(hist, rebuilt)

    warnings: list[str] = []
    if removed:
        warnings.append(f"已从赛程移除 {removed} 人次（不在本届参与名单内）。")
    if added:
        warnings.append(f"已自动补入 {added} 人次，保持 {team_size}v{team_size} 阵容。")
    quality = quality_of_rounds(rounds)
    if quality["partnerRepeats"]:
        warnings.append(f"调整后存在 {quality['partnerRepeats']} 组重复搭档，可减少总轮次或增加参与选手。")
    if not warnings:
        warnings.append("赛程与参与名单一致，无需调整。")
    log.info(
        "已按参与名单重排赛程 | 参与=%d | 局数=%d | 移除=%d | 补入=%d",
        len(pool),
        len(rounds),
        removed,
        added,
    )
    return rounds, warnings


# --------------------------------------------------------------------------- #
# 结算与排行
# --------------------------------------------------------------------------- #
def _empty_stat(player_id: str) -> dict[str, Any]:
    return {
        "playerId": player_id,
        "played": 0,
        "win": 0,
        "lose": 0,
        "draw": 0,
        "points": 0,
        "average": 0.0,
        "scored": 0,
        "conceded": 0,
        "diff": 0,
        # 用时制的 tiebreak 用：完成场次与总用时（计分制下不参与排序）
        "finished": 0,
        "spent": 0,
        "rest": 0,
        "streak": 0,
        "bestStreak": 0,
        "form": [],
    }


def compute_standings(cfg: Config) -> dict[str, Any]:
    """按积分制统计个人榜（每局独立结算、多局累计积分）。

    排名规则：
    * 场次 ``>= rules.min_rank_played`` 才参与排名，按**均分（总得分 ÷ 场次）**降序；
      均分相同再比总得分、胜场，最后比「分项」——计分制看净胜分，
      用时制看完成场次 + 总用时（见 :mod:`app.metrics`）。
    * 场次不足者 ``rank`` 为 ``None``、``qualified`` 为 ``False``，统一排在榜尾。
    * 榜内只统计**本届参与名单**中的选手；已打过已结算对局的选手即便被移出名单，
      其成绩仍保留，避免历史记录凭空消失。
    """
    rules = cfg.rules
    metric = metrics.norm(rules.metric)
    time_based = metrics.lower_is_better(metric)
    played_ids = {
        pid
        for rnd in cfg.rounds
        if rnd.status == "done" and rnd.winner
        for pid in (rnd.side_a.player_ids + rnd.side_b.player_ids)
    }
    keep_ids = {p.id for p in joined_players(cfg)} | played_ids
    players = {p.id: p for p in cfg.players if p.id in keep_ids}
    stats: dict[str, dict[str, Any]] = {pid: _empty_stat(pid) for pid in players}

    completed = [r for r in cfg.rounds if r.status == "done" and r.winner]
    for rnd in sorted(completed, key=lambda r: r.index):
        _apply_round(
            stats, rnd, rules.points_win, rules.points_lose, rules.points_draw, metric
        )

    # 轮空统计：未完成的局不计入
    for rnd in cfg.rounds:
        involved = set(rnd.side_a.player_ids) | set(rnd.side_b.player_ids)
        for pid in players:
            if pid not in involved:
                stats[pid]["rest"] += 1

    for row in stats.values():
        row["average"] = round(row["points"] / row["played"], 2) if row["played"] else 0.0
        row["winRate"] = round(row["win"] / row["played"] * 100, 1) if row["played"] else 0.0
        row["player"] = players[row["playerId"]].public()

    threshold = max(0, rules.min_rank_played)
    qualified = [row for row in stats.values() if row["played"] >= threshold]
    unqualified = [row for row in stats.values() if row["played"] < threshold]

    def rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
        """名次依据：均分 → 总积分 → 胜场 → 分项 → 姓名（完全确定，无随机）。

        分项随比法变化：计分制看净胜分（分多者优）；用时制看完成场次 + 总用时——
        未完赛的人不能因为「没跑完所以时间短」占到便宜。
        """
        tail: tuple[Any, ...] = (-row["finished"], row["spent"]) if time_based else (-row["diff"],)
        return (
            -row["average"],
            -row["points"],
            -row["win"],
            *tail,
            players[row["playerId"]].display_name,
        )

    qualified.sort(key=rank_key)
    unqualified.sort(
        key=lambda s: (
            -s["played"],
            -s["average"],
            -s["points"],
            players[s["playerId"]].display_name,
        )
    )

    for rank, row in enumerate(qualified, start=1):
        row["rank"] = rank
        row["qualified"] = True
    for row in unqualified:
        row["rank"] = None
        row["qualified"] = False

    table = qualified + unqualified
    played_rounds = len(completed)
    total = len(cfg.rounds)
    return {
        "players": table,
        "progress": {
            "played": played_rounds,
            "total": total,
            "live": sum(1 for r in cfg.rounds if r.status == "live"),
            "pending": sum(1 for r in cfg.rounds if r.status == "pending"),
            "percent": round(played_rounds / total * 100, 1) if total else 0.0,
        },
        "leader": qualified[0] if qualified else None,
        "minRankPlayed": threshold,
        "quality": quality_of_rounds(cfg.rounds),
    }


def _apply_round(
    stats: dict[str, dict[str, Any]],
    rnd: Round,
    points_win: int,
    points_lose: int,
    points_draw: int,
    metric: str = metrics.SCORE,
) -> None:
    winner = rnd.winner
    if winner not in ("A", "B", "DRAW"):
        return
    outcome: dict[SideKey, str] = {"A": "", "B": ""}
    if winner == "DRAW":
        outcome = {"A": "draw", "B": "draw"}
    else:
        loser: SideKey = "B" if winner == "A" else "A"
        outcome[winner] = "win"  # type: ignore[index]
        outcome[loser] = "lose"

    for key in ("A", "B"):
        side: Side = rnd.side_a if key == "A" else rnd.side_b
        other: Side = rnd.side_b if key == "A" else rnd.side_a
        result = outcome[key]
        gain = {"win": points_win, "lose": points_lose, "draw": points_draw}.get(result, 0)
        for pid in [pid for pid in side.player_ids if pid in stats]:
            row = stats[pid]
            row["played"] += 1
            row[result] += 1
            row["points"] += gain
            row["scored"] += side.score
            row["conceded"] += other.score
            row["diff"] += side.score - other.score
            # 该场的总成绩（填了各局就是各局合计）：用时制用它排 tiebreak
            total = metrics.round_total(side.score, side.points, bool(rnd.sets))
            if total > 0:
                row["finished"] += 1
                row["spent"] += total
            if result == "win":
                row["streak"] += 1
                row["bestStreak"] = max(row["bestStreak"], row["streak"])
            elif result == "lose":
                row["streak"] = 0
            row["form"].append("W" if result == "win" else ("L" if result == "lose" else "D"))


def player_round_counts(cfg: Config) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for rnd in cfg.rounds:
        for pid in rnd.side_a.player_ids + rnd.side_b.player_ids:
            counts[pid] += 1
    return dict(counts)
