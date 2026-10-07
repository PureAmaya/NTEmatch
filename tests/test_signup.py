"""群里自助报名 / 取消报名（``POST /api/bot/signup``，命令「比赛报名」「比赛取消报名」）。

这一组盯四件事，每一件单独看都不显眼，合起来就是「这个功能会不会被用坏」：

* **报名只改名单**：不组队、不定赛制、不生成赛程——组队与赛制等报名结束由管理员在网站上做
  （用户明确要的分工）；
* **只有筹备中的届能报名 / 取消**：开赛之后名单该冻住，赛后更不该动；
* **取消报名只取消参赛资格**：成员档案与本届选手档案都留着，队伍与赛程一个字不动；
* **白名单群里连成员都不是也能报**：站点顺手建成成员、密钥**只私聊**；
  不在白名单群的陌生人挡住（成员照旧能报名）。

还有一条容易被忽略的：报名写的是**命令里那一届**，不是「当前届」——
`store.mutate` 只认当前届，所以这里特意用「非当前届」来测。
"""

from __future__ import annotations

import re

import httpx
import pytest

from app import db, logic, qqbot
from app.auth import hash_secret
from app.main import app
from app.models import Member
from app.store import store

BOT_TOKEN = "nte_test_signup_token"
KNOWN_QQ = "20001"      # 已经是成员
NEW_QQ = "20002"        # 还不是成员（白名单群里的新人）
NEW_QQ_2 = "20003"      # 同上，换个写法再测一次白名单
GROUP = "900001"        # 白名单群
OTHER_GROUP = "900002"  # 不在白名单的群


@pytest.fixture(autouse=True)
async def _clean_members():
    """这几个 QQ 每轮都从「不在成员列表里」开始，跑完也清掉。

    成员是**全局**的（删届次不会连带删成员），上一条用例建出来的会留在库里：
    两个同 QQ 的成员会让 ``member_by_qq`` 查不到人（它只在**唯一命中**时返回），
    表现特别像「报名接口坏了」。所以每轮先把这几个 QQ 清干净。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()

    async def purge() -> None:
        for qq in (KNOWN_QQ, NEW_QQ, NEW_QQ_2):
            for member in [m for m in store.members() if m.qq == qq]:
                await store.delete_member(member.uid, actor="test")

    await purge()
    yield
    await purge()


@pytest.fixture
async def bot(monkeypatch):
    """配好机器人令牌 + 白名单群，并把「真的发私聊」换成记录器。

    返回发出去的私聊列表：断言「密钥只出现在私聊里」比断言返回值更能抓到问题
    （把密钥错发到群里就是最严重的那种事故）。
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
            "signupGroups": GROUP,
        },
        actor="test",
        internal=True,
    )
    out: list[dict] = []

    async def fake_send(text: str, *, settings=None, umo: str = "") -> dict:
        out.append({"text": text, "umo": umo or qqbot.resolved_umo(settings or {})})
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    monkeypatch.setattr(qqbot, "send_text", fake_send)
    yield out


@pytest.fixture
async def botclient():
    """带插件令牌的客户端（插件就是这么调站点的）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    ) as c:
        yield c


@pytest.fixture
async def events():
    """两届：``target`` 是被测的那届（**非当前届**），``other`` 是当前届。用完都删掉。

    为什么非当前届：报名写的是命令里那一届，而 ``store.mutate`` 只认当前届——
    这正是最容易写错、也最容易把别人的视图切走的地方。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("报名用例届 A")
    target = store.current_id
    await store.create_event("报名用例届 B")
    other = store.current_id
    await store.update_event_meta(target, {"status": "draft"})
    await store.update_event_meta(other, {"status": "draft"})
    try:
        yield target, other
    finally:
        for event_id in (target, other):
            if previous:
                await store.switch_event(previous)
            await store.delete_event(event_id)


async def _member(qq: str, name: str) -> Member:
    member, _key, _bearer = await store.save_member(
        Member(uid="", name=name, qq=qq, permission="member")
    )
    return member


def _body(qq: str, event_id: str, *, action: str = "join", group: str = GROUP, name: str = "") -> dict:
    return {"qq": qq, "action": action, "event": event_id, "group": group, "name": name}


# --------------------------------------------------------------------------- #
# 报名 / 取消报名本身
# --------------------------------------------------------------------------- #
async def test_member_signs_up_for_a_draft_event(bot, botclient, events):
    """成员报名：只进这一届的名单——不组队、不建赛程、也不动「当前届」指针。"""
    target, other = events
    await _member(KNOWN_QQ, "甲")
    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True and data["playerId"]
    assert "已报名" in data["text"] and data["participants"] == 1

    after = await store.read_event(target)
    assert after.participants == [data["playerId"]]
    assert after.participants_set is True, "报了名就该落成显式名单"
    assert after.teams == [] and after.rounds == [], "报名不该顺手组队 / 生成赛程"
    assert after.players[0].member_uid, "本届选手档案要跟成员挂上"
    assert store.current_id == other, "给别的届报名不该把所有人的视图切走"


async def test_signing_up_twice_changes_nothing(bot, botclient, events):
    """重复报名：一句话说清「你已经在名单里」，不写库、不重复建选手。"""
    target, _ = events
    await _member(KNOWN_QQ, "甲")
    await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))
    revision = (await store.read_event(target)).revision

    again = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))
    assert again.status_code == 200, again.text
    assert again.json()["already"] is True
    assert "已经在" in again.json()["text"]
    after = await store.read_event(target)
    assert after.revision == revision, "什么都没变就不该涨 revision"
    assert len(after.players) == 1


async def test_signup_keeps_the_existing_roster(bot, botclient, events):
    """从没定过名单的届（= 全员参与）：新人报名时，原来的参与人不会被挤出名单。"""
    target, _ = events
    await store.mutate_event(
        target,
        lambda data: {
            **data,
            "players": [{"id": "p01", "name": "甲"}, {"id": "p02", "name": "乙"}],
        },
        actor="test",
    )
    await _member(NEW_QQ, "新来的")
    res = await botclient.post("/api/bot/signup", json=_body(NEW_QQ, target))
    assert res.status_code == 200, res.text
    after = await store.read_event(target)
    assert sorted(after.participants) == ["p01", "p02", res.json()["playerId"]]
    assert res.json()["playerId"] == "p03", "新选手沿用 pNN 编号"


async def test_cancel_signup_only_drops_this_event(bot, botclient, events):
    """取消报名：只取消这一届的参赛资格——成员在、本届选手档案也在。"""
    target, _ = events
    member = await _member(KNOWN_QQ, "甲")
    await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))
    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target, action="cancel"))
    assert res.status_code == 200, res.text
    assert "已取消报名" in res.json()["text"]

    after = await store.read_event(target)
    assert after.participants == []
    assert [p.id for p in after.players], "选手档案留着（名字 / 头像 / UUID 不是报名的一部分）"
    assert store.member(member.uid) is not None, "取消报名绝不能删成员"
    assert after.teams == [] and after.rounds == []
    # 空名单也要留住「这是一份显式名单」：否则他会因为「名单空了」又变回全员参与
    assert after.participants_set is True
    assert logic.joined_players(after) == [], "取消之后不该又变回全员参与"


async def test_cancel_without_signup_says_so(bot, botclient, events):
    """没报过名就取消：回一句人话，不写库。"""
    target, _ = events
    await _member(KNOWN_QQ, "甲")
    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target, action="cancel"))
    assert res.status_code == 200, res.text
    assert res.json()["already"] is True
    assert "本来就不在" in res.json()["text"]


# --------------------------------------------------------------------------- #
# 白名单：非成员也能报（顺手建号）
# --------------------------------------------------------------------------- #
async def test_newcomer_in_whitelisted_group_becomes_a_member(bot, botclient, events):
    """白名单群里的新人：直接报上、顺手建成成员，**密钥只走私聊**。"""
    target, _ = events
    res = await botclient.post(
        "/api/bot/signup", json=_body(NEW_QQ, target, name="新来的")
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["createdMember"] is True

    member = store.member_by_qq(NEW_QQ)
    assert member is not None, "白名单群里报名要顺手建成成员"
    assert member.name == "新来的" and member.permission == "member"
    after = await store.read_event(target)
    assert after.participants == [data["playerId"]]

    assert bot, "建成成员后要把登录密钥私聊给本人"
    assert f"FriendMessage:{NEW_QQ}" in bot[0]["umo"], "密钥只能私聊，不能发群"
    hit = re.search(r"登录密钥（只显示这一次，请立即保存）：(\S+)", bot[0]["text"])
    assert hit, f"私聊里应当有密钥：{bot[0]['text'][:80]}"
    assert hit.group(1) not in res.text, "响应体（会回群）里一个字节都不能有密钥"


async def test_newcomer_outside_the_whitelist_is_refused(bot, botclient, events):
    """不在白名单群、又不是成员：挡住，而且**不能留下半个成员**。"""
    target, _ = events
    for group in (OTHER_GROUP, ""):
        res = await botclient.post("/api/bot/signup", json=_body(NEW_QQ, target, group=group))
        assert res.status_code == 403, res.text
        assert "成员" in str(res.json())
    assert store.member_by_qq(NEW_QQ) is None, "被拒的报名不该建出成员"
    after = await store.read_event(target)
    assert after.participants == []


async def test_whitelist_matches_group_id_or_full_umo(bot, botclient, events):
    """白名单两边写法不同也算同一个群：管理员可能填群号，插件上报的可能是完整 UMO。"""
    target, _ = events
    await store.set_qqbot({"signupGroups": GROUP}, actor="test", internal=True)
    res = await botclient.post(
        "/api/bot/signup", json=_body(NEW_QQ, target, group=f"aiocqhttp:GroupMessage:{GROUP}")
    )
    assert res.status_code == 200, res.text

    await store.set_qqbot({"signupGroups": f"aiocqhttp:GroupMessage:{GROUP}"}, actor="test", internal=True)
    res = await botclient.post("/api/bot/signup", json=_body(NEW_QQ_2, target, group=GROUP))
    assert res.status_code == 200, res.text


async def test_saving_the_whole_settings_form_keeps_every_field(admin_client):
    """管理端「保存」现在**整张表单原样提交**：服务端要照单收下每个已知键、忽略多余的。

    钉的是那个「填了、保存成功、刷新就没了」的 bug 的服务端侧契约——前端侧由
    ``tools/check_assets.py`` 盯着（保存分支必须是整张表单，不许手写字段表）。
    """
    res = await admin_client.put(
        "/api/qqbot",
        json={
            "enabled": True,
            "baseUrl": "http://astrbot.test",
            "apiKey": "abk_x",
            "umo": "123456",
            "platform": "aiocqhttp",
            "atMode": "cq",
            "maxChars": "1200",
            "timeout": "10",
            "cooldownSeconds": "20",
            "maxPerHour": "30",
            "maxParts": "8",
            "remindEnabled": True,
            "remindLeads": "1440,120",
            "imageCards": False,
            "autoResultEnabled": False,
            "signupGroups": "900001,900002",
            "前端多带的键": "应当被忽略",
        },
    )
    assert res.status_code == 200, res.text
    settings = store.qqbot_settings()
    assert settings["signupGroups"] == "900001,900002", "白名单要真的存下来"
    assert settings["imageCards"] is False, "图片推送开关要真的存下来"
    assert settings["autoResultEnabled"] is False, "打完自动播报开关要真的存下来"
    assert settings["remindLeads"] == "1440,120"


def test_whitelist_setting_drops_what_can_never_match():
    """白名单里只留拼得出会话标识的字符：填群名这种永远匹配不上的值要被剔掉。"""
    clean = qqbot.normalize_settings(
        {"signupGroups": "900001， 900002; 900001、打游戏一群 900003"},
        dict(qqbot.DEFAULT_SETTINGS),
    )
    assert clean["signupGroups"] == "900001,900002,900003"


# --------------------------------------------------------------------------- #
# 闸门：什么时候不能报名
# --------------------------------------------------------------------------- #
async def test_signup_only_while_the_event_is_being_prepared(bot, botclient, events):
    """开赛 / 结束后名单冻住：报名与取消报名都不行，并说清是「不是筹备中」。"""
    target, _ = events
    await _member(KNOWN_QQ, "甲")
    for status in ("active", "closed"):
        await store.update_event_meta(target, {"status": status})
        for action in ("join", "cancel"):
            res = await botclient.post(
                "/api/bot/signup", json=_body(KNOWN_QQ, target, action=action)
            )
            assert res.status_code == 400, res.text
            assert "筹备中" in str(res.json())
        after = await store.read_event(target)
        assert after.participants == [], "被拒的报名不能留下痕迹"
    await store.update_event_meta(target, {"status": "draft"})


async def test_signup_refused_when_the_event_is_already_teamed_up(bot, botclient, events):
    """已经组队（或生成赛程）的届：名单不能再自助改——改了队伍与对阵就对不上。"""
    target, _ = events
    await _member(KNOWN_QQ, "甲")
    await store.mutate_event(
        target,
        lambda data: {
            **data,
            "teams": [{"id": "t1", "label": "甲队", "group": "A", "playerIds": []}],
        },
        actor="test",
    )
    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))
    assert res.status_code == 400, res.text
    assert "组队" in str(res.json())


async def test_group_facing_texts_have_no_markdown(bot, botclient, events):
    """回给群里的每一句都不能带 Markdown 记号。

    插件是把站点的 ``error`` / ``text`` **原样**回群的（不经过 ``qqbot.to_plain_text``），
    所以 ``**筹备中**`` 这种写法会原样印在群里——文案只能自己保持干净。
    """
    target, _ = events
    await _member(KNOWN_QQ, "甲")
    texts = [
        (await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))).json()["text"],
        (
            await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target, action="cancel"))
        ).json()["text"],
        str(
            (
                await botclient.post(
                    "/api/bot/signup", json=_body(NEW_QQ, target, group=OTHER_GROUP)
                )
            ).json()
        ),
    ]
    await store.update_event_meta(target, {"status": "active"})
    texts.append(str((await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target))).json()))
    texts.append(str((await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, "e999"))).json()))
    await store.update_event_meta(target, {"status": "draft"})
    for text in texts:
        assert "**" not in text and "`" not in text, text


async def test_unknown_event_and_missing_fields_are_clear(bot, botclient, events):
    """届次写错 / 没写届次 / 没认到 QQ：都要回一句能照做的提示，而不是 500。"""
    target, _ = events
    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, "e999"))
    assert res.status_code == 404 and "没有这一届" in str(res.json())

    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, ""))
    assert res.status_code == 400 and "哪一届" in str(res.json())

    res = await botclient.post("/api/bot/signup", json={"event": target, "qq": ""})
    assert res.status_code == 400 and "QQ" in str(res.json())

    res = await botclient.post("/api/bot/signup", json=_body(KNOWN_QQ, target, action="quit"))
    assert res.status_code == 400 and "join" in str(res.json())
