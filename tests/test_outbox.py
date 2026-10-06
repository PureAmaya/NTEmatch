"""真 @ 投递：站点排队、插件来发（AstrBot 的 OpenAPI 没有 at 段，站点自己发不出真 @）。

这一组盯「@ 到底能不能真 @」这条链路的四条底线：

* 插件在线 → 排队交给它（正文里**不带** CQ 码，@ 由插件用 ``At`` 组件发）；
* 插件不在线 / 排队超时 → 退回文本写法（``atMode``），**消息不丢**；
* 插件回执说发不出去 → **立刻**退回文本写法，不必干等超时；
* 「插件在线」这个判据会自己过期（否则插件一停，@ 就永远写进文本了）。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app import db, outbox, qqbot
from app.auth import hash_secret
from app.main import app
from app.store import store

BOT_TOKEN = "nte_test_bot_token"


async def _drain() -> None:
    """把队列清空（标记成 failed）：队列是**全局状态**，用例之间必须隔离。"""
    for item in await store.push_pending(limit=50):
        await store.push_finish(item["id"], status="failed", via="test", detail="用例清理")


@pytest.fixture(autouse=True)
async def _ready():
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    saved = dict(store.qqbot_settings())
    await store.set_qqbot(
        {
            "enabled": True,
            "baseUrl": "http://astrbot.test",
            "apiKey": "abk_test",
            "umo": "123456",
            "botApiTokenHash": hash_secret(BOT_TOKEN),
        },
        actor="test",
        internal=True,
    )
    await _drain()
    await store.set_meta(outbox.SEEN_KEY, "")
    try:
        yield
    finally:
        await _drain()
        await store.set_meta(outbox.SEEN_KEY, "")
        await store.set_qqbot(saved, actor="test", internal=True)


@pytest.fixture
def sent(monkeypatch):
    """记录站点自己发出去的文本（``send_text`` 是唯一的出站口）。"""
    calls: list[str] = []

    async def fake_text(text, *, settings=None, umo=""):
        calls.append(text)
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    monkeypatch.setattr(qqbot, "send_text", fake_text)
    return calls


@pytest.fixture
async def bot_client():
    """按插件的方式调站点（Bearer 令牌，与插件用的同一套）。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    ) as client:
        yield client


# --------------------------------------------------------------------------- #
# 走哪条路
# --------------------------------------------------------------------------- #
async def test_queues_for_the_plugin_when_it_is_online(sent):
    """插件在线：排队交给它真 @，站点一个字都不发。"""
    await outbox.mark_seen()
    result = await outbox.deliver(kind="call", body="集合啦！", mentions=["10001", "10002"])
    assert result["via"] == "plugin" and result["ok"] is True
    assert sent == [], "交给插件了就不该再由站点发一遍"

    items = await store.push_pending()
    assert len(items) == 1
    assert items[0]["mentions"] == ["10001", "10002"]
    assert items[0]["body"] == "集合啦！", "正文里不带 @ 写法（那是插件的事）"
    assert items[0]["umo"] == "aiocqhttp:GroupMessage:123456"


async def test_sends_text_when_the_plugin_is_offline(sent):
    """插件不在线：当场按文本写法发（CQ 码），不排队、不让消息等着。"""
    result = await outbox.deliver(kind="call", body="集合啦！", mentions=["10001"])
    assert result["via"] == "webhook" and result["ok"] is True
    assert sent and sent[0] == "[CQ:at,qq=10001]\n集合啦！"
    assert await store.push_pending() == [], "没有插件可等，就不该留下排队"


async def test_at_mode_none_only_lists_names(sent):
    """``atMode=none``：不 @，只发正文（设置成不 @ 时别偷偷塞 CQ 码）。"""
    await store.set_qqbot({"atMode": "none"}, actor="test", internal=True)
    await outbox.deliver(kind="call", body="集合啦！", mentions=["10001"])
    assert sent == ["集合啦！"]


async def test_plugin_liveness_expires():
    """「插件在线」会过期：插件停了不再取件，就该回到文本写法。"""
    assert await outbox.plugin_alive() is False
    await outbox.mark_seen()
    assert await outbox.plugin_alive() is True
    later = outbox._now() + timedelta(seconds=outbox.PLUGIN_TTL_SECONDS + 5)
    assert await outbox.plugin_alive(now=later) is False


# --------------------------------------------------------------------------- #
# 回执：发出去了没有
# --------------------------------------------------------------------------- #
async def test_ack_ok_marks_it_sent(sent):
    """插件说发好了 → 标记收尾，站点不再补发。"""
    await outbox.mark_seen()
    await outbox.deliver(kind="call", body="集合啦！", mentions=["10001"])
    item = (await store.push_pending())[0]

    result = await outbox.ack(item["id"], ok=True)
    assert result["ok"] is True and result["handled"] is True
    assert sent == []
    assert await store.push_pending() == []
    done = await store.push_item(item["id"])
    assert done is not None and done["status"] == "sent" and done["via"] == "plugin"


async def test_ack_failure_falls_back_to_text_right_away(sent):
    """插件说发不出去 → **立刻**退回文本写法（别等超时，也别让消息卡在队列里）。"""
    await outbox.mark_seen()
    await outbox.deliver(kind="call", body="集合啦！", mentions=["10001"])
    item = (await store.push_pending())[0]

    result = await outbox.ack(item["id"], ok=False, detail="这个会话发不出去")
    assert result["ok"] is True and result["via"] == "webhook"
    assert sent and sent[0].startswith("[CQ:at,qq=10001]")
    done = await store.push_item(item["id"])
    assert done is not None and done["status"] == "fallback"
    assert "插件发失败" in done["detail"]


async def test_ack_for_an_unknown_item_is_a_no_op(sent):
    """回执指向一条已经不在了的消息：别报错、更别乱发。"""
    result = await outbox.ack("n不存在", ok=False)
    assert result["ok"] is False
    assert sent == []


# --------------------------------------------------------------------------- #
# 巡检：排队超时的兜底
# --------------------------------------------------------------------------- #
async def test_tick_falls_back_when_nobody_picks_it_up(sent):
    """排了 45 秒还没人来取 → 退回文本发（消息绝不能因为「等插件」而丢）。"""
    item = await store.push_enqueue(
        kind="remind", body="开赛提醒", mentions=["10001"], umo="g:1"
    )
    assert await outbox.tick() == [], "还没到时间就别急着退回"

    later = outbox._now() + timedelta(seconds=outbox.FALLBACK_SECONDS + 5)
    done = await outbox.tick(now=later)
    assert len(done) == 1 and done[0]["ok"] is True
    assert sent and sent[0].startswith("[CQ:at,qq=10001]")
    stored = await store.push_item(item["id"])
    assert stored is not None and stored["status"] == "fallback"
    assert await store.push_pending() == [], "兜底发出去之后队列里不该还留着它"


async def test_tick_drains_the_queue_when_push_is_off(sent):
    """推送整个关着：别让它反复过期，直接收尾并把原因写清楚。"""
    item = await store.push_enqueue(
        kind="call", body="集合啦！", mentions=["10001"], umo="g:1"
    )
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)
    later = outbox._now() + timedelta(seconds=outbox.FALLBACK_SECONDS + 5)
    assert await outbox.tick(now=later) == []
    assert sent == []
    assert await store.push_pending() == []
    stored = await store.push_item(item["id"])
    assert stored is not None and stored["status"] == "failed"
    assert "未启用" in stored["detail"]


# --------------------------------------------------------------------------- #
# 插件那一侧的两个接口
# --------------------------------------------------------------------------- #
async def test_outbox_endpoints_need_the_token():
    """没有令牌：一路 401 —— 真 @ 投递能 @ 一大片人，不能任人取用。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    ) as anon:
        assert (await anon.get("/api/bot/outbox")).status_code == 401
        assert (await anon.post("/api/bot/outbox/ack", json={"id": "x"})).status_code == 401


async def test_take_and_ack_round_trip(bot_client):
    """插件取件 → 站点记「在线」；回执 → 收尾。整条路走一遍。"""
    await outbox.mark_seen()
    await outbox.deliver(kind="call", body="集合啦！", mentions=["10001", "10002"])

    taken = await bot_client.get("/api/bot/outbox")
    assert taken.status_code == 200, taken.text
    items = taken.json()["items"]
    assert len(items) == 1
    assert items[0]["mentions"] == ["10001", "10002"]

    ack = await bot_client.post(
        "/api/bot/outbox/ack", json={"id": items[0]["id"], "ok": True, "detail": ""}
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["handled"] is True
    assert (await bot_client.get("/api/bot/outbox")).json()["count"] == 0


async def test_taking_items_is_what_marks_the_plugin_alive(bot_client):
    """**取件本身就是存活信号**：空队列也要照常来取，否则站点会以为插件不在。"""
    await store.set_meta(outbox.SEEN_KEY, "")
    assert await outbox.plugin_alive() is False
    await bot_client.get("/api/bot/outbox")
    assert await outbox.plugin_alive() is True
