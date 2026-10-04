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
    """服务器管理员授权：不是成员就自动建号，密钥**只私聊给本人**。"""
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
    assert first["keySent"] is True
    saved = store.member_by_qq(NEW_QQ)
    assert saved is not None and saved.display_name == "群友乙"
    # 明文**不回给调用方**：密钥只在站点发出的那条私聊里。从私聊里把它抠出来，
    # 验它确实对得上库里的哈希（否则成员根本登不进去）。
    assert "secretKey" not in first, "插件不该拿到明文（它拿不到，也就打不进群里）"
    assert len(sent) == 1 and sent[0]["umo"] == f"aiocqhttp:FriendMessage:{NEW_QQ}"
    hit = re.search(r"登录密钥（只显示这一次，请立即保存）：(\S+)", sent[0]["text"])
    assert hit is not None, "私聊里应当带上密钥"
    assert verify_secret(hit.group(1), saved.key_stored) is True
    assert "任何拿到这把密钥的人" in sent[0]["text"], "要说清「谁拿到谁能登录」"
    assert "比赛重置密钥" in sent[0]["text"], "并告诉他泄露了怎么自救"

    assert second["created"] is False and second["changed"] is False
    assert not second.get("keySent"), "已经建过号就不该再发一次密钥"
    assert len(sent) == 1, "第二次不该再发私聊"
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
# 本人的凭据自助（换密钥 / 换令牌 / 改推流码）
# --------------------------------------------------------------------------- #
async def test_credential_rotate_key_only_touches_mine(sent):
    """只能换**自己**的：新密钥私聊发本人，响应里不含明文。"""
    mine, _k, _b = await store.save_member(Member(name="甲", qq=NEW_QQ))
    other, _k2, _b2 = await store.save_member(Member(name="乙", qq="10009"))
    before_mine = store.member(mine.uid).key_stored
    before_other = store.member(other.uid).key_stored
    async with _bot() as c:
        res = (await c.post("/api/bot/credential", json={"qq": NEW_QQ, "what": "key"})).json()
        stranger = await c.post("/api/bot/credential", json={"qq": "99999", "what": "key"})
        wrong = await c.post("/api/bot/credential", json={"qq": NEW_QQ, "what": "随便什么"})

    assert res["ok"] is True and res["sent"] is True and res["kind"] == "key"
    assert stranger.status_code == 403, "没登记过 QQ 的人不能换任何凭据"
    assert wrong.status_code == 400
    assert sent[-1]["umo"] == f"aiocqhttp:FriendMessage:{NEW_QQ}", "新密钥只能私聊给本人"
    hit = re.search(r"登录密钥（只显示这一次）：(\S+)", sent[-1]["text"])
    assert hit is not None, "私聊里要带上新密钥"
    new_key = hit.group(1)
    assert new_key not in str(res), "明文绝不能出现在响应里（插件拿不到就打不进群）"
    assert verify_secret(new_key, store.member(mine.uid).key_stored) is True
    assert store.member(mine.uid).key_stored != before_mine, "自己的密钥要换掉"
    assert store.member(other.uid).key_stored == before_other, "别人的凭据一个字都不许动"
    assert "任何拿到这把密钥的人" in sent[-1]["text"], "要提醒「谁拿到谁能登录」"
    await store.delete_member(mine.uid)
    await store.delete_member(other.uid)


async def test_credential_token_needs_a_stream_key_first(sent):
    """没有推流码就不发令牌：令牌没有可填的地方，发了只是一串无处可用的字符。"""
    saved, _k, _b = await store.save_member(Member(name="还没有流名", qq=NEW_QQ))
    async with _bot() as c:
        blocked = await c.post("/api/bot/credential", json={"qq": NEW_QQ, "what": "token"})
    assert blocked.status_code == 400
    assert "推流码" in str(blocked.json())
    assert not sent, "被拒时不该发出任何私聊"
    await store.delete_member(saved.uid)


async def test_credential_change_stream_key_then_rotate_token(sent):
    """改推流码 → 就能换令牌；令牌私聊里带上推流地址与风险提示。"""
    await store.update(
        {"stream": {"enabled": True, "baseUrl": "https://live.example.com:8889"}}, actor="test"
    )
    saved, _k, _b = await store.save_member(Member(name="甲", qq=NEW_QQ))
    async with _bot() as c:
        bad = await c.post(
            "/api/bot/credential", json={"qq": NEW_QQ, "what": "streamId", "value": "中文流名"}
        )
        empty = await c.post(
            "/api/bot/credential", json={"qq": NEW_QQ, "what": "streamId", "value": ""}
        )
        ok = (
            await c.post(
                "/api/bot/credential", json={"qq": NEW_QQ, "what": "推流码", "value": "tom"}
            )
        ).json()
        stream_id = store.member(saved.uid).stream_id
        token = (await c.post("/api/bot/credential", json={"qq": NEW_QQ, "what": "token"})).json()

    assert bad.status_code == 400 and "ASCII" in str(bad.json()), "中文流名要当面拒掉"
    assert empty.status_code == 400
    assert stream_id == "tom"
    assert "推流码已改为 tom" in ok["note"]
    body = sent[-1]["text"]
    assert "https://live.example.com:8889/tom/whip" in body, "私聊里要给新的推流地址"
    assert "Bearer 令牌" in body
    hit = re.search(r"Bearer 令牌（只显示这一次）：(\S+)", body)
    assert hit is not None
    assert verify_secret(hit.group(1), store.member(saved.uid).bearer_stored) is True
    assert hit.group(1) not in str(token), "明文不该回给调用方"
    assert "任何拿到这串令牌的人" in body
    await store.delete_member(saved.uid)


async def test_credential_for_someone_else_goes_to_them_not_the_operator(sent):
    """服务器管理员可替别人重置，但**新值只发给被改的那个人**——管理员自己也看不到。"""
    other, _k, _b = await store.save_member(Member(name="新手", qq=NEW_QQ, stream_id="tom"))
    before = store.member(other.uid).key_stored
    event_admin, _k2, _b2 = await store.save_member(
        Member(name="赛事管理员甲", qq=EVENT_QQ, permission="event_admin")
    )
    async with _bot() as c:
        blocked = await c.post(
            "/api/bot/credential", json={"qq": EVENT_QQ, "targetQq": NEW_QQ, "what": "key"}
        )
        ok = (
            await c.post(
                "/api/bot/credential",
                json={"qq": ADMIN_QQ, "targetQq": NEW_QQ, "what": "key"},
            )
        ).json()
        missing = await c.post(
            "/api/bot/credential", json={"qq": ADMIN_QQ, "targetQq": "99998", "what": "key"}
        )

    assert blocked.status_code == 403, "不是服务器管理员就不能动别人的账号"
    assert missing.status_code == 404 and "比赛添加" in str(missing.json()), "先加进来再重置"
    assert ok["ok"] is True and ok["forOther"] is True and ok["toQq"] == NEW_QQ
    assert "你这边看不到" in ok["note"]
    assert store.member(other.uid).key_stored != before, "被改的是对方"
    # 新密钥只私聊给**被改的那个人**：操作者一个字都拿不到
    assert sent[-1]["umo"] == f"aiocqhttp:FriendMessage:{NEW_QQ}"
    assert all(item["umo"] != f"aiocqhttp:FriendMessage:{ADMIN_QQ}" for item in sent), (
        "新值不该发给服务器管理员"
    )
    hit = re.search(r"登录密钥（只显示这一次）：(\S+)", sent[-1]["text"])
    assert hit is not None
    assert verify_secret(hit.group(1), store.member(other.uid).key_stored) is True
    assert hit.group(1) not in str(ok), "明文不回给调用方"
    admin_name = store.server_admin().display_name
    assert f"「{admin_name}」帮你重置" in sent[-1]["text"], "要说清是谁代改的"
    await store.delete_member(other.uid)
    await store.delete_member(event_admin.uid)


async def test_credential_stream_key_must_be_free(sent):
    """推流码全局唯一：撞上别人就拒绝（两个直播间推同一个地址会互相顶掉）。"""
    a, _k, _b = await store.save_member(Member(name="甲", qq=NEW_QQ, stream_id="tom"))
    b, _k2, _b2 = await store.save_member(Member(name="乙", qq="10009"))
    async with _bot() as c:
        clash = await c.post(
            "/api/bot/credential", json={"qq": "10009", "what": "streamId", "value": "tom"}
        )
        same = (
            await c.post("/api/bot/credential", json={"qq": NEW_QQ, "what": "streamId", "value": "tom"})
        ).json()
    assert clash.status_code == 400 and "tom" in str(clash.json())
    assert same["ok"] is True, "自己把自己的流名再写一遍不算冲突"
    await store.delete_member(a.uid)
    await store.delete_member(b.uid)


async def test_stranger_can_read_but_cannot_touch(sent):
    """没登记 QQ 的路人：只读查询照答（那本来就是公开信息），要认人的操作一律挡下。"""
    async with _bot() as c:
        query = (await c.get("/api/bot/query", params={"kind": "list"})).json()
        mine = await c.get("/api/bot/my-links", params={"qq": "99999"})
        cred = await c.post("/api/bot/credential", json={"qq": "99999", "what": "key"})
    assert query["ok"] is True, "只读查询不需要成员身份（站点上这些信息本来就是公开的）"
    assert mine.status_code == 403 and "成员资料" in str(mine.json())
    assert cred.status_code == 403
    assert not sent, "被挡下时不该发出任何私聊"


async def test_credential_cannot_take_a_registered_stream_name(sent):
    """改推流码不能占用**站点里已登记的流名**（主直播间 / 频道 / 选手）。

    那条流名在 ``authorize_publish`` 里本来走白名单分支；某个成员一旦把它改成自己的，
    同名流名就会改走「成员 + Bearer 令牌」分支去找他——等于用别人的门牌号顶掉别人的直播间。
    """
    original = store.snapshot().stream.stream_key
    await store.update({"stream": {"streamKey": "mainroom"}}, actor="test")
    saved, _k, _b = await store.save_member(Member(name="甲", qq=NEW_QQ))
    try:
        async with _bot() as c:
            res = await c.post(
                "/api/bot/credential", json={"qq": NEW_QQ, "what": "streamId", "value": "mainroom"}
            )
        assert res.status_code == 400
        assert "已登记" in str(res.json())
        assert store.member(saved.uid).stream_id == "", "被拒之后一个字都不该改"
    finally:
        await store.update({"stream": {"streamKey": original}}, actor="test")
        await store.delete_member(saved.uid)


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
