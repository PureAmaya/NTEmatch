"""路由级耗时观测：只为回答「还有哪里慢」，不参与任何业务判断。

与 :mod:`app.metrics`（计分口径）无关，名字容易混，这里只管性能。

设计取舍：

* **定长环形缓冲**：每个接口只留最近 ``_SLOTS`` 次耗时，内存上界固定，
  不会因为跑久了就把进程撑大；统计值（p50/p95/峰值）按需现算。
* **按路由模板归并**：用 ``scope["route"].path_format``（例如
  ``/api/round/{code}``）而不是实际路径，否则每场比赛都会长出一个新键。
* **只统计 ``/api/``**：静态资源与 WebSocket 不进样本，那里没有可优化的业务逻辑。
* **不加锁**：写入全部发生在事件循环线程（ASGI 应用本身就在这一条线程上跑），
  读快照同样由事件循环调用，不存在并发写。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

# 每个接口保留的样本数：256 次足够覆盖一次赛事高峰，又能让内存恒定。
_SLOTS = 256

_samples: dict[str, deque[float]] = {}
_totals: dict[str, dict[str, float]] = {}


def record(route: str, elapsed_ms: float) -> None:
    """记一次调用耗时。``route`` 应是路由模板而非实际路径。"""
    slot = _samples.get(route)
    if slot is None:
        slot = _samples[route] = deque(maxlen=_SLOTS)
        _totals[route] = {"calls": 0.0, "sum": 0.0, "max": 0.0}
    slot.append(elapsed_ms)
    total = _totals[route]
    total["calls"] += 1
    total["sum"] += elapsed_ms
    total["max"] = max(total["max"], elapsed_ms)


def snapshot(limit: int = 12) -> list[dict[str, Any]]:
    """按峰值降序给出最慢的若干接口。

    小样本时 p95 没有统计意义（3 次里取第 3 次），所以同时给出 ``calls``，
    让人自己判断该不该信这个数。
    """
    rows: list[dict[str, Any]] = []
    for route, slot in _samples.items():
        values = sorted(slot)
        count = len(values)
        total = _totals[route]
        calls = int(total["calls"])
        rows.append(
            {
                "route": route,
                "calls": calls,
                "avg": round(total["sum"] / calls, 2) if calls else 0.0,
                "p50": round(values[count // 2], 2),
                "p95": round(values[min(count - 1, int(count * 0.95))], 2),
                "max": round(total["max"], 2),
            }
        )
    rows.sort(key=lambda row: (row["max"], row["calls"]), reverse=True)
    return rows[:limit]


def reset() -> None:
    """清空样本（测试与「重启计时」用）。"""
    _samples.clear()
    _totals.clear()


class TimingMiddleware:
    """纯 ASGI 中间件：只加两次 ``perf_counter``，不包装响应体。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            route = scope.get("route")
            # Starlette 在路由匹配后把 Route 放进 scope；匹配失败（404）时退回实际路径
            name = getattr(route, "path_format", None) or getattr(route, "path", None)
            record(str(name or scope.get("path", "")), elapsed)
