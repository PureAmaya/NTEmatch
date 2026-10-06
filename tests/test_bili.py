"""B站直播：只探开播状态 + 直嵌官方播放器。

这一块的性质和 MediaMTX 那套完全不同：**视频流一概不经本站**
（不中继、不转码、不代理），所以这里要钉住的其实是三件容易做错的小事：

1. 房间号的录入容错（粘链接 / 带文字 / 纯数字都收，非数字要报错而不是静默清空）；
2. 探测**只读缓存**且失败**不影响别的功能**（B站 挂了，直播页其余部分照常）；
3. 下发给前端的必须是「播放器地址 + 跳转地址」，而且**成员填过房间号这件事本身**
   该出现在公开视图里（它就是公开信息，观众要靠它跳转）。

网络一律打桩：真实请求会在 CI 里变成 flaky（B站 有风控、也可能不可达）。
"""

from __future__ import annotations

import httpx
import pytest

from app import live
from app.auth import auth
from app.main import app
from app.models import Member, StreamConfig
from app.store import store


# --------------------------------------------------------------------------- #
# 打桩：把「B站 接口」换成本地的一张表
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """只实现 ``get``：``responses`` 是 房间号 → 响应体 / 异常。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        room = str((params or {}).get("room_id") or "")
        self.calls.append(room)
        item = self.responses.get(room)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return _FakeResponse({"code": -400, "message": "房间不存在"})
        return _FakeResponse(item)  # type: ignore[arg-type]


def _room_payload(
    *,
    live_status: int,
    title: str = "上分中",
    uname: str = "主播",
    room: str = "12345",
    online: int = 321,
    area: str = "单机游戏",
    live_time: str = "1780000000",
):
    """一份 ``get_info`` 应答：字段名与 B站 实际返回一致（含标题 / 在线 / 分区 / 开播时间）。"""
    return {
        "code": 0,
        "data": {
            "room_id": int(room),
            "short_id": 0,
            "uid": 9001,
            "live_status": live_status,
            "title": title,
            "uname": uname,
            "online": online,
            "area_name": area,
            "live_time": live_time if live_status == 1 else "0",
        },
    }


@pytest.fixture(autouse=True)
def _fresh_bili_cache():
    """每个用例前把 B站 缓存清干净，免得互相看到上一轮的探测结果。"""
    blank = {"at": float("-inf"), "known": False, "rooms": {}, "missing": {}, "reason": ""}
    live._bili_cache.update(blank)
    yield
    live._bili_cache.update(blank)


@pytest.fixture
def stub_bili(monkeypatch):
    """把 ``live._client_get`` 换成假客户端，返回它以便断言「探测了几次」。"""

    def install(responses: dict[str, object]) -> _FakeClient:
        client = _FakeClient(responses)
        monkeypatch.setattr(live, "_client_get", lambda verify=True: client)
        return client

    return install


async def _member_with_bili(uid: str = "m1", room: str = "12345", name: str = "甲") -> Member:
    member, _key, _bearer = await store.save_member(Member(uid=uid, name=name, bili_room=room))
    return member


# --------------------------------------------------------------------------- #
# 房间号的录入
# --------------------------------------------------------------------------- #
def test_bili_room_accepts_numbers_and_links():
    """粘直播间链接、写「房间号 12345」，都该抠出数字来。"""
    assert Member(bili_room="12345").bili_room == "12345"
    assert Member(bili_room="https://live.bilibili.com/12345").bili_room == "12345"
    assert Member(bili_room="live.bilibili.com/12345?from=search").bili_room == "12345"
    assert Member(bili_room=" 房间号：12345 ").bili_room == "12345"
    assert Member(bili_room="").bili_room == ""


def test_bili_room_rejects_garbage_instead_of_clearing_it():
    """非空却抠不出数字要**报错**：静默清空会让人以为填上了，回来还得猜为什么没生效。"""
    with pytest.raises(ValueError) as err:
        Member(bili_room="我的直播间")
    assert "B站" in str(err.value)


def test_bili_room_is_public():
    """房间号是公开信息（跳转地址里本来就有它）：公开视图要带上，但凭据依旧不带。"""
    data = Member(uid="m1", name="甲", bili_room="12345").public()
    assert data["biliRoom"] == "12345"
    assert "qq" not in data and "keyHash" not in data


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
async def test_probe_marks_live_room(admin_client, stub_bili):
    await _member_with_bili(room="12345")
    stub_bili({"12345": _room_payload(live_status=1)})

    snap = await live.bili_probe(force=True)
    assert snap["known"] is True
    assert [item["uid"] for item in live.bili_view()["items"]] == ["m1"]
    item = live.bili_view()["items"][0]
    assert item["room"] == "12345"
    assert item["title"] == "上分中"
    assert "cid=12345" in item["embed"]
    assert item["jump"].endswith("/12345")


async def test_probe_ignores_offline_and_replay(admin_client, stub_bili):
    """只有 ``live_status == 1`` 才算在播：未开播（0）与轮播（2）都不给机位。"""
    await _member_with_bili(room="111")
    stub_bili({"111": _room_payload(live_status=0)})
    assert (await live.bili_probe(force=True))["known"] is True
    assert live.bili_view()["items"] == []

    live._bili_cache.update({"at": float("-inf"), "known": False, "rooms": {}, "reason": ""})
    stub_bili({"111": _room_payload(live_status=2)})
    assert (await live.bili_probe(force=True))["known"] is True
    assert live.bili_view()["items"] == []


async def test_probe_failure_is_unknown_and_harmless(admin_client, stub_bili):
    """探不到就是「不知道」：不能把它当成「没人播」，也不该把别的直播功能带崩。"""
    await _member_with_bili(room="12345")
    stub_bili({"12345": httpx.ConnectTimeout("boom")})

    snap = await live.bili_probe(force=True)
    assert snap["known"] is False
    assert snap["reason"]
    # 采集「谁在直播」照常返回（B站 只是其中一路）
    collected = await live.collect_live(force=False)
    assert collected["items"] == []


async def test_probe_result_is_cached(admin_client, stub_bili):
    """同一轮里重复查询不该反复敲 B站（有风控）。"""
    await _member_with_bili(room="12345")
    client = stub_bili({"12345": _room_payload(live_status=1)})
    await live.bili_probe(force=True)
    await live.bili_probe()
    await live.bili_probe()
    assert client.calls == ["12345"]


async def test_collect_live_carries_the_bili_room(admin_client, stub_bili):
    """在播时「谁在直播」里要有一路 B站：带上播放器与跳转地址，且没有本站的推流地址。"""
    await _member_with_bili(room="12345", name="甲")
    stub_bili({"12345": _room_payload(live_status=1)})
    await live.bili_probe(force=True)

    collected = await live.collect_live(force=False)
    bili_items = [item for item in collected["items"] if item["kind"] == "bili"]
    assert len(bili_items) == 1
    item = bili_items[0]
    assert item["key"] == "bili:m1"
    assert item["name"] == "甲"
    assert item["play"] == {}          # 不该出现本站的播放地址
    assert item["bili"]["room"] == "12345"
    assert "blackboard/live/live-activity-player.html" in item["bili"]["embed"]


async def test_probe_syncs_title_and_stats(admin_client, stub_bili):
    """开播后**自动同步**标题 / 主播名 / 在线人数 / 分区 / 开播时间——成员什么都不用填。"""
    await _member_with_bili(room="12345", name="甲")
    stub_bili(
        {
            "12345": _room_payload(
                live_status=1, title="排位冲分", uname="阿甲", online=1024, area="单机游戏"
            )
        }
    )
    await live.bili_probe(force=True)
    item = live.bili_view()["items"][0]
    assert item["title"] == "排位冲分"
    assert item["uname"] == "阿甲"
    assert item["online"] == 1024
    assert item["area"] == "单机游戏"
    assert item["liveTime"], "开播时间应当换算成站内时间戳文本"
    assert item["liveTime"].startswith("20"), item["liveTime"]

    # 「谁在直播」里也带上同一份信息（群消息 / 推送用）
    collected = await live.collect_live(force=False)
    bili_item = next(i for i in collected["items"] if i["kind"] == "bili")
    assert bili_item["title"] == "排位冲分"
    assert bili_item["bili"]["online"] == 1024


async def test_short_room_id_uses_the_real_room_for_urls(admin_client, stub_bili):
    """成员填短号也能用：探测按他填的号，但播放器与跳转地址要用 B站 认的真实房间号。"""
    await _member_with_bili(room="6")
    stub_bili({"6": _room_payload(live_status=1, room="21452505")})

    await live.bili_probe(force=True)
    item = live.bili_view()["items"][0]
    assert item["room"] == "6", "成员填的原样保留（显示用）"
    assert item["roomId"] == "21452505"
    assert "cid=21452505" in item["embed"]
    assert item["jump"].endswith("/21452505")


async def test_saving_an_unknown_room_warns_but_still_saves(admin_client, stub_bili):
    """填错的房间号当场提示，但**不拦保存**（否则 B站 抖动就等于不让人存资料）。"""
    stub_bili({})  # 一律回 code=-400「房间不存在」
    res = await admin_client.post("/api/members", json={"name": "甲", "biliRoom": "999"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["member"]["biliRoom"] == "999"
    assert store.member(body["member"]["uid"]).bili_room == "999"
    assert body["warnings"] and "查不到" in body["warnings"][0]


async def test_saving_a_live_room_reports_the_title(admin_client, stub_bili):
    """正在直播时，保存的反馈里直接带上同步到的标题。"""
    stub_bili({"12345": _room_payload(live_status=1, title="排位冲分")})
    res = await admin_client.post("/api/members", json={"name": "甲", "biliRoom": "12345"})
    assert res.status_code == 200, res.text
    assert "排位冲分" in res.json()["warnings"][0]


async def test_unreachable_bili_never_blocks_saving(admin_client, stub_bili):
    """B站 不可达时保存照常成功，只提示「暂时无法确认」。"""
    stub_bili({"12345": httpx.ConnectTimeout("boom")})
    res = await admin_client.post("/api/members", json={"name": "甲", "biliRoom": "12345"})
    assert res.status_code == 200, res.text
    assert store.member(res.json()["member"]["uid"]).bili_room == "12345"
    assert "无法确认" in res.json()["warnings"][0]


async def test_unchanged_room_is_not_rechecked(admin_client, stub_bili):
    """房间号没变时不重复去问 B站（探测要等网络，跟房间号无关的保存不该被拖慢）。"""
    client = stub_bili({"12345": _room_payload(live_status=0)})
    created = (
        await admin_client.post("/api/members", json={"name": "甲", "biliRoom": "12345"})
    ).json()
    calls_after_create = len(client.calls)
    await admin_client.post(
        "/api/members", json={"uid": created["member"]["uid"], "name": "甲改", "biliRoom": "12345"}
    )
    assert len(client.calls) == calls_after_create, "房间号没变，不该再探一次"


def test_embed_and_jump_urls_are_pinned():
    """两个地址的形状钉死：播放器带 ``cid``，跳转是直播间页。"""
    assert live.bili_embed_url("12345").startswith("https://www.bilibili.com/blackboard/live/")
    assert "cid=12345" in live.bili_embed_url("12345")
    assert live.bili_jump_url("12345") == "https://live.bilibili.com/12345"
    assert live.bili_embed_url("") == "" and live.bili_jump_url("") == ""


def test_stream_config_has_no_bili_fields():
    """B站 是**成员**身上的属性，不是站点级直播配置：别把房间号塞进 stream 里。"""
    assert "biliRoom" not in StreamConfig().dump()


# --------------------------------------------------------------------------- #
# 接口：管理员与成员本人两条路都能填
# --------------------------------------------------------------------------- #
async def test_admin_can_set_bili_room(admin_client):
    res = await admin_client.post(
        "/api/members", json={"name": "甲", "biliRoom": "live.bilibili.com/98765"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["member"]["biliRoom"] == "98765"


async def test_member_can_set_own_bili_room(admin_client, stub_bili):
    """成员自己在「我的」页填（``PUT /api/me``）。"""
    created = (
        await admin_client.post("/api/members", json={"name": "乙", "permission": "member"})
    ).json()
    uid = created["member"]["uid"]
    stub_bili({"246810": _room_payload(live_status=0, room="246810")})

    # 用他自己的密钥登录（模拟自助那条路）
    session = auth.issue("乙", uid=uid, name="乙", permission="member")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"X-NTE-Token": session.token},
    ) as client:
        res = await client.put("/api/me", json={"name": "乙", "biliRoom": "246810"})
    assert res.status_code == 200, res.text
    assert res.json()["member"]["biliRoom"] == "246810"
    assert store.member(uid).bili_room == "246810"


async def test_bili_room_can_be_cleared(admin_client):
    """不播 B站 了就清空房间号——保存后这一路从直播页消失。"""
    created = (
        await admin_client.post(
            "/api/members", json={"name": "丙", "biliRoom": "13579", "permission": "member"}
        )
    ).json()
    uid = created["member"]["uid"]
    assert store.member(uid).bili_room == "13579"

    res = await admin_client.post("/api/members", json={"uid": uid, "name": "丙", "biliRoom": ""})
    assert res.status_code == 200, res.text
    assert store.member(uid).bili_room == ""
    assert res.json()["member"]["biliRoom"] == ""


async def test_bot_message_shows_the_bili_link(admin_client, stub_bili):
    """群里那条「当前直播」也要能点到 B站 直播间。

    注意这里的场景是「媒体服务器没配、只有 B站 在播」：不能因为查不到媒体服务器
    就把 B站 那一路也一起藏掉（那是两条独立链路）。
    """
    from app import qqbot

    await _member_with_bili(room="12345", name="甲")
    stub_bili({"12345": _room_payload(live_status=1, title="排位冲分", online=88)})
    await live.bili_probe(force=True)

    info = await live.collect_live(force=False)
    assert info["known"] is False      # 测试环境没配 MediaMTX API
    text = qqbot.build_live_message(info)
    assert "B站直播" in text
    assert "https://live.bilibili.com/12345" in text
    # 群消息也带上同步过来的标题与在线人数
    assert "《排位冲分》" in text
    assert "88 人在看" in text


