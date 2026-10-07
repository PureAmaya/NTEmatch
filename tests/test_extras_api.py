"""附加数值（距离）的接口往返：录进去、存下来、读得回，并按它排出没成绩的人。

单独一份：这条路径横跨「接口字段 → 数据库列（JSON）→ 模型 → 判定 → 排行榜」，
任何一段漏了都会表现为「填了距离但名次没变」——那种 bug 在界面上看不出来。
"""

from __future__ import annotations

from app import db, metrics
from app.store import store


async def _fresh_event(name: str) -> tuple[str, str]:
    """建一届四队同场的锦标赛，返回 ``(届次 id, 对局编号)``。上一届与它稍后由调用方清理。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    await store.create_event(name)
    event_id = store.current_id
    await store.update(
        {
            "players": [{"id": f"p{i}", "name": f"选手{i}"} for i in range(1, 5)],
            "participants": ["p1", "p2", "p3", "p4"],
            "participantsSet": True,
            "rules": {
                "format": "tournament",
                "teamsPerMatch": 4,
                "valueType": "time",
                "better": "low",
            },
            "teams": [
                {"id": f"t{i:02d}", "name": f"{i} 队", "playerIds": [f"p{i}"]} for i in range(1, 5)
            ],
            "rounds": [
                {
                    "index": 1,
                    "code": "G-A-1-1",
                    "stage": "group",
                    "label": "A 组 · 第 1 轮 · 第 1 场",
                    "sides": [{"teamId": f"t{i:02d}"} for i in range(1, 5)],
                }
            ],
        }
    )
    return event_id, "G-A-1-1"


async def test_result_api_stores_distance_and_ranks_by_it(admin_client):
    """录分时带上 ``extras``（距离）：没跑完的人按距离排名，数据库里也真的存住了。"""
    previous = store.current_id
    event_id, code = await _fresh_event("距离用例届")
    try:
        res = await admin_client.post(
            f"/api/rounds/{code}/result",
            json={
                "sides": [
                    {"key": "A", "score": 83450},                                 # 1:23.450 完赛
                    {"key": "B", "score": metrics.MISSING, "extras": [800]},       # 没跑完
                    {"key": "C", "score": metrics.MISSING, "extras": [400]},
                    {"key": "D", "score": metrics.MISSING, "extras": [1200]},      # 跑得最远
                ]
            },
        )
        assert res.status_code == 200, res.text

        cfg = await store.read_event(event_id)
        rnd = cfg.rounds[0]
        assert [side.extras for side in rnd.sides] == [[], [800], [400], [1200]], (
            "距离要**原样存进数据库**（JSON 列），读回来还得是这几个数"
        )
        assert [side.rank for side in rnd.sides] == [1, 3, 4, 2], (
            "名次：完赛的第 1，其余按距离（D 1200 → B 800 → C 400）"
        )
        assert rnd.winner == "A"
    finally:
        if previous:
            await store.switch_event(previous)
        await store.delete_event(event_id)


async def test_result_api_rejects_a_negative_distance(admin_client):
    """距离是「跑了多远」：负数没有意义，多半是把主成绩填错了格子——直接报错，别悄悄存。"""
    previous = store.current_id
    event_id, code = await _fresh_event("距离负数用例届")
    try:
        res = await admin_client.post(
            f"/api/rounds/{code}/result",
            json={"sides": [{"key": "A", "score": 83450, "extras": [-5]}]},
        )
        assert res.status_code == 400, res.text
        assert "距离" in res.text
    finally:
        if previous:
            await store.switch_event(previous)
        await store.delete_event(event_id)
