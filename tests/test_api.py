"""接口层冒烟：分享卡片标签、默认分享图、操作日志的权限与过滤。

用 ``httpx.ASGITransport`` 直接跑 ASGI 应用（不启端口），所以它足够快，
可以在 CI 里当门禁用。
"""

from __future__ import annotations

import httpx

from app import db
from app.main import AuditMiddleware, app
from app.store import store


async def test_share_card_tags(client):
    """分享到群里抓的是**原始 HTML**，所以 og 标签必须在服务端就写对。"""
    html = (await client.get("/")).text
    assert 'property="og:image" content="http://nte.test/og.png"' in html
    assert 'property="og:site_name"' in html
    assert 'property="og:url" content="http://nte.test/"' in html
    assert 'property="og:image:width" content="1200"' in html
    assert 'content="summary_large_image"' in html


async def test_cache_policy_by_path_kind(client):
    """缓存策略按「路径类别」走：带版本号的可长缓存、没版本号的必须回源校验。

    这是「源文件更新后浏览器要及时更新」的地基：版本号变了就是新 URL，
    而**没带版本号的旧 URL 绝不能被浏览器自己启发式缓存住**。
    """
    from app.main import asset_version

    versioned = await client.get(f"/static/v/{asset_version()}/js/core.js")
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["cache-control"]

    plain = await client.get("/static/js/core.js")
    assert plain.status_code == 200
    assert plain.headers["cache-control"] == "no-cache"

    api = await client.get("/api/state")
    assert api.headers["cache-control"] == "no-store"
    assert api.headers["x-content-type-options"] == "nosniff"


async def test_share_card_image_is_served(client):
    """内置分享图得真的存在且是 PNG——否则卡片会退回纯文字。"""
    res = await client.get("/og.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(res.content) > 20_000


async def test_help_image_route_is_honest(client):
    """`/help.jpg`：放了图就回图；没放就 404 并说清放哪儿。

    帮助图是站长自己丢进 `static/` 的**可选**文件，所以两种状态都接受（不逼 CI 变红）；
    要钉的是「不许回一张空白图 / 不许 500 / 不许被 SPA 回落吞掉变成一张 HTML」。
    """
    from app.main import HELP_IMAGE_NAMES

    assert HELP_IMAGE_NAMES[0] == "help.jpg"

    res = await client.get("/help.jpg")
    if res.status_code == 404:
        # 本站的异常处理器回的是 {"ok": false, "error": ...}（不是 FastAPI 默认的 detail）
        body = res.json()
        message = str(body.get("detail") or body.get("error") or "")
        assert "static/help" in message  # 说清了放哪儿，而不是干巴巴的 404
        return
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")
    assert res.headers["cache-control"] == "no-cache"  # 换图要立刻生效


async def test_help_image_route_answers_head(client):
    """`/help.jpg` 必须**支持 HEAD**：插件的「默认帮助图」就是先 HEAD 探一下在不在。

    FastAPI 的 `@app.get` 默认**不挂 HEAD**（回 405 + `allow: GET`），于是插件永远探不到图、
    帮助图会静默地一直不生效——这个坑真踩过（联调时才发现）。所以这条钉死：**不许 405**。
    """
    res = await client.request("HEAD", "/help.jpg")
    assert res.status_code in (200, 404), f"HEAD 被拒了（{res.status_code}）：路由是不是又只写了 GET？"
    assert not res.content  # HEAD 不该带正文


async def test_event_path_keeps_its_own_share_card(client):
    """子路径（某一届）也要带图，并且 og:url 指向自己那一页。"""
    html = (await client.get("/e001")).text
    assert 'property="og:image"' in html
    assert "/e001" in html


async def test_activity_requires_server_admin(client):
    """操作日志只给服务器管理员看。"""
    assert (await client.get("/api/activity")).status_code == 401


async def test_activity_skips_login_and_media_server_callbacks():
    """这两类路径不能进日志：一个带密钥，一个是媒体服务器的高频回调。"""
    assert AuditMiddleware._SKIP == ("/api/auth", "/api/live/auth")


async def test_write_requests_are_logged_but_logins_are_not(client):
    """未授权的写请求也要留痕（「谁在乱动」最该看到的就是它），登录请求不记。"""
    await client.post("/api/reload")
    await client.post("/api/auth")

    rows = await store.activity(20)
    assert any(row["path"] == "/api/reload" for row in rows)
    assert not any(row["path"].startswith("/api/auth") for row in rows)

    with db.connect(store._db_path) as conn:
        conn.execute("DELETE FROM activity WHERE path LIKE '/api/%'")
        conn.commit()


async def test_failed_bot_token_attempts_are_logged(client):
    """机器人接口的鉴权失败要留痕：那是「还有实例在用旧令牌」的唯一线索。

    只读接口平时不记（量大又没价值），所以这一条是**刻意**的例外。
    """
    res = await client.get("/api/bot/ping")
    assert res.status_code in (401, 403)

    rows = await store.activity(10)
    assert any(row["path"] == "/api/bot/ping" for row in rows)

    with db.connect(store._db_path) as conn:
        conn.execute("DELETE FROM activity WHERE path LIKE '/api/bot/%'")
        conn.commit()


async def test_public_reads_are_not_logged(client):
    """普通只读接口不进日志，否则日志会被轮询冲干净。"""
    await client.get("/api/health")
    rows = await store.activity(10)
    assert not any(row["path"] == "/api/health" for row in rows)


async def test_bot_managers_lists_only_managers():
    """召集名单：本届举办者 + 服务器管理员（按 QQ 认人），普通成员不在里面。

    这是「谁能召集」的后端依据——名单错了要么谁都召不了，要么谁都能召集。
    """
    from app.auth import hash_secret
    from app.models import Member

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    token = "nte_test_managers_token"
    prev_current = store.current_id
    await store.set_qqbot({"botApiTokenHash": hash_secret(token)}, actor="test", internal=True)
    await store.save_member(
        Member(uid="u_mgr_test", name="队长", qq="10001", permission="event_admin")
    )
    await store.save_member(Member(uid="u_other_test", name="路人", qq="10002", permission="member"))
    await store.create_event("召集名单测试届", owner_uid="u_mgr_test")
    created = store.current_id
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            res = await c.get(
                "/api/bot/managers", headers={"Authorization": f"Bearer {token}"}
            )
        assert res.status_code == 200
        body = res.json()
        assert "10001" in body["qqs"], "本届举办者必须在名单里"
        assert "10002" not in body["qqs"], "与本届无关的普通成员不该在名单里"

        # 未带令牌 => 拒绝（这是只读接口，但同样要令牌）
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            assert (await c.get("/api/bot/managers")).status_code in (401, 403)
    finally:
        await store.switch_event(prev_current)
        await store.delete_event(created)
        await store.delete_member("u_mgr_test")
        await store.delete_member("u_other_test")
        await store.set_qqbot({"botApiTokenHash": ""}, actor="test", internal=True)


async def test_bot_token_is_accepted_from_headers_only():
    """机器人令牌只认请求头：``?token=`` 即使完全正确也必须被拒。

    理由是这个令牌**不会过期**，而查询串会被反向代理 / CDN 的访问日志原样记下来，
    漏一次就是长期只读权限（能读到参与名单里的 QQ）。插件发的是 ``Authorization``
    头，所以这把「只能走头」钉在测试里，免得日后有人图方便又加回去。
    """
    from app.auth import hash_secret

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    token = "nte_test_header_only_token"
    await store.set_qqbot({"botApiTokenHash": hash_secret(token)}, actor="test", internal=True)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            head = await c.get("/api/bot/ping", headers={"Authorization": f"Bearer {token}"})
            assert head.status_code == 200
            alt = await c.get("/api/bot/ping", headers={"X-NTE-Token": token})
            assert alt.status_code == 200
            # 令牌一字不差地放在查询串里也要被挡——否则「禁止」只是嘴上说说
            query = await c.get("/api/bot/ping", params={"token": token})
            assert query.status_code == 401
    finally:
        await store.set_qqbot({"botApiTokenHash": ""}, actor="test", internal=True)
