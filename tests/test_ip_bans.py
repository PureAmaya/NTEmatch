"""封禁 IP 列表：人工封禁、永久、改时长、分页 / 搜索 / 过滤。

这张表是「出事时要用」的东西，所以判据都要硬：

* **人工封禁优先于白名单**：管理员点了封禁却拦不住，比没有这个功能更糟；
* 自动封禁仍然尊重白名单（别把自己人误伤进去）；
* 搜索 / 过滤 / 分页在**服务端**做（被攻击时可能有成百上千条）。
"""

from __future__ import annotations

import time

import pytest

from app import login_guard
from app.login_guard import DEFAULT_SETTINGS

SETTINGS = dict(DEFAULT_SETTINGS, whitelist="", enabled=True)


@pytest.fixture(autouse=True)
def _clean_bans():
    """封禁状态是模块级单例：每个用例前后都清干净，免得互相串。"""
    login_guard.clear()
    yield
    login_guard.clear()


def test_manual_ban_temporary_and_permanent():
    assert login_guard.ban("203.0.113.7", 60, reason="乱试密码") is True
    assert login_guard.blocked_seconds("203.0.113.7", SETTINGS) > 0
    # 同一条 IP 再来一次是「改」不是「新增」
    assert login_guard.ban("203.0.113.7", 0, reason="永久封禁") is False
    assert login_guard.blocked_seconds("203.0.113.7", SETTINGS) == login_guard.PERMANENT_RETRY_AFTER

    page = login_guard.bans_page()
    assert page["total"] == 1
    row = page["items"][0]
    assert row["ip"] == "203.0.113.7"
    assert row["permanent"] is True and row["reason"] == "永久封禁"
    assert row["until"] == "", "永久封禁没有到期时间"


def test_unban_and_clear():
    login_guard.ban("203.0.113.1", 60)
    login_guard.ban("203.0.113.2", 60)
    assert login_guard.unban("203.0.113.1") is True
    assert login_guard.unban("203.0.113.1") is False, "解封两次不该报错，只是没东西可解"
    assert login_guard.bans_page()["total"] == 1
    assert login_guard.clear() == 1
    assert login_guard.bans_page()["total"] == 0


def test_bad_ip_is_rejected():
    for bad in ("", "   ", "not-an-ip", "192.168.1.0/24", "999.1.1.1"):
        with pytest.raises(ValueError):
            login_guard.ban(bad, 60)


def test_manual_ban_beats_the_whitelist():
    """白名单挡的是「自动封禁」，不该挡管理员的手动封禁。"""
    settings = dict(SETTINGS, whitelist="203.0.113.9")
    login_guard.ban("203.0.113.9", 300, reason="手动封禁")
    assert login_guard.blocked_seconds("203.0.113.9", settings) > 0, "手动封禁必须生效"


def test_whitelist_still_protects_from_auto_bans():
    """白名单仍然挡自动封禁（别把自己人误伤），失败计数照常清空。"""
    settings = dict(SETTINGS, whitelist="203.0.113.9", maxAttempts=2, banSeconds=600)
    for _ in range(5):
        assert login_guard.record_failure("203.0.113.9", settings) == 0
    assert login_guard.blocked_seconds("203.0.113.9", settings) == 0
    assert login_guard.bans_page()["total"] == 0


def test_auto_ban_after_repeated_failures():
    settings = dict(SETTINGS, maxAttempts=3, banSeconds=600, windowSeconds=300)
    assert login_guard.record_failure("198.51.100.5", settings) == 0
    assert login_guard.record_failure("198.51.100.5", settings) == 0
    assert login_guard.record_failure("198.51.100.5", settings) == 600
    row = login_guard.bans_page()["items"][0]
    assert row["auto"] is True and "失败" in row["reason"]
    # 登录成功会把这个 IP 的封禁与计数一起清掉
    login_guard.record_success("198.51.100.5")
    assert login_guard.blocked_seconds("198.51.100.5", settings) == 0


def test_bans_page_search_filter_and_paging():
    login_guard.ban("203.0.113.1", 60, reason="爬接口")
    login_guard.ban("203.0.113.2", 3600, reason="爬接口")
    login_guard.ban("198.51.100.9", 0, reason="扫描器")
    for _ in range(3):
        login_guard.record_failure(
            "192.0.2.50", dict(SETTINGS, maxAttempts=3, banSeconds=600, windowSeconds=300)
        )

    assert login_guard.bans_page()["total"] == 4
    # 过滤：永久 / 临时 / 自动 / 人工
    assert login_guard.bans_page(kind="permanent")["total"] == 1
    assert login_guard.bans_page(kind="temp")["total"] == 3
    assert login_guard.bans_page(kind="auto")["total"] == 1
    assert login_guard.bans_page(kind="manual")["total"] == 3
    # 搜索：IP 片段与原因文字都能命中
    assert login_guard.bans_page(query="203.0.113")["total"] == 2
    assert login_guard.bans_page(query="爬接口")["total"] == 2
    assert login_guard.bans_page(query="不存在")["total"] == 0
    # 分页：每页 3 条 → 2 页；永久封禁排在最前（最该先处理的先看到）
    first = login_guard.bans_page(size=3, page=1)
    assert first["pages"] == 2 and len(first["items"]) == 3
    assert first["items"][0]["permanent"] is True
    second = login_guard.bans_page(size=3, page=2)
    assert len(second["items"]) == 1
    assert login_guard.bans_page(page=99)["items"] == [], "越界页回空列表而不是报错"


def test_expired_bans_are_gone_from_the_list():
    login_guard.ban("203.0.113.77", 1)  # 1 秒
    time.sleep(1.1)
    assert login_guard.bans_page()["total"] == 0, "过期的封禁不该还挂在列表里"
    assert login_guard.blocked_seconds("203.0.113.77", SETTINGS) == 0


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
async def test_bans_api_needs_server_admin(client, admin_client):
    assert (await client.get("/api/server/bans")).status_code == 401

    resp = await admin_client.put(
        "/api/server/bans", json={"ip": "203.0.113.33", "seconds": 120, "reason": "测试"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True

    listed = (await admin_client.get("/api/server/bans", params={"q": "203.0.113.33"})).json()
    assert listed["total"] == 1
    assert listed["items"][0]["remaining"] > 0

    # 改时长：同一条 IP 再 PUT 一次 = 覆盖（不是新增）
    again = await admin_client.put(
        "/api/server/bans", json={"ip": "203.0.113.33", "seconds": 0, "reason": "升级为永久"}
    )
    assert again.json()["created"] is False
    assert (await admin_client.get("/api/server/bans")).json()["items"][0]["permanent"] is True

    assert (await admin_client.delete("/api/server/bans/203.0.113.33")).json()["removed"] is True
    assert (await admin_client.get("/api/server/bans")).json()["total"] == 0


async def test_bans_api_rejects_a_bad_ip(admin_client):
    resp = await admin_client.put("/api/server/bans", json={"ip": "不是IP", "seconds": 60})
    assert resp.status_code == 400
    assert "IP" in resp.json()["error"]
