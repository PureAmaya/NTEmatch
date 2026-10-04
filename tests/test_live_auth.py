"""推流鉴权：**推流 ID + 令牌**必须成对，且令牌只能推自己的那个流名。

这条规则是「别人拿不到你的推流地址就能顶替你开播」的唯一防线，而它最容易在两类
改动里被破坏：① 把令牌校验写成「任意有效令牌即可」；② 新加流名时忘了要求令牌。
"""

from __future__ import annotations

import pytest

from app import live
from app.logic import clean_key
from app.models import LiveBan, Member
from app.store import store


async def _ready_store():
    if not store.current_id:
        await store.start()
    return store


@pytest.fixture
async def two_members():
    """两个各带推流 ID 与 Bearer 令牌的成员；用完删掉，别污染共享的 store。"""
    await _ready_store()
    made: list[tuple[str, str, str]] = []
    for uid, name, key in (("u_auth_a", "甲", "stream-a"), ("u_auth_b", "乙", "stream-b")):
        _, _, bearer = await store.save_member(
            Member(uid=uid, name=name, stream_id=key, permission="member"), new_bearer=True
        )
        assert bearer, "save_member(new_bearer=True) 应该回明文令牌"
        made.append((uid, key, bearer))
    try:
        yield made
    finally:
        for uid, _key, _bearer in made:
            await store.delete_member(uid)


async def test_token_only_pushes_its_own_stream(two_members):
    (_uid_a, key_a, bearer_a), (_uid_b, key_b, _bearer_b) = two_members

    ok, reason = live.authorize_publish(key_a, bearer_a)
    assert ok is True, reason

    # 甲的令牌推不了乙的流：这正是「令牌必须与推流 ID 一致」的含义
    ok, reason = live.authorize_publish(key_b, bearer_a)
    assert ok is False
    assert "令牌" in reason


async def test_missing_or_wrong_token_is_rejected(two_members):
    _uid_a, key_a, _bearer_a = two_members[0]

    assert live.authorize_publish(key_a, "")[0] is False
    assert live.authorize_publish(key_a, "随便编一个")[0] is False


async def test_unregistered_stream_is_rejected():
    await _ready_store()
    assert live.authorize_publish("not-registered-at-all", "x")[0] is False


async def test_member_without_bearer_cannot_publish(two_members):
    """成员没配令牌时不能推（否则「有推流 ID 就能推」又回来了）。"""
    _uid_a, key_a, bearer_a = two_members[0]
    member = store.member("u_auth_a")
    await store.save_member(member.model_copy(update={"stream_id": key_a}))

    # 直接把存储里的令牌清掉，模拟「没配过令牌」
    bare = Member(uid="u_auth_c", name="丙", stream_id="stream-c", permission="member")
    await store.save_member(bare)  # 不生成令牌
    try:
        ok, reason = live.authorize_publish("stream-c", bearer_a)
        assert ok is False
        assert "令牌" in reason
    finally:
        await store.delete_member("u_auth_c")


async def test_main_stream_token_is_optional_but_enforced_when_set():
    """主直播间 / 遗留频道：留空照旧放行；填了就必须带对令牌。"""
    st = await _ready_store()
    main_key = clean_key(st.snapshot().stream.stream_key) or "stream"

    original = st.snapshot().stream.push_token
    try:
        await st.update({"stream": {"pushToken": ""}}, actor="test")
        assert live.authorize_publish(main_key, "")[0] is True  # 旧行为：白名单放行

        await st.update({"stream": {"pushToken": "main-secret"}}, actor="test")
        assert live.authorize_publish(main_key, "")[0] is False
        assert live.authorize_publish(main_key, "错的")[0] is False
        assert live.authorize_publish(main_key, "main-secret")[0] is True
    finally:
        await st.update({"stream": {"pushToken": original}}, actor="test")


async def test_ban_follows_the_member_not_the_stream_name():
    """封禁按**成员**记：换令牌、改推流码都绕不过（``authorize_publish`` 先查封禁）。

    这是「禁止某人直播」真正成立的前提——封的是**人**，不是那串名字：
    否则他改一个推流码就又能开播了。
    """
    st = await _ready_store()
    _saved, _key, bearer = await st.save_member(
        Member(name="捣乱的", stream_id="ban-a", permission="member"), new_bearer=True
    )
    member = next(m for m in st.members() if m.stream_id == "ban-a")
    ban = LiveBan(member_uid=member.uid, stream_id="ban-a", name="捣乱的", reason="测试")
    saved_ban = await st.add_live_ban(ban, actor="test")
    try:
        ok, reason = live.authorize_publish("ban-a", bearer)
        assert ok is False and "封禁" in reason

        # 换令牌：照样拒（封禁在令牌校验之前）
        _m, _k, fresh = await st.save_member(st.member(member.uid), new_bearer=True)
        assert live.authorize_publish("ban-a", fresh)[0] is False

        # 改推流码：新名字照样拒——封的是这个人
        await st.save_member(st.member(member.uid).model_copy(update={"stream_id": "ban-b"}))
        ok, reason = live.authorize_publish("ban-b", fresh)
        assert ok is False and "封禁" in reason
    finally:
        await st.remove_live_ban(saved_ban.id, actor="test")
        await st.delete_member(member.uid)
