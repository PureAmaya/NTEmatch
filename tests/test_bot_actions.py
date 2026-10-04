"""QQ 机器人的写操作：认人、授权 / 添加成员、我的推流地址、私聊投递、赛前提醒。

这一组盯的是「**谁能让机器人替谁做事**」——比功能本身更容易出事：

* 认人只认 QQ，且**由站点判定**（令牌只代表「这是本站的机器人」，不代表权限）；
* 只有服务器管理员能授权 / 添加成员，而且**不能经这条路授予 server_admin**；
* 新建成员的登录密钥只出现一次，且只会走**私聊**（绝不发群里）；
* 赛前提醒要**去重**，且只在窗口内发——服务重启不该补发出「明天开赛，还有 3 小时」这种错话。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx
import pytest

from app import db, qqbot, remind
from app.auth import hash_secret, verify_secret
from app.main import app
from app.models import Member
from app.store import store

BOT_TOKEN = "nte_test_bot_token"
ADMIN_QQ = "10001"
EVENT_QQ = "10002"
NEW_QQ = "10003"


def _iso(dt: datetime) -> str:
    """提醒判时间用的格式：与界面上录开赛时间的一模一样（本地时间、无时区）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
async def sent(monkeypatch):
    """配好机器人 + 服务器管理员 QQ，并把「真的发消息」换成记录器。

    返回那个列表：断言「发了什么、发到哪个会话」比断言返回值更能抓到问题
    （例如把密钥错发到群里）。
    """
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
            "remindEnabled": True,
        },
        actor="test",
        internal=True,
    )
    admin = store.server_admin()
    assert admin is not None, "启动自检应保证服务器管理员存在"
    await store.save_member(admin.model_copy(update={"qq": ADMIN_QQ}))

    out: list[dict] = []

    async def fake_send(text: str, *, settings=None, umo: str = "") -> dict:
        out.append({"text": text, "umo": umo or qqbot.resolved_umo(settings or {})})
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    monkeypatch.setattr(qqbot, "send_text", fake_send)
    yield out

    await store.set_qqbot(
        {"botApiTokenHash": "", "enabled": False, "remindEnabled": False},
        actor="test",
        internal=True,
    )


async def _clear_marks() -> None:
    """清掉提醒的去重标记。

    去重标记是**跨用例共享**的（都写在同一个测试库里）：上一次失败留下的标记会让
    这次「什么都没发」，看起来像功能坏了。所以每个提醒用例开头先清一遍。
    """
    for lead in (1440, 120):
        await store.set_meta(f"test:remind:{store.current_id}:{lead}", "")


def _bot() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    )


# --------------------------------------------------------------------------- #
# 认人
# --------------------------------------------------------------------------- #
async def test_whoami_knows_member_and_stranger(sent):
    """登记过 QQ 的人认得出身份；路人只知道「不认识」。"""
    async with _bot() as c:
        mine = (await c.get("/api/bot/whoami", params={"qq": ADMIN_QQ})).json()
        stranger = (await c.get("/api/bot/whoami", params={"qq": "99999"})).json()
    assert mine["known"] is True and mine["isServer"] is True
    assert mine["permission"] == "server_admin"
    assert stranger["known"] is False
    assert "还没对上" in stranger["note"]


# --------------------------------------------------------------------------- #
# 授权 / 添加成员
# --------------------------------------------------------------------------- #
async def test_grant_is_rejected_for_non_server_admin(sent):
    """赛事管理员（乃至路人）拿不到这个能力——闸门在站点，不在插件。"""
    _saved, key, _bearer = await store.save_member(
        Member(name="赛事管理员甲", qq=EVENT_QQ, permission="event_admin")
    )
    _ = key
    async with _bot() as c:
        blocked = await c.post(
            "/api/bot/members",
            json={
                "actorQq": EVENT_QQ,
                "targetQq": NEW_QQ,
                "name": "群友甲",
                "permission": "event_admin",
            },
        )
        stranger = await c.post(
            "/api/bot/members",
            json={"actorQq": "99999", "targetQq": NEW_QQ, "permission": "member"},
        )
    assert blocked.status_code == 403
    assert "服务器管理员" in str(blocked.json())
    assert stranger.status_code == 403
    assert store.member_by_qq(NEW_QQ) is None, "被拒之后不该留下任何成员"
    await store.delete_member(next(m.uid for m in store.members() if m.qq == EVENT_QQ))


async def test_grant_creates_member_and_hands_out_the_key(sent):
    """服务器管理员授权：不是成员就自动建号，密钥只在这**一次**返回。"""
    async with _bot() as c:
        first = (
            await c.post(
                "/api/bot/members",
                json={
                    "actorQq": ADMIN_QQ,
                    "targetQq": NEW_QQ,
                    "name": "群友乙",
                    "permission": "event_admin",
                },
            )
        ).json()
        second = (
            await c.post(
                "/api/bot/members",
                json={
                    "actorQq": ADMIN_QQ,
                    "targetQq": NEW_QQ,
                    "name": "群友乙",
                    "permission": "event_admin",
                },
            )
        ).json()

    assert first["created"] is True and first["changed"] is True
    assert first["permission"] == "event_admin"
    saved = store.member_by_qq(NEW_QQ)
    assert saved is not None and saved.display_name == "群友乙"
    # 返回的明文密钥要能对上库里那份哈希（否则成员根本登不进去）
    assert verify_secret(first["secretKey"], saved.key_stored) is True

    assert second["created"] is False and second["changed"] is False
    assert not second.get("secretKey"), "已经建过号就不该再发一次密钥"
    await store.delete_member(saved.uid)


async def test_grant_only_changes_permission(sent):
    """已是成员：只改权限，**不碰密钥**（否则等于偷偷把人的登录口令作废）。"""
    saved, _key, _bearer = await store.save_member(
        Member(name="待升级", qq=NEW_QQ, permission="member")
    )
    before = store.member(saved.uid).key_stored
    async with _bot() as c:
        res = (
            await c.post(
                "/api/bot/members",
                json={
                    "actorQq": ADMIN_QQ,
                    "targetQq": NEW_QQ,
                    "permission": "event_admin",
                },
            )
        ).json()
    assert res["changed"] is True and res["created"] is False
    assert not res.get("secretKey")
    after = store.member(saved.uid)
    assert after.permission == "event_admin"
    assert after.key_stored == before, "只改权限，密钥必须原封不动"
    await store.delete_member(saved.uid)


async def test_grant_refuses_server_admin_and_blank_target(sent):
    """不许经群里授予最高权限；也必须真的 @ 到人。"""
    async with _bot() as c:
        top = await c.post(
            "/api/bot/members",
            json={"actorQq": ADMIN_QQ, "targetQq": NEW_QQ, "permission": "server_admin"},
        )
        blank = await c.post(
            "/api/bot/members",
            json={"actorQq": ADMIN_QQ, "targetQq": "", "permission": "member"},
        )
    # 提示里要写清「只能给这两个」，并把「最高权限请到站点改」说在前面
    assert top.status_code == 400
    detail = str(top.json())
    assert "member / event_admin" in detail and "站点" in detail
    assert blank.status_code == 400


# --------------------------------------------------------------------------- #
# 我的推流与直播间地址
# --------------------------------------------------------------------------- #
async def test_my_links_returns_site_room_url(sent):
    """给的是**本站**的直播间地址 + 媒体服务器的推流地址（不是 m 开头的观看地址）。"""
    await store.update(
        {
            "stream": {
                "enabled": True,
                "baseUrl": "https://live.example.com:8889",
                "hlsBase": "https://live.example.com:8888",
            }
        },
        actor="test",
    )
    saved, _key, _bearer = await store.save_member(
        Member(name="选手甲", qq=NEW_QQ, stream_id="player1", room_title="甲的直播间")
    )
    async with _bot() as c:
        data = (await c.get("/api/bot/my-links", params={"qq": NEW_QQ})).json()
        unknown = await c.get("/api/bot/my-links", params={"qq": "99999"})

    assert data["ok"] is True
    assert data["pushUrl"] == "https://live.example.com:8889/player1/whip"
    assert data["roomUrl"] == "http://nte.test/channels/player1"
    body = "\n".join(data["parts"])
    assert data["roomUrl"] in body and data["pushUrl"] in body
    assert "Bearer 令牌" in body, "得告诉本人 OBS 里还要填令牌"
    # 站内地址不能出现媒体服务器的观看地址（那是「m 开头」那一套）
    assert "live.example.com:8888" not in body
    assert unknown.status_code == 403
    await store.delete_member(saved.uid)


async def test_my_links_without_stream_id_explains_what_to_do(sent):
    """还没填推流 ID：告诉本人去哪儿填，而不是给一堆空地址。"""
    saved, _key, _bearer = await store.save_member(Member(name="还没开播", qq=NEW_QQ))
    async with _bot() as c:
        data = (await c.get("/api/bot/my-links", params={"qq": NEW_QQ})).json()
    assert data["streamId"] == "" and data["pushUrl"] == ""
    assert "推流 ID" in "\n".join(data["parts"])
    await store.delete_member(saved.uid)


# --------------------------------------------------------------------------- #
# 私聊投递
# --------------------------------------------------------------------------- #
async def test_notify_sends_a_private_message(sent):
    """私聊走 ``…:FriendMessage:QQ``：只发本人，不进群。"""
    async with _bot() as c:
        res = (await c.post("/api/bot/notify", json={"qq": ADMIN_QQ, "text": "你好"})).json()
    assert res["ok"] is True
    assert sent[-1]["umo"] == f"aiocqhttp:FriendMessage:{ADMIN_QQ}"
    assert sent[-1]["text"] == "你好"
    assert not any("GroupMessage" in item["umo"] for item in sent), "不该误发到群里"


async def test_notify_rejects_blank(sent):
    async with _bot() as c:
        no_qq = await c.post("/api/bot/notify", json={"qq": "", "text": "x"})
        no_text = await c.post("/api/bot/notify", json={"qq": ADMIN_QQ, "text": "  "})
    assert no_qq.status_code == 400
    assert no_text.status_code == 400


# --------------------------------------------------------------------------- #
# 赛前提醒
# --------------------------------------------------------------------------- #
def test_due_leads_windows():
    """只在【提前量 − 1 小时, 提前量】窗口内算「到点」。"""
    now = datetime(2026, 10, 4, 12, 0, 0)  # noqa: DTZ001  (提醒比较的是"本地墙上时间"，没有时区概念)

    def at(hours: float) -> str:
        return _iso(now + timedelta(hours=hours))

    leads = [1440, 120]
    assert remind.due_leads(at(24), leads, now=now) == [1440]
    assert remind.due_leads(at(23), leads, now=now) == [1440]
    assert remind.due_leads(at(22), leads, now=now) == []  # 太早：还没到窗口
    assert remind.due_leads(at(2), leads, now=now) == [120]
    assert remind.due_leads(at(1.5), leads, now=now) == [120]
    assert remind.due_leads(at(0.5), leads, now=now) == []  # 太晚：窗口已过
    assert remind.due_leads(at(-1), leads, now=now) == []  # 已经开赛
    assert remind.due_leads("", leads, now=now) == []
    assert remind.due_leads("不是时间", leads, now=now) == []


async def test_reminder_fires_once_and_mentions_the_owner(sent, monkeypatch):
    """开赛前一天发一条、@ 举办者；再巡检不会重复发。"""
    monkeypatch.setattr(remind, "MARK_PREFIX", "test:remind:")
    await _clear_marks()
    start = datetime.now() + timedelta(hours=23)  # noqa: DTZ005  (开赛时间就是本地时间)
    await store.update(
        {"event": {"startTime": _iso(start), "venue": "测试场馆", "locked": False}}, actor="test"
    )
    admin = store.server_admin()

    first = await remind.tick()
    assert len(first) == 1, "到点应当发一条"
    assert first[0]["lead"] == 1440
    assert first[0]["owner"] == admin.display_name
    text = sent[-1]["text"]
    assert "1 天后开赛" in text
    assert f"[CQ:at,qq={ADMIN_QQ}]" in text, "要 @ 到举办者本人"
    assert "测试场馆" in text

    assert await remind.tick() == [], "同一条提前量只发一次"
    # 清掉测试用的标记，别影响别的用例
    await store.set_meta(f"test:remind:{store.current_id}:1440", "")


async def test_reminder_respects_switches(sent, monkeypatch):
    """关掉开关、或比赛已开赛 / 已结束时不发。"""
    monkeypatch.setattr(remind, "MARK_PREFIX", "test:remind:")
    await _clear_marks()
    start = _iso(datetime.now() + timedelta(hours=23))  # noqa: DTZ005
    await store.update({"event": {"startTime": start, "locked": False}}, actor="test")

    await store.set_qqbot({"remindEnabled": False}, actor="test", internal=True)
    assert await remind.tick() == []

    await store.set_qqbot({"remindEnabled": True}, actor="test", internal=True)
    await store.update({"event": {"locked": True}}, actor="test")
    assert await remind.tick() == [], "比赛已开始就不该再提醒"

    await store.update({"event": {"locked": False}}, actor="test")
    # 没有登记 QQ 的举办者：仍然要发（退化成 @名字），但文案里要带上他
    admin = store.server_admin()
    await store.save_member(admin.model_copy(update={"qq": ""}))
    result = await remind.tick()
    assert len(result) == 1
    assert result[0]["hasOwnerQq"] is False
    assert sent[-1]["text"].startswith(f"@{admin.display_name}")
    assert not re.search(r"\[CQ:at", sent[-1]["text"]), "没 QQ 就别硬塞 CQ 码"
    await store.save_member(admin.model_copy(update={"qq": ADMIN_QQ}))
    await store.set_meta(f"test:remind:{store.current_id}:1440", "")
