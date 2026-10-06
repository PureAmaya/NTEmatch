"""对局级替补（**仅积分制**）：把「谁换谁」落成实际阵容，并支持撤销。

为什么只给积分制：积分制逐局独立排阵，某一场换人是常态；锦标赛制队伍全程固定，
没有「只换某一场」这回事（要换就整队换）。

为什么**直接改写对局阵容**而不是渲染时才套用：积分榜、场次、净胜分都按对局阵容
统计——渲染时套用会让「页面显示的人」和「统计口径的人」变成两套。代价是必须能
撤回，所以每次应用都留一条 :class:`~app.models.Substitution` 记录，撤回时按它还原。

三条硬规矩：

* **已结算的对局不改写**：打完的比赛保留当时实际出场的阵容与比分（与队伍换人同一约定）；
* **同一场不出现同一个人两次**：替补在本场已经上场时跳过该场（记进 ``conflict``）；
* **同一位选手在同一范围只有一处替补**：改人时先按旧记录还原、再应用新的，
  因此「最终只显示最终的替补」。

本模块只处理**原始 dict**（``store.mutate`` 的入参形态），不碰 IO 与 Web 框架，
便于单独测试；语义判定全部集中在 :func:`apply` 与 :func:`revert` 两处。
"""

from __future__ import annotations

from typing import Any

from .logging_conf import get_logger
from .models import Substitution

log = get_logger("subs")


# --------------------------------------------------------------------------- #
# 原始字典工具（与 main._raw_sides 同一套兼容写法）
# --------------------------------------------------------------------------- #
def raw_sides(rnd: dict[str, Any]) -> list[dict[str, Any]]:
    """一场比赛的各方阵容（以 ``sides`` 为准，兼容旧 ``sideA`` / ``sideB``）。"""
    sides = rnd.get("sides")
    if not sides:
        sides = [rnd.get("sideA") or {}, rnd.get("sideB") or {}]
        rnd["sides"] = sides
    return sides


def _lineup(side: dict[str, Any]) -> list[str]:
    ids = side.get("playerIds")
    if not isinstance(ids, list):
        ids = []
        side["playerIds"] = ids
    return ids


def round_index(rnd: dict[str, Any]) -> int:
    try:
        return int(rnd.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def round_code(rnd: dict[str, Any]) -> str:
    """对局的稳定标识：优先 ``code``，没有就用序号（与 ``_round_mutator`` 一致）。"""
    code = str(rnd.get("code") or "").strip()
    return code or str(round_index(rnd))


def is_settled(rnd: dict[str, Any]) -> bool:
    return str(rnd.get("status") or "") == "done"


# --------------------------------------------------------------------------- #
# 覆盖范围
# --------------------------------------------------------------------------- #
def _rounds(data: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = data.get("rounds")
    return rounds if isinstance(rounds, list) else []


def anchor_index(rounds: list[dict[str, Any]], anchor: str) -> int | None:
    """把起点的 ``code`` 换成对局序号；找不到返回 ``None``。"""
    want = str(anchor or "").strip()
    if not want:
        return None
    for rnd in rounds:
        if round_code(rnd) == want:
            return round_index(rnd)
    return None


def covered_rounds(data: dict[str, Any], sub: Substitution) -> list[dict[str, Any]]:
    """这条替补影响到的对局（按赛程顺序）。范围认不出来时返回空列表。"""
    rounds = _rounds(data)
    if sub.scope == "event":
        return list(rounds)
    start = anchor_index(rounds, sub.anchor)
    if start is None:
        return []
    if sub.scope == "round":
        return [rnd for rnd in rounds if round_index(rnd) == start]
    return [rnd for rnd in rounds if round_index(rnd) >= start]


def round_in_scope(data: dict[str, Any], sub: Substitution, rnd: dict[str, Any]) -> bool:
    """这条替补是否覆盖这一场比赛。"""
    return any(item is rnd for item in covered_rounds(data, sub))


def original_lineup(data: dict[str, Any], rnd: dict[str, Any]) -> list[str]:
    """某场比赛的**原始阵容**：把当前生效的替补按记录还原回去。

    替补直接改写了阵容，所以「原来是谁」只能从记录里反推——某场的 ``to_id``
    占着的位置，原本是 ``from_id`` 的。改人和取消都依赖这个换算：
    只看当前阵容的话，被换下的人会被判成「不在阵容里」，改人就没法做了。
    """
    ids = [pid for side in raw_sides(rnd) for pid in _lineup(side)]
    for sub in load(data):
        if sub.to_id in ids and round_in_scope(data, sub, rnd):
            ids[ids.index(sub.to_id)] = sub.from_id
    return ids


def appears_in(data: dict[str, Any], sub: Substitution) -> bool:
    """被替换的人在生效范围内是否真的上过场（用来拦住「换一个根本没上场的人」）。

    按**原始阵容**判断：这样「同一位选手换个范围再指定一次」不会被自己上一次的
    替补挡住。
    """
    return any(sub.from_id in original_lineup(data, rnd) for rnd in covered_rounds(data, sub))


# --------------------------------------------------------------------------- #
# 应用 / 撤销
# --------------------------------------------------------------------------- #
def _blank_result() -> dict[str, list[str]]:
    return {"changed": [], "locked": [], "conflict": []}


def apply(data: dict[str, Any], sub: Substitution) -> dict[str, list[str]]:
    """把替补写进受影响的对局阵容。

    返回 ``{"changed", "locked", "conflict"}``（对局 code 列表）：

    * ``changed``：真的换了人的对局；
    * ``locked``：已结算、按约定保留原阵容的对局；
    * ``conflict``：替补在本场已经上场，换了会出现「同一个人两边都是他」的对局。
    """
    out = _blank_result()
    for rnd in covered_rounds(data, sub):
        code = round_code(rnd)
        if is_settled(rnd):
            out["locked"].append(code)
            continue
        sides = raw_sides(rnd)
        from_side = next((side for side in sides if sub.from_id in _lineup(side)), None)
        if from_side is None:
            continue          # 这场他本来就没上（轮换制有人轮空），无需改动
        if any(sub.to_id in _lineup(side) for side in sides):
            out["conflict"].append(code)
            continue
        ids = _lineup(from_side)
        ids[ids.index(sub.from_id)] = sub.to_id
        out["changed"].append(code)
    return out


def revert(data: dict[str, Any], sub: Substitution) -> dict[str, list[str]]:
    """按记录把 ``to_id`` 换回 ``from_id``（只动还没结算、且确实换过的那一场）。"""
    out = _blank_result()
    for rnd in covered_rounds(data, sub):
        code = round_code(rnd)
        if is_settled(rnd):
            out["locked"].append(code)
            continue
        sides = raw_sides(rnd)
        to_side = next((side for side in sides if sub.to_id in _lineup(side)), None)
        if to_side is None:
            continue          # 没换过（或已被后续操作改掉），无需还原
        if any(sub.from_id in _lineup(side) for side in sides):
            out["conflict"].append(code)   # 原选手已经在场上，别换出两个人
            continue
        ids = _lineup(to_side)
        ids[ids.index(sub.to_id)] = sub.from_id
        out["changed"].append(code)
    return out


# --------------------------------------------------------------------------- #
# 记录维护
# --------------------------------------------------------------------------- #
def same_slot(sub: Substitution, *, from_id: str, scope: str, anchor: str) -> bool:
    """是否是「同一位选手 + 同一范围」的那一处替补（改人时按它覆盖）。"""
    return sub.from_id == from_id and sub.scope == scope and sub.anchor == anchor


def new_id(existing: list[Substitution]) -> str:
    """分配一个 ``s001`` 式编号（在已有记录里取最大号 +1，避免复用旧号）。"""
    used = {sub.id for sub in existing}
    index = len(used) + 1
    while f"s{index:03d}" in used:
        index += 1
    return f"s{index:03d}"


def load(data: dict[str, Any]) -> list[Substitution]:
    """把配置里的替补记录读成模型列表（脏数据直接丢弃，不让它拖垮整份配置）。"""
    out: list[Substitution] = []
    for raw in data.get("substitutions") or []:
        try:
            sub = Substitution.model_validate(raw)
        except (ValueError, TypeError):
            log.warning("替补记录无法解析，已忽略 | %s", raw)
            continue
        if sub.from_id and sub.to_id and sub.from_id != sub.to_id:
            out.append(sub)
    return out
