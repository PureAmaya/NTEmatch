"""通知 / 赛事信息 / 服务器信息 / 图片上传。

这一组盯的是「谁能发、什么时候不能改、图片真的被校验了吗」——
功能好不好用是次要的，**权限与校验**才是这类接口最容易出事的地方：

* 赛事管理员不能通过通知接口碰到别人的届；
* 服务器级通知只有服务器管理员能发（否则赛事管理员就能全站弹窗）；
* 本届结束后赛事信息只读，但通知仍然可发（这是需求里明说的一条）；
* 图片必须真的是图片（按魔数判断，不信声明的 MIME）。
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app import db, media
from app.auth import auth
from app.main import app
from app.store import store

#: 一张真实的 1×1 PNG（67 字节）——魔数校验必须能认出它
PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _client(session) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"X-NTE-Token": session.token},
    )


@pytest.fixture
async def anon_client():
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    ) as c:
        yield c


@pytest.fixture
async def server_client(admin_client):
    """服务器管理员（``admin_client`` 用的就是服务器管理员会话）。"""
    yield admin_client


@pytest.fixture
async def event_admin_client():
    """赛事管理员：uid 设成当前届的创办者。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    uid = "u_event_owner"
    prev_owner = store.snapshot().event.owner_uid
    await store.update({"event": {"ownerUid": uid}}, actor="test")
    session = auth.issue("赛事管理员", uid=uid, name="小赛", permission="event_admin")
    async with _client(session) as c:
        yield c
    await store.update({"event": {"ownerUid": prev_owner}}, actor="test")


# --------------------------------------------------------------------------- #
# 发布与读取
# --------------------------------------------------------------------------- #
async def test_event_notice_round_trip(event_admin_client, anon_client):
    """赛事通知：发布 → 列表（摘要）→ 详情（渲染后的 HTML）。"""
    res = await event_admin_client.post(
        "/api/notices",
        json={"scope": "event", "title": "第 2 轮改期", "body": "# 注意\n\n**19:30** 开始，别迟到。"},
    )
    assert res.status_code == 200, res.text
    notice = res.json()["notice"]
    assert notice["title"] == "第 2 轮改期"
    assert "<h1>注意</h1>" in notice["html"] and "<strong>19:30</strong>" in notice["html"]
    assert notice["author"] == "小赛"

    listed = await anon_client.get("/api/notices", params={"scope": "event"})
    assert listed.status_code == 200
    data = listed.json()
    assert data["items"][0]["id"] == notice["id"]
    assert "摘要" not in data["items"][0]  # 列表只给 summary，不给正文
    assert "body" not in data["items"][0]
    assert "#" not in data["items"][0]["summary"]  # 摘要已剥掉标记

    detail = await anon_client.get(f"/api/notices/{notice['id']}")
    assert detail.json()["notice"]["body"].startswith("# 注意")

    await event_admin_client.delete(f"/api/notices/{notice['id']}")


async def test_notice_list_is_paginated(server_client):
    """分页：默认每页 4 条（管理界面「超过 4 行」就翻页）。"""
    created = []
    for index in range(5):
        res = await server_client.post(
            "/api/notices", json={"scope": "event", "title": f"通知 {index}", "body": f"第 {index} 条"}
        )
        created.append(res.json()["notice"]["id"])
    first = (await server_client.get("/api/notices", params={"scope": "event", "size": 4})).json()
    assert len(first["items"]) == 4
    assert first["pages"] >= 2
    assert first["page"] == 1
    for notice_id in created:
        await server_client.delete(f"/api/notices/{notice_id}")


async def test_server_notice_needs_server_admin(event_admin_client, server_client):
    """站点级通知只有服务器管理员能发——否则赛事管理员就能给全站弹窗。"""
    denied = await event_admin_client.post(
        "/api/notices", json={"scope": "server", "title": "冒充", "body": "不该成功"}
    )
    assert denied.status_code == 403

    ok = await server_client.post(
        "/api/notices", json={"scope": "server", "title": "站点维护", "body": "今晚 23:00 停机"}
    )
    assert ok.status_code == 200
    assert ok.json()["notice"]["scope"] == "server"
    await server_client.delete(f"/api/notices/{ok.json()['notice']['id']}")


async def test_other_event_admin_cannot_touch_notice(event_admin_client):
    """别人的届：连读带写都不行（赛事管理员只能管自己创建的届）。"""
    created = await event_admin_client.post(
        "/api/notices", json={"scope": "event", "title": "我的", "body": "内容"}
    )
    notice_id = created.json()["notice"]["id"]

    outsider = auth.issue("别的赛事管理员", uid="u_someone_else", permission="event_admin")
    async with _client(outsider) as c:
        assert (await c.post("/api/notices", json={"scope": "event", "title": "x", "body": "y"})).status_code == 403
        assert (await c.put(f"/api/notices/{notice_id}", json={"title": "改", "body": "改"})).status_code == 403
        assert (await c.delete(f"/api/notices/{notice_id}")).status_code == 403

    await event_admin_client.delete(f"/api/notices/{notice_id}")


async def test_notice_requires_title_and_body(server_client):
    for payload in ({"title": "", "body": "有内容"}, {"title": "有标题", "body": "  "}):
        res = await server_client.post("/api/notices", json={"scope": "event", **payload})
        assert res.status_code == 400


async def test_latest_notice_is_the_newest_even_within_one_second(server_client, anon_client):
    """同一秒里连发多条，「最新一条」必须是最后发的那条。

    时间戳只精确到秒（``now_iso``），靠它排序会在同秒里变成随机顺序——
    列表顺序与「弹出哪一条」都会飘。排序靠的是单调递增的 ``seq``。
    """
    ids = []
    for index in range(3):
        res = await server_client.post(
            "/api/notices", json={"scope": "server", "title": f"第 {index} 条", "body": "内容"}
        )
        ids.append(res.json()["notice"]["id"])

    state = (await anon_client.get("/api/state")).json()
    assert state["notices"]["server"]["id"] == ids[-1]

    listed = (await anon_client.get("/api/notices", params={"scope": "server"})).json()
    assert [item["id"] for item in listed["items"]] == list(reversed(ids))

    for notice_id in ids:
        await server_client.delete(f"/api/notices/{notice_id}")


async def test_latest_notice_head_is_broadcast_in_state(server_client, anon_client):
    """状态里只带**最新一条的轻量信息**（弹窗据此判断要不要弹、正文按需再取）。"""
    res = await server_client.post(
        "/api/notices", json={"scope": "event", "title": "现在开始", "body": "半决赛 19:00"}
    )
    notice_id = res.json()["notice"]["id"]
    state = (await anon_client.get("/api/state")).json()
    head = state["notices"]["event"]
    assert head["id"] == notice_id
    assert head["title"] == "现在开始"
    assert "body" not in head and "html" not in head

    await server_client.delete(f"/api/notices/{notice_id}")
    state = (await anon_client.get("/api/state")).json()
    assert (state["notices"]["event"] or {}).get("id") != notice_id


# --------------------------------------------------------------------------- #
# 「赛后只允许发通知」
# --------------------------------------------------------------------------- #
async def test_closed_event_info_is_read_only_but_notices_still_work(server_client):
    """本届结束后：改赛事信息被拒，但发通知照常。"""
    prev_status = store.snapshot().event.status
    prev_text = store.snapshot().event.rules_text
    try:
        await store.update({"event": {"status": "closed", "rulesText": "旧说明"}}, actor="test")

        blocked = await server_client.put("/api/config", json={"event": {"rulesText": "新说明"}})
        assert blocked.status_code == 400
        assert "已结束" in blocked.text

        # 原值照旧可以带上来（保存别的字段时前端会带上它，不能因此报错）
        same = await server_client.put("/api/config", json={"event": {"rulesText": "旧说明"}})
        assert same.status_code == 200

        # 通知不受影响
        notice = await server_client.post(
            "/api/notices", json={"scope": "event", "title": "赛后通告", "body": "结果已公示"}
        )
        assert notice.status_code == 200
        await server_client.delete(f"/api/notices/{notice.json()['notice']['id']}")

        info = (await server_client.get("/api/event/info")).json()
        assert info["closed"] is True and info["editable"] is False
    finally:
        await store.update(
            {"event": {"status": prev_status, "rulesText": prev_text}}, actor="test"
        )


async def test_event_info_is_markdown_rendered(server_client, anon_client):
    """赛事信息（Markdown）会渲染进规则面板下发的状态里。"""
    prev = store.snapshot().event.rules_text
    try:
        await server_client.put(
            "/api/config", json={"event": {"rulesText": "## 参赛须知\n\n- 自带设备\n- 提前 10 分钟到"}}
        )
        state = (await anon_client.get("/api/state")).json()
        assert "<h2>参赛须知</h2>" in state["rulebook"]["noteHtml"]
        assert "<li>自带设备</li>" in state["rulebook"]["noteHtml"]
    finally:
        await server_client.put("/api/config", json={"event": {"rulesText": prev}})


# --------------------------------------------------------------------------- #
# 服务器信息与 Markdown 预览
# --------------------------------------------------------------------------- #
async def test_server_info_write_needs_server_admin(event_admin_client, server_client, anon_client):
    assert (await event_admin_client.put("/api/server/info", json={"text": "# 我发的"})).status_code == 403

    saved = await server_client.put("/api/server/info", json={"text": "# 关于本站\n\n欢迎。"})
    assert saved.status_code == 200
    assert "<h1>关于本站</h1>" in saved.json()["html"]

    public = (await anon_client.get("/api/server/info")).json()
    assert public["text"].startswith("# 关于本站")

    await server_client.put("/api/server/info", json={"text": ""})


async def test_markdown_preview_needs_login(anon_client, server_client):
    assert (await anon_client.post("/api/md/preview", json={"text": "**x**"})).status_code == 401
    res = await server_client.post("/api/md/preview", json={"text": "**x**"})
    assert res.json()["html"] == "<p><strong>x</strong></p>"


# --------------------------------------------------------------------------- #
# 图片上传
# --------------------------------------------------------------------------- #
async def test_image_upload_and_immutable_cache(server_client, anon_client):
    res = await server_client.post("/api/media", json={"data_url": PNG_1PX})
    assert res.status_code == 200, res.text
    url = res.json()["url"]
    assert url.startswith("/api/media/") and url.endswith(".png")

    served = await anon_client.get(url)
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    # 内容寻址 → 可以发一年期 immutable（源图换了就是新文件名）
    assert "immutable" in served.headers["cache-control"]
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_image_upload_is_deduplicated(server_client):
    """同一张图重复上传只有一个文件（文件名就是内容哈希）。"""
    first = (await server_client.post("/api/media", json={"data_url": PNG_1PX})).json()
    second = (await server_client.post("/api/media", json={"data_url": PNG_1PX})).json()
    assert first["url"] == second["url"]


async def test_non_image_is_rejected(server_client):
    """声明成 PNG 的一段文本不能落地——否则配合内容嗅探就是存储型 XSS。"""
    fake = "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode()
    res = await server_client.post("/api/media", json={"data_url": fake})
    assert res.status_code == 400
    assert "图片" in res.text


async def test_svg_is_rejected(server_client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    payload = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    assert (await server_client.post("/api/media", json={"data_url": payload})).status_code == 400


async def test_media_upload_needs_login(anon_client):
    assert (await anon_client.post("/api/media", json={"data_url": PNG_1PX})).status_code == 401


def test_media_resolve_rejects_traversal():
    """文件名白名单：``../`` 之类的一律解析不到。"""
    assert media.resolve("../config/nte.sqlite3") is None
    assert media.resolve("a.png") is None
    assert media.resolve("") is None
