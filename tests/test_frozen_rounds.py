"""**已结束的比赛只读**——谁都不能再改它；读取与 QQ 播报照旧。

这是一条纪律性规则，所以钉在**写入闸门**上（``app.store.Store._guard_frozen_rounds``），
而不是靠每个接口自觉：录分、重置、判弃权、登记时间、替补换人、赛程重排——**以及以后
新加的接口**——最后都要过 ``Store.mutate``，而系统自己的上下游重算也在同一段 try 里跑完。
于是「以后无论代码怎么改，都不会影响到旧的比赛成绩」是**结构上**被保证的。

判定交给纯函数 ``app.tournament.finished_rounds_changed``（比对「成绩记录」那几个字段），
接口层的报错文案与前端只读视图都建在它上面。

三条边界，本文件逐条钉住：

* 「已完成」= **已有胜者**（与 ``announce.settled`` 同一口径）；**还没出结果的照旧能录分、
  能重置**——包括"标了结束但比分还没录"那一场（只是标记，要留着补录的入口）；
* 一场比赛一旦有结果（有胜者，含记成 ``DRAW`` 的平局）就锁住：比分、对手、名次、
  弃权留痕、起止时间都改不动了；
* 改一个数字、抹掉胜者与弃权留痕、把状态退回未开始、换对手（``teamId``）→ 一律拒绝；
* 队伍改名这类**展示字段**（label / source）刷新不算改成绩，照旧放行。
"""

from __future__ import annotations

import pytest

from app import db
from app.models import Player
from app.store import FrozenRoundError, store


@pytest.fixture(autouse=True)
async def _own_event():
    """自己建一届再跑：这组要往赛程里写「已经打完的比赛」，别脏了共用的那一届。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("只读用例届")
    mine = store.current_id
    try:
        yield
    finally:
        if previous and previous != mine:
            await store.switch_event(previous)
        await store.delete_event(mine)


def _index_of(code: str) -> int:
    """对局序号（同一届内唯一）：从编号尾部取数字，``L-2`` → 2。

    每次写入都按编号推同一个序号——序号撞车会当场违反库里的 ``(event_id, idx)`` 唯一约束。
    """
    tail = code.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 1


def _round(code: str = "L-1", *, done: bool = False, **overrides) -> dict:
    """一场积分制对局；``done=True`` 时是「已经打完」的样子（有胜者、有比分）。"""
    row = {
        "index": _index_of(code),
        "code": code,
        "stage": "league",
        "label": code,
        "status": "done" if done else "pending",
        "winner": "A" if done else "",
        "sides": [
            {"playerIds": ["p1"], "score": 2 if done else -1, "rank": 1 if done else 0},
            {"playerIds": ["p2"], "score": 1 if done else -1, "rank": 2 if done else 0},
        ],
    }
    row.update(overrides)
    return row


async def _seed(rounds: list[dict]) -> None:
    """铺一份积分制配置。

    4 名参与选手是**故意**的：低于 4 人时「重建赛程」会在生成那一步就报「选手不足」，
    于是永远走不到闸门——那样 ``test_every_round_writing_endpoint_hits_the_gate``
    就测不到真正要测的那条路。
    """
    await store.update(
        {
            "rules": {"format": "league"},
            "players": [Player(id=f"p{i}", name=name).dump() for i, name in enumerate("甲乙丙丁", 1)],
            "participants": ["p1", "p2", "p3", "p4"],
            "rounds": rounds,
        },
        actor="test",
    )


async def test_a_finished_match_cannot_be_changed():
    """改比分 / 抹掉胜者 / 退回未开始 / 换对手：**一个都不许**，而且库里必须原样。"""
    await _seed([_round(done=True)])
    attempts = (
        # 改比分（最常见的"手滑改错"）
        {"sides": [{"playerIds": ["p1"], "score": 9}, {"playerIds": ["p2"], "score": 0}]},
        # 抹掉胜者（相当于把结果取消）
        {"winner": ""},
        # 退回未开始 = 现实里说的"重新打开比赛"
        {"status": "pending", "winner": ""},
        # 换对手（对阵被改写）
        {
            "sides": [
                {"playerIds": ["p1"], "score": 2, "rank": 1},
                {"playerIds": ["p2"], "score": 1, "rank": 2, "teamId": "t9"},
            ]
        },
    )
    for patch in attempts:
        with pytest.raises(FrozenRoundError):
            await store.update({"rounds": [{**_round(done=True), **patch}]}, actor="test")

    rnd = store.snapshot().rounds[0]
    assert (rnd.status, rnd.winner) == ("done", "A"), "被拒绝的写入不许留在库里"
    assert [side.score for side in rnd.sides] == [2, 1]
    assert [side.rank for side in rnd.sides] == [1, 2]


async def test_an_unfinished_match_is_still_editable():
    """还没打完的对局照旧能改：标进行中、重置、再录分都不受影响（闸门别做过头）。

    只读是从**打完那一刻**起的：``live``（进行中）不算打完——比赛还在打，当然要能改。
    """
    await _seed([_round()])

    await store.update({"rounds": [{**_round(), "status": "live"}]}, actor="test")
    assert store.snapshot().rounds[0].status == "live", "进行中：标状态照旧"

    await store.update({"rounds": [_round()]}, actor="test")
    assert store.snapshot().rounds[0].status == "pending", "进行中：重置照旧"

    await store.update({"rounds": [_round(done=True)]}, actor="test")
    assert store.snapshot().rounds[0].status == "done", "打完之前录分照旧"


async def test_renaming_does_not_trip_the_guard():
    """展示字段（label 等）由赛程重算顺手刷新，**不算改成绩**——队伍改名不该被拦住。"""
    await _seed([_round(done=True)])
    await store.update({"rounds": [{**_round(done=True), "label": "A 组 · 第 1 轮"}]}, actor="test")
    assert store.snapshot().rounds[0].label == "A 组 · 第 1 轮"


async def test_marking_it_done_before_the_score_is_in_does_not_lock_it():
    """点「结束」只是标记：**比分还没录之前照样能补录**（闸门认的是"有没有结果"）。

    这是操作顺序的护栏：管理员顺手点了「结束」，回头再录比分——不该被"已结束只读"
    挡在门外。录出胜者（真正定局）之后才锁。
    """
    await _seed([_round("L-1")])

    await store.update({"rounds": [{**_round("L-1"), "status": "done"}]}, actor="test")
    assert store.snapshot().rounds[0].status == "done", "先标成结束"

    await store.update({"rounds": [_round("L-1", done=True)]}, actor="test")
    rnd = store.snapshot().rounds[0]
    assert (rnd.status, rnd.winner) == ("done", "A"), "回头补录比分照样算数"

    with pytest.raises(FrozenRoundError, match="只读"):
        await store.update({"rounds": [{**_round("L-1", done=True), "winner": "B"}]}, actor="test")


async def test_ending_a_match_still_allows_entering_the_score(admin_client):
    """端到端：先点「结束」、再录比分这条路必须通。

    否则就是「点一下就再也录不进成绩」——谁都会犯一次的顺序错误，不该由用户承担。
    """
    await _seed([_round("L-1")])
    res = await admin_client.post("/api/rounds/L-1/status", json={"status": "done"})
    assert res.status_code == 200, res.text

    res = await admin_client.post(
        "/api/rounds/L-1/result",
        json={
            "winner": "B",
            "sides": [
                {"playerIds": ["p1"], "score": 1},
                {"playerIds": ["p2"], "score": 2},
            ],
        },
    )
    assert res.status_code == 200, res.text
    rnd = store.snapshot().rounds[0]
    assert (rnd.status, rnd.winner) == ("done", "B"), "结束之后录的比分要生效"


async def test_writing_something_else_leaves_it_alone():
    """写别的对局、改赛制、改届信息……都碰不到它。"""
    await _seed([_round("L-1", done=True), _round("L-2")])
    await store.update(
        {
            "rounds": [_round("L-1", done=True), _round("L-2", done=True)],
            "event": {"title": "换个届名"},
        },
        actor="test",
    )
    first = next(r for r in store.snapshot().rounds if r.code == "L-1")
    assert (first.status, first.winner) == ("done", "A")
    assert [side.score for side in first.sides] == [2, 1]


async def test_clearing_or_rebuilding_the_schedule_is_refused():
    """清空 / 整批替换赛程 = 让打完的成绩**凭空消失**：同样拒绝（最容易漏的那个空子）。"""
    await _seed([_round("L-1", done=True), _round("L-2")])

    # 三种"重来一遍"的写法：清空、整批换成新编号、只留没打完的那场
    for rounds in ([], [_round("L-9")], [_round("L-2")]):
        with pytest.raises(FrozenRoundError, match="只读"):
            await store.update({"rounds": rounds}, actor="test")

    kept = [r.code for r in store.snapshot().rounds]
    assert kept == ["L-1", "L-2"], "被拒之后赛程必须原样"


async def test_deleting_a_finished_round_is_refused_but_a_pending_one_is_fine():
    """删**没打完**的照旧可以；删**已打完**的那一局是「成绩凭空消失」，拒绝。"""
    await _seed([_round("L-1", done=True), _round("L-2")])

    await store.update({"rounds": [_round("L-1", done=True)]}, actor="test")
    assert [r.code for r in store.snapshot().rounds] == ["L-1"], "删没打完的那场照旧可以"

    with pytest.raises(FrozenRoundError, match="不能删除"):
        await store.update({"rounds": []}, actor="test")
    assert [r.code for r in store.snapshot().rounds] == ["L-1"]


async def test_every_round_writing_endpoint_hits_the_gate(admin_client):
    """**把所有会改对局的接口逐个点一遍**：一个都不许漏。

    这是「制度化」的那一步：闸门挂在 ``Store.mutate`` 上，接口只要走 store 就自动被拦；
    本用例把这批口子点名钉住，以后新增 / 改动接口时不会有人"忘了走 store"还不自知。
    """
    await _seed([_round(done=True)])
    before = store.snapshot().rounds[0].model_dump()

    attempts = (
        ("录入比分", "POST", "/api/rounds/L-1/result", {"winner": "B"}),
        ("重置", "POST", "/api/rounds/L-1/reset", None),
        ("登记时间", "POST", "/api/rounds/L-1/times", {"startedAt": "2026-10-08T10:00"}),
        ("判弃权", "POST", "/api/rounds/L-1/walkover", {"side": "A"}),
        ("改状态", "POST", "/api/rounds/L-1/status", {"status": "pending"}),
        ("删除本局", "DELETE", "/api/rounds/L-1", None),
        ("清空赛程", "DELETE", "/api/rounds", None),
        ("重建赛程", "POST", "/api/schedule/generate", {}),
    )
    for name, method, url, payload in attempts:
        res = await admin_client.request(method, url, json=payload)
        assert res.status_code == 400, f"{name} 应当被拒，实际 {res.status_code}：{res.text[:200]}"
        assert "只读" in res.text, f"{name} 的拒绝理由要说明是「已结束的比赛只读」"

    assert store.snapshot().rounds[0].model_dump() == before, "被拒的写入不许留下任何痕迹"
