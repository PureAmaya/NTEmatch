"""推流鉴权：**推流 ID + 令牌**必须成对，且令牌只能推自己的那个流名。

这条规则是「别人拿不到你的推流地址就能顶替你开播」的唯一防线，而它最容易在两类
改动里被破坏：① 把令牌校验写成「任意有效令牌即可」；② 新加流名时忘了要求令牌。
"""

from __future__ import annotations

import pytest

from app import live
from app.logic import clean_key
from app.models import Member
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
