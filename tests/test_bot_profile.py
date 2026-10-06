"""QQ 机器人的自助资料：直播注册 / 游戏 UUID / 资料查看与修改。

这一组盯的是三件事（都比「功能有没有」更容易出事）：

1. **私信只发给被操作的那个人**：管理员 @ 代办也一样——令牌与资料不能落到群里，
   响应体里也不能有明文（插件拿不到，也就打不进群）；
2. **「缺什么补什么」不会顺手换掉已有的东西**：已经开着播的人再发一次
   「比赛直播注册」，令牌必须一动不动；
3. **认人仍在站点这一侧**：令牌只代表「这是本站的机器人」，谁能让机器人替谁做事
   由 QQ 决定（与 ``/credential`` 同一条闸门）。
"""

from __future__ import annotations

import re

import httpx
import pytest

from app import db, qqbot
from app.auth import hash_secret, verify_secret
from app.main import app
from app.models import Member
from app.store import store

BOT_TOKEN = "nte_test_bot_token"
ADMIN_QQ = "10001"
MEMBER_QQ = "10002"
OTHER_QQ = "10003"


def _bot() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    )


@pytest.fixture
async def sent(monkeypatch):
    """配好机器人 + 服务器管理员 QQ，并把「真的发消息」换成记录器。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    await store.set_qqbot(
        {
            "botApiTokenHash": hash_secret(BOT_TOKEN),
            "enabled": True,
            "baseUrl": "http://astrbot.test",
            "apiKey": "abk_test",
            "umo": "123456",
        },
        actor="test",
        internal=True,
    )
    admin = store.server_admin()
    assert admin is not None
    await store.save_member(admin.model_copy(update={"qq": ADMIN_QQ}))

    out: list[dict] = []

    async def fake_send(text: str, *, settings=None, umo: str = "") -> dict:
        out.append({"text": text, "umo": umo or qqbot.resolved_umo(settings or {})})
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    monkeypatch.setattr(qqbot, "send_text", fake_send)
    yield out
    await store.set_qqbot(
        {"botApiTokenHash": "", "enabled": False}, actor="test", internal=True
    )


@pytest.fixture(autouse=True)
async def _clean_members():
    """每条用例前后清掉本文件用到的几个 QQ。

    测试库是**共享**的：同一个 QQ 挂在两位成员上，``store.member_by_qq`` 会直接判为歧义
    （它只在唯一命中时返回，挑错人比查不到更危险），后面的用例就会莫名其妙地 403。
    """

    async def wipe() -> None:
        for member in list(store.members()):
            if member.qq in (MEMBER_QQ, OTHER_QQ):
                await store.delete_member(member.uid)

    await wipe()
    yield
    await wipe()


async def _member(name: str, qq: str, **update) -> Member:
    """建一位成员（可顺带覆盖字段）。"""
    saved, _key, _bearer = await store.save_member(Member(name=name, qq=qq))
    if update:
        saved = (await store.save_member(saved.model_copy(update=update)))[0]
    return saved


def _drop_bearer(qq: str) -> None:
    """把某位成员的令牌抹成空——模拟**老数据**里「令牌列是空的」那种状态。

    只能直接改内存快照：``store.save_member`` 出于安全考虑**总是**把当前哈希写回去
    （见它的 490-493 行），所以「清空令牌」这件事走不了正常保存路径。
    """
    current = store.member_by_qq(qq)
    assert current is not None
    cleared = current.model_copy(update={"bearer_hash": "", "bearer_sha256": ""})
    store._members = [cleared if m.uid == cleared.uid else m for m in store.members()]


# --------------------------------------------------------------------------- #
# 比赛直播注册
# --------------------------------------------------------------------------- #
async def test_stream_setup_gives_everything_to_a_first_timer(sent):
    """第一次注册：推流码 + 令牌 + 推流地址 + 注意事项，一次给全（私聊发本人）。"""
    await _member("甲", MEMBER_QQ)
    async with _bot() as c:
        res = (
            await c.post("/api/bot/stream-setup", json={"qq": MEMBER_QQ, "streamKey": "tom"})
        ).json()

    assert res["ok"] is True
    assert res["streamId"] == "tom"
    assert any("推流码" in item for item in res["created"])
    assert any("令牌" in item for item in res["created"])
    assert res["sent"] is True
    # 明文**不回给调用方**：令牌只在站点发出的那条私聊里
    assert "Bearer 令牌（只显示这一次" not in str(res)

    assert len(sent) == 1 and sent[0]["umo"] == f"aiocqhttp:FriendMessage:{MEMBER_QQ}"
    text = sent[0]["text"]
    assert "推流码（推流 ID）：tom" in text
    assert "推流服务器（WHIP）" in text
    assert "注意事项" in text and "B 帧" in text
    assert "比赛重置令牌" in text
    hit = re.search(r"Bearer 令牌（只显示这一次，请立即保存）：(\S+)", text)
    assert hit is not None, "私聊里应当带上新令牌"
    saved = store.member_by_qq(MEMBER_QQ)
    assert saved is not None and saved.stream_id == "tom"
    assert verify_secret(hit.group(1), saved.bearer_stored) is True


async def test_stream_setup_never_rotates_a_live_pusher_token(sent):
    """已经有推流码的人：令牌**一动不动**（他可能正开着播，换了就被顶下线）。"""
    saved = await _member("乙", OTHER_QQ, stream_id="tom")
    before = saved.bearer_stored
    async with _bot() as c:
        res = (await c.post("/api/bot/stream-setup", json={"qq": OTHER_QQ})).json()

    assert res["created"] == [], "什么都不该改"
    assert res["hasToken"] is True
    after = store.member_by_qq(OTHER_QQ)
    assert after is not None and after.bearer_stored == before
    text = sent[0]["text"]
    assert "推流码（推流 ID）：tom" in text
    assert "Bearer 令牌（只显示这一次" not in text, "不能把别人的旧令牌也「重新发一遍」"
    assert "比赛重置令牌" in text, "忘了令牌要告诉他怎么自救"


async def test_stream_setup_mints_a_token_when_the_member_has_none(sent):
    """有推流码但令牌是空的（老数据）：补一把新的，推流码不动。"""
    await _member("丙", OTHER_QQ, stream_id="tom")
    _drop_bearer(OTHER_QQ)
    async with _bot() as c:
        res = (await c.post("/api/bot/stream-setup", json={"qq": OTHER_QQ})).json()

    assert res["streamId"] == "tom"
    assert res["created"] == ["一把新的直播令牌"]
    assert "Bearer 令牌（只显示这一次" in sent[0]["text"]
    saved = store.member_by_qq(OTHER_QQ)
    assert saved is not None and saved.bearer_stored, "应当写下新令牌的哈希"


async def test_stream_setup_ignores_the_given_key_when_one_exists(sent):
    """已有推流码时，命令行里那个流名不该把现有的顶掉。"""
    await _member("丁", OTHER_QQ, stream_id="keep-me")
    async with _bot() as c:
        res = (
            await c.post("/api/bot/stream-setup", json={"qq": OTHER_QQ, "streamKey": "other"})
        ).json()
    assert res["streamId"] == "keep-me"
    saved = store.member_by_qq(OTHER_QQ)
    assert saved is not None and saved.stream_id == "keep-me"


async def test_stream_setup_admin_can_do_it_for_someone_else(sent):
    """管理员 @ 代办：私聊**仍然发给那个人**（管理员自己也看不到内容）。"""
    await _member("戊", MEMBER_QQ)
    async with _bot() as c:
        res = (
            await c.post(
                "/api/bot/stream-setup", json={"qq": ADMIN_QQ, "targetQq": MEMBER_QQ}
            )
        ).json()
    assert res["forOther"] is True
    assert res["toQq"] == MEMBER_QQ
    assert sent and sent[0]["umo"] == f"aiocqhttp:FriendMessage:{MEMBER_QQ}"
    assert "服务器管理员" in sent[0]["text"], "要说清是谁帮他注册的"


async def test_stream_setup_refuses_others_for_non_admin(sent):
    """普通成员不能替别人注册（认人在站点这一侧，绕过插件也一样）。"""
    await _member("己", MEMBER_QQ)
    await _member("庚", OTHER_QQ)
    async with _bot() as c:
        res = await c.post(
            "/api/bot/stream-setup", json={"qq": MEMBER_QQ, "targetQq": OTHER_QQ}
        )
    assert res.status_code == 403
    assert "服务器管理员" in str(res.json())
    assert store.member_by_qq(OTHER_QQ).stream_id == "", "被拒之后不该动对方的资料"


async def test_stream_setup_private_failure_keeps_the_token_out_of_the_group(sent, monkeypatch):
    """私聊发不出去：**绝不把令牌打进群**，只让他加好友后再发一次。"""

    async def fail(text: str, *, settings=None, umo: str = "") -> dict:
        return {"ok": False, "status": 400, "detail": "Bot not found", "umo": umo}

    monkeypatch.setattr(qqbot, "send_text", fail)
    await _member("辛", MEMBER_QQ)
    async with _bot() as c:
        res = (await c.post("/api/bot/stream-setup", json={"qq": MEMBER_QQ})).json()
    assert res["sent"] is False
    assert "Bearer 令牌" not in str(res), "响应里不能有明文，更不能有「令牌」的口子"
    assert store.member_by_qq(MEMBER_QQ).stream_id, "推流码已经设好了，只是没送到"


# --------------------------------------------------------------------------- #
# 游戏 UUID（群里直接回）
# --------------------------------------------------------------------------- #
async def test_uid_answers_in_group_without_any_permission(sent):
    """游戏 UUID 群内可查：不需要管理员，也不需要私聊（举办者要拿它加人）。"""
    await _member("壬", MEMBER_QQ, game_uuid="UUID-1234")
    async with _bot() as c:
        mine = (await c.get("/api/bot/uid", params={"qq": MEMBER_QQ})).json()
        other = (
            await c.get("/api/bot/uid", params={"qq": MEMBER_QQ, "targetQq": ADMIN_QQ})
        ).json()
    assert mine["known"] is True and mine["uuid"] == "UUID-1234"
    assert "UUID-1234" in mine["parts"][0]
    # 查别人的：服务器管理员的 UUID 没填 → 给的是「怎么填」，不是报错
    assert other["known"] is True
    assert "还没登记游戏 UUID" in other["parts"][0]
    assert "比赛资料 游戏UID" in other["parts"][0]


async def test_uid_for_a_stranger_explains_how_to_register(sent):
    """完全没登记的 QQ：说清「怎么变成成员」，而不是含糊地说查不到。"""
    async with _bot() as c:
        res = (await c.get("/api/bot/uid", params={"qq": "88888"})).json()
    assert res["ok"] is True and res["known"] is False
    assert "比赛添加" in res["parts"][0]


# --------------------------------------------------------------------------- #
# 资料：查看与修改
# --------------------------------------------------------------------------- #
async def test_profile_lists_every_field_and_how_to_change_it(sent):
    """资料全文：每个字段都在，而且**每一项怎么写都列出来**（用户不用记命令）。"""
    await _member("甲", MEMBER_QQ, game_uuid="U-1", bili_room="12345", stream_id="tom")
    async with _bot() as c:
        res = (await c.get("/api/bot/profile", params={"qq": MEMBER_QQ})).json()
    text = res["text"]
    for label in ("QQ：", "名字：", "游戏 UUID：", "推流码（推流 ID）：", "登录密钥：", "直播令牌："):
        assert label in text
    for how in ("比赛资料 名字", "比赛资料 游戏UID", "比赛资料 B站", "比赛资料 推流码"):
        assert how in text
    assert res["toQq"] == MEMBER_QQ
    # 只有「有没有设置」，没有明文
    assert "已设置（看不到原文，只能换新的）" in text
    assert res["parts"][0] == text


async def test_profile_edit_changes_one_field_and_returns_the_new_view(sent):
    await _member("甲", MEMBER_QQ)
    async with _bot() as c:
        res = (
            await c.post(
                "/api/bot/profile",
                json={"qq": MEMBER_QQ, "field": "名字", "value": "新名字"},
            )
        ).json()
    assert res["ok"] is True and res["note"] == "名字 → 新名字"
    assert store.member_by_qq(MEMBER_QQ).name == "新名字"
    assert "新名字" in res["text"]


async def test_profile_edit_supports_clearing_a_field(sent):
    """写「清空」就清掉那一项；空串**不算**清空（免得一个手滑把资料抹了）。"""
    await _member("甲", MEMBER_QQ, bili_room="12345")
    async with _bot() as c:
        cleared = (
            await c.post(
                "/api/bot/profile", json={"qq": MEMBER_QQ, "field": "B站", "value": "清空"}
            )
        ).json()
        empty = await c.post(
            "/api/bot/profile", json={"qq": MEMBER_QQ, "field": "B站", "value": ""}
        )
    assert cleared["ok"] is True
    assert store.member_by_qq(MEMBER_QQ).bili_room == ""
    assert empty.status_code == 400
    assert "清空" in str(empty.json())


async def test_profile_edit_guards_the_stream_key_and_qq(sent):
    """推流码要唯一、QQ 要唯一——两个直接决定「谁能推流」「谁是谁」。"""
    await _member("甲", MEMBER_QQ, stream_id="tom")
    await _member("乙", OTHER_QQ)
    async with _bot() as c:
        dup_stream = await c.post(
            "/api/bot/profile", json={"qq": OTHER_QQ, "field": "推流码", "value": "tom"}
        )
        dup_qq = await c.post(
            "/api/bot/profile", json={"qq": OTHER_QQ, "field": "QQ", "value": MEMBER_QQ}
        )
        bad = await c.post(
            "/api/bot/profile", json={"qq": OTHER_QQ, "field": "推流码", "value": "中文流名"}
        )
    assert dup_stream.status_code == 400 and "已被成员" in str(dup_stream.json())
    assert dup_qq.status_code == 400 and "已经被成员" in str(dup_qq.json())
    assert bad.status_code == 400
    saved = store.member_by_qq(OTHER_QQ)
    assert saved is not None and saved.stream_id == "" and saved.qq == OTHER_QQ


async def test_profile_unknown_field_explains_the_vocabulary(sent):
    await _member("甲", MEMBER_QQ)
    async with _bot() as c:
        res = await c.post(
            "/api/bot/profile", json={"qq": MEMBER_QQ, "field": "什么鬼", "value": "x"}
        )
    assert res.status_code == 400
    assert "名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ" in str(res.json())


async def test_profile_for_others_needs_server_admin(sent):
    await _member("甲", MEMBER_QQ)
    await _member("乙", OTHER_QQ, game_uuid="U-2")
    async with _bot() as c:
        blocked = await c.get(
            "/api/bot/profile", params={"qq": MEMBER_QQ, "targetQq": OTHER_QQ}
        )
        as_admin = (
            await c.post(
                "/api/bot/profile",
                json={"qq": ADMIN_QQ, "targetQq": OTHER_QQ, "field": "游戏UID", "value": "U-9"},
            )
        ).json()
    assert blocked.status_code == 403
    assert as_admin["forOther"] is True
    assert as_admin["toQq"] == OTHER_QQ, "结果要发回被改的那个人（不是操作者）"
    assert store.member_by_qq(OTHER_QQ).game_uuid == "U-9"


async def test_profile_edit_requires_a_value(sent):
    await _member("甲", MEMBER_QQ)
    async with _bot() as c:
        res = await c.post(
            "/api/bot/profile", json={"qq": MEMBER_QQ, "field": "名字", "value": ""}
        )
    assert res.status_code == 400
    assert "用法" in str(res.json())
