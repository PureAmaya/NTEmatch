"""通知顺带推群：**发布不受影响**，推不出去也要说清原因。

三条底线（都是「不报错但会让人误判」的类型）：

* 群里发不出去时**通知本身照旧发布成功**——若把它当成失败，管理员会重发一遍，
  站点里就多一条重复通知；
* 推的是**摘要 + 站点链接**，不是两万字全文（那会把群刷成一屏）；
* 被限流时把「还要等几秒」一起回给前端，而不是让人瞎试。
"""

from __future__ import annotations

import httpx
import pytest

from app import db, qqbot
from app.main import app
from app.store import store

ADMIN_QQ = "10001"


async def _client() -> httpx.AsyncClient:
    """带服务器管理员会话的客户端（与 conftest 的 admin_client 同源，这里要自己控生命周期）。"""
    from app.auth import auth

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    session = auth.issue("test-admin")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://nte.test",
        headers={"X-NTE-Token": session.token},
    )


@pytest.fixture(autouse=True)
async def _clean_notices():
    """用例结束后删掉自己发的通知。

    通知是**共享的测试库**里的数据，而 ``test_notices.py`` 有一条用例断言
    「服务器通知列表恰好是我刚发的那几条」——留下垃圾会让它红，而且看起来像是它的毛病。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    async def server_ids() -> set[str]:
        listed = await store.list_notices("server", size=50)
        return {row["id"] for row in listed["items"]}

    before = await server_ids()
    yield
    for notice_id in (await server_ids()) - before:
        await store.delete_notice(notice_id)


@pytest.fixture
async def bot_settings():
    """把推送配成「就绪」状态（启用 + Key + 目标会话），用完恢复。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    await store.set_qqbot(
        {
            "enabled": True,
            "baseUrl": "http://astrbot.test",
            "apiKey": "abk_test",
            "umo": "aiocqhttp:GroupMessage:123456",
        },
        actor="test",
        internal=True,
    )
    yield
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)


@pytest.fixture
def pushed(monkeypatch):
    """把「真的发消息」换成记录器，并让限流永远放行。"""
    calls: list[list[str]] = []

    async def fake_parts(parts, *, settings, umo=""):
        calls.append(list(parts))
        return {"ok": True, "status": 200, "detail": "", "sent": len(parts), "total": len(parts), "umo": umo}

    async def allow(settings=None):
        return True, "", 0

    monkeypatch.setattr(qqbot, "send_parts", fake_parts)
    monkeypatch.setattr(qqbot.limiter, "acquire", allow)
    return calls


async def test_notice_push_sends_a_summary_with_a_link(bot_settings, pushed):
    """勾了「同时发到群」：群里收到标题 + 摘要 + 站点链接，且 Markdown 记号被去掉。"""
    async with await _client() as c:
        res = await c.post(
            "/api/notices",
            json={
                "scope": "server",
                "title": "**改期**通知",
                "body": "第 2 轮改到 19:30。\n\n**请提前十分钟到场**",
                "push": True,
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["notice"]["title"] == "**改期**通知"  # 站内标题保持原文（网页会渲染 Markdown）
    assert body["push"]["ok"] is True
    assert len(pushed) == 1
    text = pushed[0][0]
    assert "【NTE 比赛 · 服务器通知】改期通知" in text, "推群的是纯文本标题"
    assert "请提前十分钟到场" in text
    assert "**" not in text, "群消息是纯文本，星号只会显得莫名其妙"
    assert "http://nte.test" in text, "要给一条回站点看完整内容的链接"


async def test_event_notice_push_links_to_that_event(bot_settings, pushed):
    """赛事通知的链接要指到**那一届**，而不是站点首页。"""
    async with await _client() as c:
        res = await c.post(
            "/api/notices",
            json={"scope": "event", "eventId": store.current_id, "title": "集合", "body": "开打", "push": True},
        )
    assert res.status_code == 200, res.text
    assert f"http://nte.test/{store.current_id}" in pushed[0][0]


async def test_push_is_optional(admin_client, bot_settings, pushed):
    """没勾就不推：通知照旧发布，一次消息都不发。"""
    res = await admin_client.post(
        "/api/notices", json={"scope": "server", "title": "只是公告", "body": "站内看看就好"}
    )
    assert res.status_code == 200, res.text
    assert "push" not in res.json(), "没勾就不该有推送结果"
    assert pushed == []


async def test_push_failure_still_publishes_the_notice(admin_client, pushed):
    """推送没配好：通知**已经发布成功**，只回一句原因（不能被当成发布失败）。"""
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)
    res = await admin_client.post(
        "/api/notices",
        json={"scope": "server", "title": "重要", "body": "正文", "push": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["notice"]["title"] == "重要"
    assert body["push"]["ok"] is False
    assert "未启用群推送" in body["push"]["detail"]
    assert pushed == []


async def test_push_reports_rate_limit(admin_client, monkeypatch):
    """被限流：把「还要等几秒」一起回给前端，人不至于瞎试。"""

    async def deny(settings=None):
        return False, "距上次推送不足 20 秒", 12

    monkeypatch.setattr(qqbot.limiter, "acquire", deny)
    await store.set_qqbot(
        {"enabled": True, "apiKey": "abk_test", "umo": "123456"}, actor="test", internal=True
    )
    res = await admin_client.post(
        "/api/notices",
        json={"scope": "server", "title": "限流", "body": "正文", "push": True},
    )
    assert res.status_code == 200, res.text
    out = res.json()["push"]
    assert out["ok"] is False and out["retryAfter"] == 12
    assert "不足 20 秒" in out["detail"]
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)


async def test_update_can_push_too(admin_client, bot_settings, pushed):
    """改完通知也能勾着推一次（改期 / 改时间之后往往正是要再喊一次的时候）。"""
    created = (
        await admin_client.post(
            "/api/notices", json={"scope": "server", "title": "初版", "body": "第一版"}
        )
    ).json()
    notice_id = created["notice"]["id"]
    res = await admin_client.put(
        f"/api/notices/{notice_id}",
        json={"scope": "server", "title": "改好了", "body": "第二版", "push": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["push"]["ok"] is True
    assert pushed and "改好了" in pushed[0][0]


async def test_long_notice_is_truncated_in_the_group(admin_client, bot_settings, pushed):
    """两万字的通知不会整篇推进群里：只带一段摘要，其余回站点看。"""
    body = "这是一句会被反复引用的说明。" * 400
    assert len(body) > 4000
    res = await admin_client.post(
        "/api/notices", json={"scope": "server", "title": "很长的通知", "body": body, "push": True}
    )
    assert res.status_code == 200, res.text
    text = pushed[0][0]
    assert len(text) < 2000, f"推群的内容应当被截断，实际 {len(text)} 字"
    assert "完整内容在站点查看" in text
