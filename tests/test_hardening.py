"""加固项与出厂默认值。

这一组盯的是「不该发生的事」——它们都不会报错、不会崩，只是安静地开着一扇门：

* 会话令牌**只认请求头**：否则一个 ``?token=…`` 链接就能改数据 / 下载整库
  （查询串还会原样进反向代理与 CDN 的访问日志）；
* 健康检查是公开探针，但**不能顺带把名单漏出去**（配置自检里会说到具体选手名）；
* 保存队伍时「没有成员的分组」自动消失（接口层也要兜住，不能只靠前端过滤）；
* 新一届的「赛事信息」出厂为空——规则由「比赛规则」面板按赛制自动生成，
  预填一段通则只会在改了赛制之后变成假话；
* 直播**没有总开关**：只要有赛事就允许直播（那个假开关只会让人以为推流被拦住了）。
"""

from __future__ import annotations

import httpx
import pytest

from app import db
from app.auth import auth
from app.defaults import default_config
from app.main import app
from app.models import StreamConfig
from app.store import store


@pytest.fixture
async def anon_client():
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    ) as c:
        yield c


def _with_token(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"X-NTE-Token": token},
    )


@pytest.fixture(autouse=True)
async def _started_store():
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    yield


# --------------------------------------------------------------------------- #
# 会话令牌：只认请求头
# --------------------------------------------------------------------------- #
async def test_session_token_is_rejected_from_the_query_string(anon_client):
    """``?token=`` 一律不认（GET 也不认）：查询串会进日志，而且一个链接就能动手。"""
    token = auth.issue("测试会话").token

    # 读接口：带对了令牌也必须 401
    res = await anon_client.get(f"/api/private?token={token}")
    assert res.status_code == 401, "查询串里的会话令牌不该被接受"

    # 写接口同理（这条以前是唯一的口子：一个链接就能改数据）
    res = await anon_client.put(f"/api/teams?token={token}", json={"teams": []})
    assert res.status_code == 401

    # 同样的令牌走请求头就正常
    async with _with_token(token) as client:
        ok = await client.get("/api/private")
        assert ok.status_code == 200


async def test_health_issues_need_an_admin_session(anon_client, admin_client):
    """健康检查公开（容器探针要打），但配置自检只在管理端会话下回。"""
    await store.set_participants([], actor="test")  # 显式空名单 → 一定有不通过项

    public = (await anon_client.get("/api/health")).json()
    assert public["ok"] is True and public["issues"] == []

    admin = (await admin_client.get("/api/health")).json()
    assert admin["issues"], "管理端应当看到配置自检结果"

    await store.set_participants([], actor="test")


# --------------------------------------------------------------------------- #
# 队伍：没有成员的分组自动删除
# --------------------------------------------------------------------------- #
async def test_teams_without_members_are_dropped(admin_client):
    """保存队伍时空分组自动消失（接口层兜底），并如实回报清理了几个。"""
    await store.update(
        {
            "players": [
                {"id": "p1", "name": "甲"},
                {"id": "p2", "name": "乙"},
            ],
            "participants": ["p1", "p2"],
            "rounds": [],
            "teams": [],
        },
        actor="test",
    )
    res = await admin_client.put(
        "/api/teams",
        json={
            "teams": [
                {"id": "t1", "name": "甲队", "playerIds": ["p1", "p2"]},
                {"id": "t2", "name": "空队", "playerIds": []},
            ]
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 1
    assert body["droppedEmpty"] == 1
    assert [t["id"] for t in body["teams"]] == ["t1"]


# --------------------------------------------------------------------------- #
# 出厂默认：赛事信息为空 / 直播没有总开关
# --------------------------------------------------------------------------- #
def test_new_event_has_no_prefilled_info():
    """赛事信息出厂留空：规则由赛制自动生成，这里只说组织者想补充的话。"""
    assert default_config()["event"]["rulesText"] == ""


async def test_new_event_keeps_info_empty(admin_client):
    cfg = await store.create_event("下一届", copy_roster=True)
    assert cfg.event.rules_text == ""


def test_live_has_no_master_switch():
    """直播配置里没有「启用」这个字段了；``enabled`` 只是恒真的只读属性。"""
    stream = StreamConfig()
    assert "enabled" not in stream.dump()
    assert stream.enabled is True


async def test_live_stays_on_even_if_someone_patches_enabled(admin_client, anon_client):
    """就算有人硬塞 ``stream.enabled=false``（老前端 / 手写请求），直播也不会被关掉。"""
    res = await admin_client.put("/api/config", json={"stream": {"enabled": False}})
    assert res.status_code == 200
    state = (await anon_client.get("/api/state")).json()
    assert state["live"]["enabled"] is True
    assert "enabled" not in state["stream"]
