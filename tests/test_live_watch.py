"""直播常驻探测：**没人访问也在探**，状态变了才推，且绝不拖慢任何人。

这一组的四件事都是「改回去不会报错、但会慢慢坏掉」的类型：

* **一直在探**：不常驻的话，没人看直播时缓存会过期，第一位访客看到的先是旧数据；
* **变了才推**：每 5 秒无条件广播一次，会把前端白刷一遍（正在播的画面也可能被重建）；
* **崩了继续**：媒体服务器半死不活时，超时与异常都不能把循环带走——它挂了没人能救；
* **不与请求抢活**：前端请求顺手触发的 ``kick_refresh`` 要能看出「刚探过」，不再多打一轮。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import live


class _FakeHub:
    """只记下广播内容的假 hub（真 hub 要连 WebSocket 才有意义）。"""

    def __init__(self, size: int = 0) -> None:
        self.size = size
        self.sent: list[dict[str, Any]] = []

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _view(**over: Any) -> dict[str, Any]:
    """一份最小的「直播健康视图」（形状与 ``GET /api/live/health`` 一致）。"""
    base: dict[str, Any] = {
        "ok": True,
        "pending": False,
        "streamingKnown": True,
        "streaming": [],
        "streamingChannels": [],
        "streamingMembers": [],
        "mainStreaming": False,
        "bili": {"known": True, "items": []},
    }
    base.update(over)
    return base


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.fixture(autouse=True)
def _clean_tasks(monkeypatch):
    """每条用例前后都别留下正在跑的常驻任务。"""
    monkeypatch.setattr(live, "_watch_task", None)
    monkeypatch.setattr(live, "_refresh_task", None)
    monkeypatch.setattr(live, "_last_refresh", float("-inf"))
    yield


# --------------------------------------------------------------------------- #
# 指纹与间隔（纯函数）
# --------------------------------------------------------------------------- #
def test_fingerprint_follows_who_is_live_only():
    """指纹只认「谁在播」：B站 标题变了不值得让前端重绘一遍视图。"""
    a = _view(streaming=["p1"], bili={"known": True, "items": [{"uid": "u1", "title": "上分"}]})
    b = _view(streaming=["p1"], bili={"known": True, "items": [{"uid": "u1", "title": "换了个标题"}]})
    c = _view(streaming=["p1", "p2"], bili={"known": True, "items": [{"uid": "u1"}]})
    assert live.live_fingerprint(a) == live.live_fingerprint(b)
    assert live.live_fingerprint(a) != live.live_fingerprint(c)


@pytest.mark.parametrize(
    ("view", "online", "expected"),
    [
        (_view(streaming=["p1"]), 0, "WATCH_INTERVAL"),
        (_view(mainStreaming=True), 0, "WATCH_INTERVAL"),
        (_view(streamingChannels=["c1"]), 0, "WATCH_INTERVAL"),
        (_view(bili={"known": True, "items": [{"uid": "u1"}]}), 0, "WATCH_INTERVAL"),
        (_view(), 1, "WATCH_INTERVAL"),          # 没人播但有页面开着（可能在等开播）
        (_view(), 0, "WATCH_IDLE_INTERVAL"),     # 没人播、也没人在线 → 放宽，但**不是停**
        (None, 0, "WATCH_IDLE_INTERVAL"),
    ],
)
def test_watch_delay(monkeypatch, view, online, expected):
    monkeypatch.setattr(live, "hub", _FakeHub(size=online))
    assert live.watch_delay(view) == getattr(live, expected)


# --------------------------------------------------------------------------- #
# 常驻循环
# --------------------------------------------------------------------------- #
async def test_watch_loop_probes_forever_and_pushes_on_change(monkeypatch):
    """一直在探；**状态变了才推**——同一种状态连探三轮也不该推三次。"""
    hub = _FakeHub(size=1)
    monkeypatch.setattr(live, "hub", hub)
    monkeypatch.setattr(live, "watch_delay", lambda view=None: 0.01)
    probes = {"n": 0}
    state = {"view": _view()}

    async def fake_refresh() -> None:
        probes["n"] += 1

    async def fake_health(force: bool = False) -> dict[str, Any]:
        return state["view"]

    monkeypatch.setattr(live, "_refresh_once", fake_refresh)
    monkeypatch.setattr(live, "health_view", fake_health)

    task = asyncio.create_task(live.watch_loop())
    try:
        assert await _wait_until(lambda: probes["n"] >= 3), "循环应当一直在探"
        assert await _wait_until(lambda: len(hub.sent) == 1), "状态变了才推一次"
        assert hub.sent[0]["type"] == "live"
        assert hub.sent[0]["data"] == state["view"], "推的就是 /api/live/health 那一份"
        await asyncio.sleep(0.05)
        assert len(hub.sent) == 1, "状态没变就不该再推"

        # 有人开播：集合变了 → 再推一次
        state["view"] = _view(streaming=["p1"])
        assert await _wait_until(lambda: len(hub.sent) == 2)
        assert hub.sent[1]["data"]["streaming"] == ["p1"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_watch_loop_survives_probe_errors(monkeypatch):
    """探测抛异常 → 只记日志、**很快**再来一轮（它挂了不会有第二个进程来救）。"""
    hub = _FakeHub(size=1)
    monkeypatch.setattr(live, "hub", hub)
    monkeypatch.setattr(live, "watch_delay", lambda view=None: 0.01)
    monkeypatch.setattr(live, "WATCH_RETRY", 0.01)
    probes = {"n": 0}

    async def boom() -> None:
        probes["n"] += 1
        raise RuntimeError("媒体服务器炸了")

    monkeypatch.setattr(live, "_refresh_once", boom)
    task = asyncio.create_task(live.watch_loop())
    try:
        assert await _wait_until(lambda: probes["n"] >= 3), "异常之后仍要继续探"
        assert hub.sent == [], "没拿到数据就不该推（宁可不说，也不给假的）"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_watch_loop_gives_up_on_a_hanging_probe(monkeypatch):
    """探测卡住（媒体服务器半死不活）→ 超时跳过这一轮，循环继续。"""
    hub = _FakeHub(size=1)
    monkeypatch.setattr(live, "hub", hub)
    monkeypatch.setattr(live, "watch_delay", lambda view=None: 0.01)
    monkeypatch.setattr(live, "WATCH_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(live, "WATCH_RETRY", 0.01)
    probes = {"n": 0}

    async def hang() -> None:
        probes["n"] += 1
        await asyncio.sleep(30)

    monkeypatch.setattr(live, "_refresh_once", hang)
    task = asyncio.create_task(live.watch_loop())
    try:
        assert await _wait_until(lambda: probes["n"] >= 3), "超时之后要接着探下一轮"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_start_and_stop_watcher(monkeypatch):
    """启停成对：启动幂等，关站时能被 stop_refresher 干净收走。"""
    monkeypatch.setattr(live, "hub", _FakeHub(size=1))
    started = asyncio.Event()

    async def fake_loop() -> None:
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(live, "watch_loop", fake_loop)
    live.start_watcher()
    first = live._watch_task
    assert first is not None
    live.start_watcher()
    assert live._watch_task is first, "重复启动不该起第二个任务"
    await started.wait()

    await live.stop_refresher()
    assert live._watch_task is None
    assert first.cancelled() or first.done()


async def test_stop_refresher_cancels_both_tasks(monkeypatch):
    """按需探测与常驻探测都要被收走（关连接池之前必须先停它们）。"""

    async def fake_refresh() -> None:
        await asyncio.sleep(30)

    async def fake_loop() -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(live, "_refresh_once", fake_refresh)
    monkeypatch.setattr(live, "watch_loop", fake_loop)
    monkeypatch.setattr(live, "_last_refresh", float("-inf"))
    monkeypatch.setattr(live, "_ready_cache", {"at": float("-inf"), "paths": None})

    live.start_watcher()
    live.kick_refresh()
    watch, one_shot = live._watch_task, live._refresh_task
    assert watch is not None and one_shot is not None

    await live.stop_refresher()
    assert live._watch_task is None and live._refresh_task is None
    assert (watch.done() or watch.cancelled()) and (one_shot.done() or one_shot.cancelled())


# --------------------------------------------------------------------------- #
# 与请求路径的关系：常驻跑起来后，请求别再多打一轮
# --------------------------------------------------------------------------- #
async def test_kick_refresh_skips_when_the_cache_is_fresh(monkeypatch):
    """缓存刚被常驻探测刷新过 → 请求触发的这次 kick 直接跳过，不重复探测。"""
    monkeypatch.setattr(live, "_ready_cache", {"at": time.monotonic(), "paths": set()})
    live.kick_refresh()
    assert live._refresh_task is None


async def test_kick_refresh_still_works_when_the_cache_is_old(monkeypatch):
    """缓存旧了（常驻探测还没跑 / 挂了）→ 请求仍然能催一次。"""
    monkeypatch.setattr(live, "_ready_cache", {"at": float("-inf"), "paths": None})

    async def fake_refresh() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(live, "_refresh_once", fake_refresh)
    live.kick_refresh()
    task = live._refresh_task
    assert task is not None, "缓存不新鲜时该照旧安排一次后台探测"
    await task


def test_snapshot_treats_a_stale_cache_as_unknown(monkeypatch):
    """缓存太旧（探测停了）→ 按「查不到」处理，不能把最后一次结果永久当成直播中。"""
    monkeypatch.setattr(
        live,
        "_ready_cache",
        {"at": time.monotonic() - live._SNAPSHOT_MAX_AGE - 1, "paths": {"tom"}},
    )
    assert live.ready_paths_snapshot() is None
    monkeypatch.setattr(live, "_ready_cache", {"at": time.monotonic(), "paths": {"tom"}})
    assert live.ready_paths_snapshot() == {"tom"}


# --------------------------------------------------------------------------- #
# WebSocket 回放：刚打开页面的人也该立刻知道谁在播
# --------------------------------------------------------------------------- #
class _FakeWS:
    """只实现 hub 用到的三个方法，并把收到的消息记下来。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, body: str) -> None:
        self.sent.append(json.loads(body))

    async def close(self, code: int = 0, reason: str = "") -> None:
        return None


async def test_hub_replays_the_last_live_state():
    """新连接会收到**最近一次**直播状态：不用等下一次状态变化才点亮「直播中」。"""
    from app.ws import Hub

    hub = Hub()
    await hub.broadcast({"type": "live", "data": {"streaming": ["p1"]}})
    ws = _FakeWS()
    assert await hub.connect(ws) is True
    assert [m["type"] for m in ws.sent] == ["live"]
    assert ws.sent[0]["data"] == {"streaming": ["p1"]}


async def test_hub_replays_state_before_live():
    """回放顺序：先状态（页面骨架）后直播（标记），与推送时序一致。"""
    from app.ws import Hub

    hub = Hub()
    await hub.broadcast_state({"eventId": "e001"})
    await hub.broadcast({"type": "live", "data": {"streaming": []}})
    ws = _FakeWS()
    await hub.connect(ws)
    assert [m["type"] for m in ws.sent] == ["state", "live"]


def test_watch_constants_are_sane():
    """空闲间隔只该比忙碌间隔大（写反了会变成「没人看时反而探得更勤」）。"""
    assert live.WATCH_IDLE_INTERVAL > live.WATCH_INTERVAL > 0
    assert live._SNAPSHOT_MAX_AGE >= live.WATCH_IDLE_INTERVAL, (
        "空闲间隔不能让快照过期，否则没人访问时状态会一直显示成「查不到」"
    )
