"""路由耗时观测的统计口径与中间件接线。

这些数会直接出现在管理端诊断面板上，所以「怎么算」必须钉住：
计数是累计、样本是最近 ``_SLOTS`` 次、按路由模板归并而不是实际路径。
"""

from __future__ import annotations

from app import perf


def test_snapshot_statistics():
    perf.reset()
    for elapsed in (1.0, 3.0, 2.0):
        perf.record("/api/x", elapsed)
    row = perf.snapshot()[0]
    assert row["route"] == "/api/x"
    assert row["calls"] == 3
    assert row["avg"] == 2.0
    assert row["p50"] == 2.0
    assert row["max"] == 3.0


def test_ring_buffer_is_bounded_but_counter_is_cumulative():
    perf.reset()
    total = perf._SLOTS + 50
    for index in range(total):
        perf.record("/api/x", float(index))
    row = perf.snapshot()[0]
    # 计数一直累加，样本只留最近 _SLOTS 次：所以 p50 已经不是 0
    assert row["calls"] == total
    assert row["max"] == float(total - 1)
    assert row["p50"] >= float(total - perf._SLOTS)


def test_snapshot_sorted_by_peak_and_limited():
    perf.reset()
    perf.record("/api/fast", 1.0)
    perf.record("/api/slow", 90.0)
    rows = perf.snapshot(limit=1)
    assert len(rows) == 1
    assert rows[0]["route"] == "/api/slow"


async def test_middleware_records_route_template(client):
    """走一遍真实请求，确认 scope 里拿得到路由模板（而不是带参数的实际路径）。"""
    perf.reset()
    res = await client.get("/api/health")
    assert res.status_code == 200
    routes = [row["route"] for row in perf.snapshot()]
    assert "/api/health" in routes, f"没有记录到这个接口：{routes}"
