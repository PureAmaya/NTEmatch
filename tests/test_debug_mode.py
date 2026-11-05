"""调试模式：演示数据、模拟机位与本机免登录的放宽。

三条都必须钉住，因为它们的失败方式都很隐蔽：

* 演示数据在**有数据**的库上被写入 = 悄悄改掉真实赛事；
* 模拟机位在**配了媒体服务器**时仍然生效 = 真实直播被假画面顶掉；
* ``force`` 把「本机不设防」开关一起绕过去 = 鉴权判定被放宽。
"""

from __future__ import annotations

from typing import ClassVar

from app import debug, logic
from app.models import Config


class _FakeStore:
    """只实现 ensure_demo 用到的那两个方法，避免碰真实的全局 store。"""

    def __init__(self, players=None) -> None:
        self.patch = None
        self._cfg = Config.model_validate({"players": players or []})

    def snapshot(self):
        return self._cfg

    async def update(self, patch, actor="api"):
        self.patch = patch
        return self._cfg


def _cfg(base_url: str = "") -> Config:
    return Config.model_validate(
        {
            "players": [{"id": "p1", "name": "甲", "streamKey": "s1"}],
            "stream": {"baseUrl": base_url},
        }
    )


# --------------------------------------------------------------------------- #
# 演示数据
# --------------------------------------------------------------------------- #
def test_demo_patch_is_playable(monkeypatch):
    """演示数据要能直接看出各页面的样子：有人、有局、有一局正在打。"""
    monkeypatch.setenv("NTE_DEBUG", "1")
    patch = debug.demo_patch()

    players = patch["players"]
    assert len(players) == 8
    # 没有流名就没有机位：直播页与导播台会一直是空的
    assert all(p.get("streamKey") for p in players)
    assert patch["participants"] == [p["id"] for p in players]
    assert patch["rules"]["format"] == "league"

    rounds = patch["rounds"]
    assert len(rounds) == 5
    statuses = [r["status"] for r in rounds]
    assert statuses[0] == "live", "导播台跟的是进行中的那一场，必须有一局在打"
    assert statuses[1] == "done" and statuses[2] == "done"
    # 已打完的两局要有比分，否则排名页是空的
    assert rounds[1]["winner"] and rounds[1]["sides"][0]["score"] > 0


async def test_ensure_demo_writes_only_into_empty_store(monkeypatch):
    monkeypatch.setenv("NTE_DEBUG", "1")
    empty = _FakeStore()
    assert await debug.ensure_demo(empty) is True
    assert empty.patch is not None and len(empty.patch["players"]) == 8

    used = _FakeStore(players=[{"id": "p1", "name": "真人"}])
    assert await debug.ensure_demo(used) is False
    assert used.patch is None, "有真实数据的库绝不能被演示数据覆盖"


async def test_ensure_demo_does_nothing_outside_debug(monkeypatch):
    monkeypatch.delenv("NTE_DEBUG", raising=False)
    store = _FakeStore()
    assert await debug.ensure_demo(store) is False
    assert store.patch is None


# --------------------------------------------------------------------------- #
# 模拟机位
# --------------------------------------------------------------------------- #
def test_mock_streams_only_without_media_server(monkeypatch):
    """配了媒体服务器就照常走真实推流：调试日志与真实直播可以同时要。"""
    monkeypatch.setenv("NTE_DEBUG", "1")
    assert logic.build_stream_map(_cfg())["p1"]["mock"] is True
    assert "mock" not in logic.build_stream_map(_cfg("https://live.test:8889"))["p1"]

    monkeypatch.delenv("NTE_DEBUG", raising=False)
    assert "mock" not in logic.build_stream_map(_cfg())["p1"]


# --------------------------------------------------------------------------- #
# 本机免登录
# --------------------------------------------------------------------------- #
def test_is_local_force_ignores_switch_but_not_loopback():
    """``force`` 只让「本机不设防」开关失效，回环 / 转发头 / Host 三条一条都不放过。"""
    from app import login_guard

    class _Client:
        host = "127.0.0.1"

    class _Request:
        client = _Client()
        headers: ClassVar[dict[str, str]] = {"host": "localhost:8000"}

    off = {"localTrust": False}
    assert login_guard.is_local(_Request(), off) is False
    assert login_guard.is_local(_Request(), off, force=True) is True

    # 非回环：force 也不能放行
    class _Remote(_Request):
        class client:
            host = "203.0.113.9"

    assert login_guard.is_local(_Remote(), off, force=True) is False

    # 带转发头（说明是代理进来的）：同样放行不了
    class _Proxied(_Request):
        headers: ClassVar[dict[str, str]] = {
            "host": "localhost:8000",
            "x-forwarded-for": "203.0.113.9",
        }

    assert login_guard.is_local(_Proxied(), off, force=True) is False
