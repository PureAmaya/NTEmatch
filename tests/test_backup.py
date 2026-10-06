"""备份 / 导出 / 还原：**出事时要救命**的那条路。

为什么单独一组用例：这条路径平时没人跑（一年可能就用一次），而用的时候正好是
最慌的时候——「备份是不是真的能倒回去」「还原会不会把现在的东西弄坏」，
只能靠测试回答。这里把几件要命的事钉住：

* 打出来的 zip 里**确实**有数据库快照与清单（不是个空壳）；
* **还原真的能倒回去**（改过的数据回到备份那一刻）；
* 还原前**自动打一份安全备份**（还原错了还能再倒回来）；
* 还原后**注销全部会话**（成员与凭据都可能已回退，旧会话不该继续有效）；
* 坏文件与路径穿越一律被拒，且**当前数据毫发无损**。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app import backup


@pytest.fixture(autouse=True)
def _clean_backups():
    """用例前后清掉自己打出来的备份（备份目录在临时数据目录里，但仍别互相串）。"""
    def names() -> set[str]:
        return {item["name"] for item in backup.list_backups()}

    before = names()
    yield
    for item in backup.list_backups():
        if item["name"] not in before:
            try:
                backup.delete_backup(item["name"])
            except (ValueError, FileNotFoundError):  # pragma: no cover - 已被别的用例删掉
                pass


async def _site_name(client) -> str:
    return str((await client.get("/api/state")).json().get("siteName") or "")


# --------------------------------------------------------------------------- #
# 备份与导出
# --------------------------------------------------------------------------- #
async def test_backup_is_listed_and_downloadable(admin_client):
    """打一份备份 → 列表里有它 → 下载的 zip 里真的有数据库快照与清单。"""
    res = await admin_client.post("/api/backups")
    assert res.status_code == 200, res.text
    body = res.json()
    name = body["backup"]["name"]
    assert name.endswith(".zip") and name in {item["name"] for item in body["backups"]}

    got = await admin_client.get(f"/api/backups/{name}/download")
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        names = set(zf.namelist())
        assert backup.MANIFEST_MEMBER in names, "备份里要有清单（还原时靠它认格式）"
        assert backup.DB_MEMBER in names, "备份里要有数据库快照——没有它这份备份等于空壳"
        import json

        manifest = json.loads(zf.read(backup.MANIFEST_MEMBER).decode("utf-8"))
        assert manifest["format"] == backup.FORMAT


async def test_backup_name_traversal_is_rejected(admin_client):
    """备份名走白名单：``../`` 这类一律拒（否则能把整库甚至别的文件拖走）。"""
    for bad in ("..%2Fnte.sqlite3", "..%2F..%2Fconfig%2Fnte.sqlite3", "not-a-zip.txt"):
        res = await admin_client.get(f"/api/backups/{bad}/download")
        assert res.status_code in (400, 404), f"{bad} 应当被拒，实际 {res.status_code}"


async def test_backup_endpoints_need_server_admin(client, admin_client):
    """备份能整库拖走，权限只能是服务器管理员。"""
    assert (await client.get("/api/backups")).status_code == 401
    assert (await client.post("/api/backups")).status_code == 401


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #
async def test_restore_brings_the_data_back(admin_client):
    """还原真的能倒回去：备份之后改的东西，还原后回到备份那一刻。"""
    before = await _site_name(admin_client)
    created = await admin_client.post("/api/backups")
    name = created.json()["backup"]["name"]

    changed = await admin_client.put("/api/site/name", json={"name": "还原测试名"})
    assert changed.status_code == 200, changed.text
    assert await _site_name(admin_client) == "还原测试名"

    res = await admin_client.post(f"/api/backups/{name}/restore")
    assert res.status_code == 200, res.text
    assert await _site_name(admin_client) == before, "还原之后应当回到备份那一刻的样子"


async def test_restore_makes_a_safety_backup(admin_client):
    """还原是破坏性操作：动手之前先自动打一份「还原前」的安全备份。"""
    created = await admin_client.post("/api/backups")
    name = created.json()["backup"]["name"]
    res = await admin_client.post(f"/api/backups/{name}/restore")
    body = res.json()
    safety = body.get("safety") or {}
    assert safety.get("name"), "还原前必须先自动打一份安全备份（还原错了还能倒回来）"
    assert safety["name"] in {item["name"] for item in body["backups"]}


async def test_restore_revokes_every_session(admin_client):
    """还原后注销全部会话：成员与凭据可能已经回退，旧会话不该继续有效。"""
    from app.auth import auth

    created = await admin_client.post("/api/backups")
    name = created.json()["backup"]["name"]
    session = auth.issue("restore-test", uid="u-restore", permission="member")
    assert auth.get(session.token) is not None

    res = await admin_client.post(f"/api/backups/{name}/restore")
    body = res.json()
    assert body["reauth"] is True, "要明确告诉前端「必须重新登录」"
    assert auth.get(session.token) is None, "还原后旧会话必须失效"
    assert body["revoked"] >= 1


async def test_bad_upload_is_rejected_without_touching_data(admin_client):
    """上传坏文件：明确报错，且**当前数据一点没动**。"""
    before = await _site_name(admin_client)
    res = await admin_client.post(
        "/api/backups/upload",
        params={"name": "坏文件.zip"},
        content=b"this is not a zip at all",
        headers={"Content-Type": "application/zip"},
    )
    assert res.status_code == 400, res.text
    assert await _site_name(admin_client) == before, "还原失败不能把现在的数据弄坏"


# --------------------------------------------------------------------------- #
# 过时旧数据（升级前的原样留存）
# --------------------------------------------------------------------------- #
async def test_legacy_snapshots_are_read_only(admin_client):
    """旧数据快照：能列、能下载，但**故意不提供还原**。

    把旧结构塞回新版本只会得到一份读不动的数据，比没有更危险（见 app/legacy.py）。
    """
    listed = await admin_client.get("/api/legacy-backups")
    assert listed.status_code == 200
    body = listed.json()
    assert "backups" in body, "至少要能列出快照（干净库为空也正常）"

    res = await admin_client.post("/api/legacy-backups/whatever.zip/restore")
    assert res.status_code in (404, 405), "旧数据快照不该有还原入口"
