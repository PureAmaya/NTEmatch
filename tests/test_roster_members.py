"""本届参赛名单**从成员列表来**：勾一位成员，他就参加这一届。

背景：平台有两层——全局的「成员」（跨届共享）与每届的「报名池 / 参与名单」。
以前报名池只能一位位手工登记（姓名 + 游戏 UUID），而成员列表里明明已经有人了。
这组用例盯住新那条路：

* 候选 = **全部成员**（含本届还没有档案的），并标出「有没有档案 / 参不参加」；
* 勾选 → 缺档案的按成员资料自动建好（姓名 / QQ / 头像 / 游戏 UUID 一起带过来）；
* 成员资料改了 → 再勾选时档案跟着更新（选手档案只是成员在本届的投影）；
* **取消勾选只改名单、不删档案**（档案一删，赛程与比分就断了）；
* 非成员的客串选手（手工登记的）不会被一次勾选挤出去；
* 比赛开始后照旧锁定（与其它名单操作一致）。
"""

from __future__ import annotations

import pytest

from app import db, logic
from app.models import Member
from app.store import store


@pytest.fixture(autouse=True)
async def _own_event():
    """这组用例跑在**自己新建的一届**上。

    参与名单是「每届一份」，而单场比赛需要几个人由赛制决定（``need = 每队人数 × 2``，
    出厂默认 4 人）。别的用例会把当前届改得**带赛程**——那时再勾一位成员就会触发
    「按名单重排未结算对局」，撞上「可用选手 1 人，少于单场所需的 4 人」这种与
    本题无关的 400（单独跑这组时是绿的，全量跑就红：典型的测试互相污染）。
    自己建一届（出厂赛制、空赛程），谁来跑都一样。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    await store.create_event("名单用例专用届")
    mine = store.current_id
    try:
        yield
    finally:
        if previous and previous != mine:
            await store.switch_event(previous)
        await store.delete_event(mine)


@pytest.fixture(autouse=True)
async def _clean_members():
    """用例造的成员用完删掉，别留给别的用例（成员是跨届共享的测试数据）。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    before = {m.uid for m in store.members()}

    yield

    for member in list(store.members()):
        if member.uid not in before:
            await store.delete_member(member.uid)


async def _add_member(name: str, qq: str = "", uuid: str = "") -> Member:
    member, _key, _bearer = await store.save_member(
        Member(name=name, qq=qq, game_uuid=uuid, permission="member")
    )
    return member


async def _candidates(client) -> dict:
    res = await client.get("/api/roster/members")
    assert res.status_code == 200, res.text
    return res.json()


async def _adopt(client, member_uids: list[str], player_ids: list[str] | None = None):
    return await client.post(
        "/api/roster/members",
        json={"memberUids": member_uids, "playerIds": player_ids or []},
    )


# --------------------------------------------------------------------------- #
# 候选列表：来源是成员列表
# --------------------------------------------------------------------------- #
async def test_candidates_list_every_member(admin_client):
    """「选择参赛成员」列的必须是**全部成员**——没登记过档案的也要在里面。"""
    fresh = await _add_member("还没上过场的阿岚", qq="20001")

    body = await _candidates(admin_client)
    rows = {row["uid"]: row for row in body["members"]}
    assert fresh.uid in rows, "新成员必须出现在候选里（否则管理员根本看不到他）"
    assert rows[fresh.uid]["playerId"] == "", "还没勾选过，本届不该有档案"
    assert rows[fresh.uid]["selected"] is False
    assert "qq" not in rows[fresh.uid], "候选列表不下发 QQ（这个界面用不到）"


# --------------------------------------------------------------------------- #
# 勾选 = 参加本届（缺档案自动建）
# --------------------------------------------------------------------------- #
async def test_picking_a_member_creates_his_profile(admin_client):
    """勾一位成员：本届自动建好档案，资料从成员带过来，并进入参与名单。"""
    member = await _add_member("夜见", qq="20002", uuid="GAME-UUID-9")

    res = await _adopt(admin_client, [member.uid])
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["created"]) == 1, body

    cfg = store.snapshot()
    player = next(p for p in cfg.players if p.member_uid == member.uid)
    assert player.id in body["participants"], "勾了就该在本届参与名单里"
    assert player.name == "夜见" and player.qq == "20002" and player.uuid == "GAME-UUID-9"
    assert logic.is_selected(cfg, player.id) is True


async def test_picking_again_is_idempotent(admin_client):
    """重复勾选不会重复建档案（同一成员在本届只有一份档案）。"""
    member = await _add_member("重复勾选的人")
    first = (await _adopt(admin_client, [member.uid])).json()
    second = (await _adopt(admin_client, [member.uid])).json()
    assert len(first["created"]) == 1
    assert second["created"] == [], "第二次不该再建一份档案"
    assert len([p for p in store.snapshot().players if p.member_uid == member.uid]) == 1


async def test_profile_follows_the_member_record(admin_client):
    """成员资料改过之后再勾选：本届档案跟着更新（别名 / 换 QQ 不用去改两处）。"""
    member = await _add_member("旧名字", qq="20003")
    await _adopt(admin_client, [member.uid])

    renamed = member.model_copy(update={"name": "新名字", "qq": "20009"})
    await store.save_member(renamed)

    await _adopt(admin_client, [member.uid])
    player = next(p for p in store.snapshot().players if p.member_uid == member.uid)
    assert player.name == "新名字" and player.qq == "20009"


async def test_unchecking_keeps_the_profile(admin_client):
    """取消勾选只把他移出**名单**，档案留着——删档案会连带断掉赛程与比分。"""
    keep = await _add_member("留下的人")
    drop = await _add_member("被取消的人")
    await _adopt(admin_client, [keep.uid, drop.uid])
    cfg = store.snapshot()
    drop_player = next(p for p in cfg.players if p.member_uid == drop.uid)

    res = await _adopt(admin_client, [keep.uid])
    assert res.status_code == 200, res.text
    cfg = store.snapshot()
    assert logic.is_selected(cfg, drop_player.id) is False, "取消勾选后不该还在名单里"
    assert any(p.id == drop_player.id for p in cfg.players), "但档案必须还在（赛程要用）"


async def test_loose_players_are_not_squeezed_out(admin_client):
    """手工登记的客串选手（没有关联成员）不会因为一次勾选被挤出名单。"""
    member = await _add_member("有账号的人")
    loose = await admin_client.post(
        "/api/players", json={"name": "没账号的客串", "uuid": "GUEST-1"}
    )
    assert loose.status_code == 200, loose.text
    loose_id = loose.json()["player"]["id"]

    await _adopt(admin_client, [member.uid], player_ids=[loose_id])
    cfg = store.snapshot()
    assert logic.is_selected(cfg, loose_id) is True, "一起提交的客串选手要保留在名单里"


# --------------------------------------------------------------------------- #
# 权限与锁定
# --------------------------------------------------------------------------- #
async def test_candidates_need_login(client):
    assert (await client.get("/api/roster/members")).status_code == 401


async def test_locked_event_refuses_changes(admin_client):
    """比赛开始后名单冻结（与手工勾选手同一道闸门）。"""
    member = await _add_member("锁定后想加的人")
    await store.mutate(lambda data: {**data, "event": {**data.get("event", {}), "locked": True}})
    try:
        res = await _adopt(admin_client, [member.uid])
        assert res.status_code == 409, res.text
        assert "锁定" in res.text
    finally:
        await store.mutate(
            lambda data: {**data, "event": {**data.get("event", {}), "locked": False}}
        )
