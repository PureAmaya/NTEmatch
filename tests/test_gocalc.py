"""Go 计算侧车与 Python 回退实现的逐局对拍。

一致性的唯一判据是「每局 A/B 双方与轮空备注完全相同」：选手池顺序、洗牌、
加权抽样、浮点累加次序，任何一处偏离都会让赛程分叉。侧车不可用时整组跳过
（未构建 ``go/bin/ntecalc`` 的环境不该因此变红）。
"""

from __future__ import annotations

import time

import pytest

from app import gocalc, league
from app.models import Player


def _pool(count: int) -> list[Player]:
    return [Player(id=f"p{index:02d}", name=f"选手{index:02d}") for index in range(1, count + 1)]


def _signature(rounds: list) -> list[tuple[tuple[str, ...], tuple[str, ...], str]]:
    return [(tuple(rnd.side_a.player_ids), tuple(rnd.side_b.player_ids), rnd.note) for rnd in rounds]


@pytest.fixture(scope="module", autouse=True)
def _require_sidecar():
    if not gocalc.available():
        pytest.skip("ntecalc 不可用（未构建 go/bin/ntecalc）")


# (选手数, 每队人数, 局数, seed)：覆盖「刚好一场」「有余量」「奇数人」「多队同场」
CASES = [
    (4, 2, 1, None),
    (4, 2, 5, 20261003),
    (7, 2, 5, 20261003),
    (8, 2, 7, 1),
    (9, 2, 6, -5),
    (12, 2, 10, 20261003),
    (6, 1, 5, 3),
    (9, 3, 4, 11),
    (11, 2, 3, 2**40 + 7),
]


@pytest.mark.parametrize("count,team_size,rounds,seed", CASES)
def test_go_matches_python(count, team_size, rounds, seed):
    players = _pool(count)
    got = league._rotation_via_gocalc(players, team_size, rounds, seed, True)
    assert got is not None, "侧车应当返回结果"
    want = league._rotation_via_python(players, team_size, rounds, seed, True)
    assert _signature(got) == _signature(want), (
        f"赛程不一致：{count}人/{team_size}v{team_size}/{rounds}局/seed={seed}"
    )


@pytest.mark.parametrize("seed", [1, 20261003, -5])
def test_go_matches_python_without_fair_rotation(seed):
    """``fair=False`` 走的是另一条代价分支，必须单独钉住。"""
    players = _pool(9)
    got = league._rotation_via_gocalc(players, 2, 5, seed, False)
    assert got is not None
    want = league._rotation_via_python(players, 2, 5, seed, False)
    assert _signature(got) == _signature(want)


def test_sidecar_covers_whole_pool():
    """每局 A ∪ B ∪ 轮空 = 整个选手池，且三方互不重叠。"""
    players = _pool(10)
    decided = gocalc.rotation([(p.id, p.display_name) for p in players], 2, 5, 20261003, True)
    assert decided is not None
    pool = {p.id for p in players}
    for item in decided:
        a, b, resting = set(item["a"]), set(item["b"]), set(item.get("resting") or [])
        assert len(a) == 2 and len(b) == 2
        assert not (a & b) and not (a & resting) and not (b & resting)
        assert a | b | resting == pool


def test_generate_falls_back_when_sidecar_unavailable(monkeypatch):
    """侧车不可用时结果仍与 Python 实现一致（回退路径必须永远可用）。"""
    monkeypatch.setattr(gocalc, "available", lambda: False)
    players = _pool(7)
    rounds, warnings = league.generate_rotation_rounds(players, 2, 5, 20261003, True)
    want = league._rotation_via_python(players, 2, 5, 20261003, True)
    assert _signature(rounds) == _signature(want)
    assert isinstance(warnings, list)


def test_benchmark_reports_speedup(capsys):
    """不做硬性断言，只把两条路径的耗时打出来，便于现场核对收益。"""
    players = _pool(12)
    started = time.perf_counter()
    league._rotation_via_python(players, 2, 10, 20261003, True)
    python_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    league._rotation_via_gocalc(players, 2, 10, 20261003, True)
    go_ms = (time.perf_counter() - started) * 1000
    with capsys.disabled():
        print(
            f"\n[gocalc] 12人/10局：python={python_ms:.1f}ms go={go_ms:.1f}ms 加速={python_ms / max(go_ms, 0.01):.1f}x"
        )
