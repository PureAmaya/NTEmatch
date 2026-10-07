"""接口层冒烟：分享卡片标签、默认分享图、操作日志的权限与过滤。

用 ``httpx.ASGITransport`` 直接跑 ASGI 应用（不启端口），所以它足够快，
提交前跑一遍就能当门禁用。
"""

from __future__ import annotations

import httpx

from app import db
from app.main import AuditMiddleware, app
from app.store import store


# --------------------------------------------------------------------------- #
# 帮助图：发之前要自查「这张图是不是比出图代码旧」
# --------------------------------------------------------------------------- #
async def test_help_image_is_regenerated_before_serving(client):
    """``/help.jpg`` 发出去之前，旧图要**当场重画**（不能等重启）。

    踩过的坑：帮助图的排版修好了，可运维的常态是**代码更新了、进程没换**——启动那一刻
    的重画没发生，``/help.jpg`` 一直挂着旧排版，用户在群里看到的还是压在一起的那张，
    只会觉得「修了没用」。所以闸门放在**发这张图的时候**。
    """
    import pytest

    from app import helpcard

    if not helpcard.available():
        pytest.skip("没装 Pillow：帮助图本来就不生成（插件退回文字说明）")
    target = helpcard.default_path()
    stamp = helpcard.stamp_path(target)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("0" * 64 + "\n", encoding="utf-8")  # 伪造成「旧图」

    res = await client.get("/help.jpg")
    assert res.status_code == 200, res.text
    assert target.exists(), "发过这张图之后本地就该有一份"
    assert stamp.read_text(encoding="utf-8").strip() == helpcard.source_digest(), (
        "发图之前应当重画并更新指纹"
    )


# --------------------------------------------------------------------------- #
# 删除组队（`POST /api/teams/clear`）
# --------------------------------------------------------------------------- #
async def test_clearing_teams_keeps_the_roster_and_drops_the_schedule(admin_client):
    """「删除组队」只清队伍与赛程：**名单 / 选手档案 / 参与状态都不动**。

    这条是这次特意加的能力：组队之后还想让人自助报名、或者想把队伍重排一遍，
    以前只能一支支删分组；现在一键清空，而且**不会把名单一起清掉**（那是「删届」）。
    赛程必须跟着清：每场对阵都引用 ``teamId``，队伍没了，旧对阵就是一堆空席位。
    """
    from app import db
    from app.store import store

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("删除组队用例届")
    mine = store.current_id
    try:
        await store.update(
            {
                "players": [{"id": "p01", "name": "甲"}, {"id": "p02", "name": "乙"}],
                "participants": ["p01", "p02"],
                "participantsSet": True,
                "teams": [
                    {"id": "t1", "name": "甲队", "playerIds": ["p01"]},
                    {"id": "t2", "name": "乙队", "playerIds": ["p02"]},
                ],
                "rounds": [
                    {
                        "index": 1,
                        "code": "G-A-1-1",
                        "stage": "group",
                        "label": "A 组 · 第 1 场",
                        "sides": [{"teamId": "t1"}, {"teamId": "t2"}],
                    }
                ],
            }
        )
        res = await admin_client.post("/api/teams/clear")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["removed"] == 2 and body["count"] == 0

        cfg = await store.read_event(mine)
        assert cfg.teams == [], "队伍应当被清空"
        assert cfg.rounds == [], "赛程必须一起清（对阵引用着队伍 id）"
        assert [p.id for p in cfg.players] == ["p01", "p02"], "选手档案不许动"
        assert cfg.participants == ["p01", "p02"], "参与名单不许动"
        assert cfg.participants_set is True, "「名单是显式指定过的」这个标记也要留着"
    finally:
        if previous:
            await store.switch_event(previous)
        await store.delete_event(mine)


# --------------------------------------------------------------------------- #
# 赛程预览接口（「快速创建分组」弹窗的实时预览）
# --------------------------------------------------------------------------- #
async def test_tournament_preview_still_works(admin_client):
    """``POST /api/tournament/preview`` 必须能跑通（前端那个按钮是唯一入口）。

    这条是补的：``8ccd154`` 给 ``auto_form_teams`` 加了 ``allow_substitutes``，
    而 ``611762d`` 把参数删掉时**没删调用点**——预览接口从那以后一直在
    ``TypeError: auto_form_teams() got an unexpected keyword argument``，
    而**没有任何测试碰过它**：参数活着、调用点没跟着改，只有「真的调一次」才发现。
    """
    res = await admin_client.post("/api/tournament/preview", json={"teamSize": 2})
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body.get("ok"), bool), body
    for key in ("players", "teams", "teamSize", "groupCount", "groupSizes", "total", "warnings"):
        assert key in body, f"预览结果少了 {key}"


# --------------------------------------------------------------------------- #
# 小组赛对阵调整：跨组换队
# --------------------------------------------------------------------------- #
def _pairing_fixture() -> dict:
    """两份小组、各两支队、各一场的小组赛数据（跨组换队用）。"""
    return {
        "teams": [
            {"id": "t1", "name": "甲", "group": "A"},
            {"id": "t2", "name": "乙", "group": "A"},
            {"id": "t3", "name": "丙", "group": "B"},
            {"id": "t4", "name": "丁", "group": "B"},
        ],
        "rounds": [
            {
                "code": "G-A-1-1",
                "stage": "group",
                "bracketRound": 1,
                "slot": 1,
                "label": "A 组 · 第 1 轮 · 第 1 场",
                "sides": [{"teamId": "t1"}, {"teamId": "t2"}],
            },
            {
                "code": "G-B-1-1",
                "stage": "group",
                "bracketRound": 1,
                "slot": 1,
                "label": "B 组 · 第 1 轮 · 第 1 场",
                "sides": [{"teamId": "t3"}, {"teamId": "t4"}],
            },
        ],
    }


def test_cross_group_pairing_swaps_the_team_groups():
    """跨组换队：两支队**整队互换**后，队伍的 ``group`` 跟着对阵一起改。

    这是「跨组调整后其他界面也应当同步」的落点：分组名单 / 选手页 / 队伍列表都读 ``group``，
    不同步就还是老的组。以前整段逻辑写在闭包里，跨组直接 400（只能同组同轮）。
    """
    from app.main import GroupPairingChange, _apply_group_pairings

    data = _pairing_fixture()
    _apply_group_pairings(
        data,
        [
            GroupPairingChange(code="G-A-1-1", team_ids=["t3", "t2"]),
            GroupPairingChange(code="G-B-1-1", team_ids=["t1", "t4"]),
        ],
    )
    assert {t["id"]: t["group"] for t in data["teams"]} == {
        "t1": "B",
        "t2": "A",
        "t3": "A",
        "t4": "B",
    }
    assert [s["teamId"] for s in data["rounds"][0]["sides"]] == ["t3", "t2"]


def test_half_crossed_team_is_rejected():
    """只换一半（同一支队挂在两个组上）必须报错，而不是悄悄把名次算歪。"""
    import pytest
    from fastapi import HTTPException

    from app.main import GroupPairingChange, _apply_group_pairings

    data = _pairing_fixture()
    with pytest.raises(HTTPException) as exc:
        _apply_group_pairings(data, [GroupPairingChange(code="G-A-1-1", team_ids=["t3", "t2"])])
    assert "同时排进了" in str(exc.value.detail)


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


async def test_bot_managers_follow_event_creator():
    """召集权限只有一条：**谁创建的届，谁可以召集**（服务器管理员另有全局权限）。

    这才是「赛事管理员也能召集自己那届」的准确含义——赛事管理员能召集的是
    *他自己创建*的届；服务器管理员建的届，他召集不了。另外带 ``qq`` 时会回
    ``mine``（他自己创建的届），插件据此把「你该敲哪条命令」直接告诉他。
    """
    from app.auth import hash_secret
    from app.models import Member

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    token = "nte_test_managers_owner_token"
    prev_current = store.current_id
    await store.set_qqbot({"botApiTokenHash": hash_secret(token)}, actor="test", internal=True)
    await store.save_member(
        Member(uid="u_owner_test", name="小队长", qq="20001", permission="event_admin")
    )
    await store.save_member(Member(uid="u_root_test", name="服管", qq="20002", permission="server_admin"))
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    own = ""
    theirs = ""
    try:
        await store.create_event("小队长自己的届", owner_uid="u_owner_test")
        own = store.current_id
        await store.create_event("服管建的届", owner_uid="u_root_test")
        theirs = store.current_id
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            mine = (
                await c.get(
                    "/api/bot/managers", params={"eventId": own, "qq": "20001"}, headers=headers
                )
            ).json()
            other = (
                await c.get(
                    "/api/bot/managers", params={"eventId": theirs, "qq": "20001"}, headers=headers
                )
            ).json()

        # ① 自己创建的届：创建者本人能召集（服务器管理员的全局权限也在名单里）
        assert "20001" in mine["qqs"], "届的创建者必须能召集自己那届"
        assert mine["owner"] == {"name": "小队长", "hasQq": True}
        assert [m["id"] for m in mine["mine"]] == [own]
        # ② 服务器管理员建的届：赛事管理员不在名单里（谁创建的谁召集）
        assert "20001" not in other["qqs"], "服务器管理员建的届，赛事管理员不该能召集"
        assert "20002" in other["qqs"], "服务器管理员能召集自己建的届"
        assert other["owner"]["name"] == "服管"
        # ③ mine 只跟「问的人」有关：换一届看，他自己那届依然列在里面
        assert [m["id"] for m in other["mine"]] == [own]
    finally:
        await store.switch_event(prev_current)
        for eid in (own, theirs):
            if eid and eid != prev_current:
                await store.delete_event(eid)
        await store.delete_member("u_owner_test")
        await store.delete_member("u_root_test")
        await store.set_qqbot({"botApiTokenHash": ""}, actor="test", internal=True)


async def test_bot_ids_scope_mine_filters_by_creator():
    """「比赛届次 我的」在**站点侧**过滤：认人靠 QQ（ownerUid → 成员 → qq）。

    过滤放这里而不是插件：插件拿不到 uid 与成员表，只能靠站点认人。另外
    ``scope=mine`` 不认人时直接 **400**——宁可报清楚，也别悄悄回个空列表，
    那会让人以为「我一届都没建过」。
    """
    from app.auth import hash_secret
    from app.models import Member

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    token = "nte_test_ids_mine_token"
    prev_current = store.current_id
    await store.set_qqbot({"botApiTokenHash": hash_secret(token)}, actor="test", internal=True)
    await store.save_member(
        Member(uid="u_ids_owner", name="小办", qq="30001", permission="event_admin")
    )
    await store.save_member(Member(uid="u_ids_root", name="服管", qq="30002", permission="server_admin"))
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    mine_id = ""
    other_id = ""
    try:
        await store.create_event("小办自己的届", owner_uid="u_ids_owner")
        mine_id = store.current_id
        await store.create_event("服管建的届", owner_uid="u_ids_root")
        other_id = store.current_id
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            mine = (
                await c.get(
                    "/api/bot/query",
                    params={"kind": "ids", "scope": "mine", "qq": "30001"},
                    headers=headers,
                )
            ).json()
            # kind=ids 是全局信息：不写届次也照样能查（这里刻意不带 eventId）
            every = (
                await c.get("/api/bot/query", params={"kind": "ids"}, headers=headers)
            ).json()
            no_qq = await c.get(
                "/api/bot/query", params={"kind": "ids", "scope": "mine"}, headers=headers
            )

        assert mine["ok"] is True
        assert "你创建的届" in mine["text"]
        assert "小办自己的届" in mine["text"]
        assert "服管建的届" not in mine["text"], "别人的届不该出现在「我的」里"
        assert "小办自己的届" in every["text"] and "服管建的届" in every["text"]
        assert no_qq.status_code == 400
        assert "qq" in no_qq.text
    finally:
        await store.switch_event(prev_current)
        for eid in (mine_id, other_id):
            if eid and eid != prev_current:
                await store.delete_event(eid)
        await store.delete_member("u_ids_owner")
        await store.delete_member("u_ids_root")
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


async def test_api_password_never_reaches_the_browser(admin_client):
    """直播的 API 密码像别的凭据一样：**明文只留服务端**，接口只回布尔。

    以前 ``/api/private`` 直接下发 ``stream.dump()``——而**赛事管理员**就能读这个接口，
    等于把媒体服务器的控制密码交给了每一位赛事管理员（表单还会把它显示在密码框里）。
    """
    saved = store.snapshot().stream.dump()
    try:
        await store.update({"stream": {**saved, "apiPass": "s3cret-live"}})
        for path in ("/api/private", "/api/server/config"):
            body = (await admin_client.get(path)).text
            assert "s3cret-live" not in body, f"{path} 把 API 密码明文发出去了"
            assert '"apiPass"' not in body, f"{path} 还在下发 apiPass 这个键"
        private = (await admin_client.get("/api/private")).json()
        assert private["stream"]["hasApiPass"] is True  # 表单据此显示「已配置（留空 = 不改）」
    finally:
        await store.update({"stream": saved})


async def test_partial_stream_patch_keeps_the_rest(admin_client):
    """直播配置是**局部补丁**：改一个键不能把地址与凭据一起冲成默认值。

    它们存在同一行里，一次整份写就会互相覆盖——所以「补丁是局部的」值得钉一条。
    """
    saved = store.snapshot().stream.dump()
    try:
        await store.update(
            {
                "stream": {
                    **saved,
                    "baseUrl": "https://live.test:8889",
                    "hlsBase": "https://live.test:8888",
                    "pushToken": "tk-live",
                }
            }
        )
        res = await admin_client.put("/api/config", json={"stream": {"title": "场馆直播"}})
        assert res.status_code == 200
        stream = (await admin_client.get("/api/server/config")).json()["stream"]
        assert stream["title"] == "场馆直播"
        assert stream["baseUrl"] == "https://live.test:8889"
        assert stream["hlsBase"] == "https://live.test:8888"
        assert stream["pushToken"] == "tk-live"
    finally:
        await store.update({"stream": saved})
