"""旧数据快照与计分口径升级。

这一块守的是**升级时的退路**：老库（只有 ``metric``，没有三件套）在第一次启动时
会被自动转成新口径，而**转换之前**必须先落一份原样快照。两件事都不做，或者顺序
反了，用户就失去了「万一转错了还能捞回原始数据」的机会。
"""

from __future__ import annotations

import sqlite3

import pytest

from app import db, legacy
from app.store import store


def _rules_row(event_id: str) -> dict[str, str]:
    with db.connect(store._db_path) as conn:
        row = conn.execute(
            "SELECT metric, value_type, value_label, better FROM event_rules WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return dict(row) if row else {}


def _demote_to_legacy(event_id: str, metric: str = "score") -> None:
    """把某届的规则行改回「这个功能出现之前」的样子（只有旧口径 metric）。"""
    with db.connect(store._db_path) as conn:
        conn.execute(
            "UPDATE event_rules SET value_type = '', value_label = '', better = '', metric = ?"
            " WHERE event_id = ?",
            (metric, event_id),
        )
        conn.commit()


def _legacy_names() -> list[str]:
    return sorted(p.name for p in legacy.LEGACY_DIR.glob("*.zip")) if legacy.LEGACY_DIR.is_dir() else []


@pytest.fixture(autouse=True)
def _clean_legacy_dir():
    """每条用例前后都清掉自己造的旧数据快照（目录在临时数据目录里，隔离的）。"""
    for path in legacy.LEGACY_DIR.glob("*.zip") if legacy.LEGACY_DIR.is_dir() else []:
        path.unlink(missing_ok=True)
    yield
    for path in legacy.LEGACY_DIR.glob("*.zip") if legacy.LEGACY_DIR.is_dir() else []:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 快照本身
# --------------------------------------------------------------------------- #
async def test_snapshot_writes_a_readable_zip():
    """快照里要有完整旧库 + 一份说清「这是什么」的清单。"""
    if not store.current_id:
        await store.start()
    meta = legacy.snapshot(reason="unit", note="测试用")

    assert meta["name"].startswith("nte-legacy-")
    assert meta["size"] > 0
    assert meta["reason"] == "unit"
    assert meta["note"] == "测试用"

    items = legacy.list_snapshots()
    assert [it["name"] for it in items] == [meta["name"]]
    assert legacy.path_of(meta["name"]) is not None


def test_path_traversal_is_rejected():
    """文件名白名单：不能让 `../` 之类的东西读到目录外面去。"""
    assert legacy.path_of("../../etc/passwd") is None
    assert legacy.path_of("..\\..\\nte.sqlite3") is None
    assert legacy.path_of("") is None
    assert legacy.delete("../../etc/passwd") is False


async def test_delete_removes_the_snapshot():
    if not store.current_id:
        await store.start()
    meta = legacy.snapshot(reason="unit")
    assert legacy.delete(meta["name"]) is True
    assert legacy.list_snapshots() == []


# --------------------------------------------------------------------------- #
# 升级：先留快照，再转换
# --------------------------------------------------------------------------- #
async def test_upgrade_snapshots_before_converting():
    """老库升级：先落快照、再把三件套写下来（数值一个字节都不动）。"""
    if not store.current_id:
        await store.start()
    event_id = store.current_id
    await store.update({"rules": {"valueType": "time", "valueLabel": "", "better": "low"}})
    _demote_to_legacy(event_id, metric="time")
    assert _rules_row(event_id)["value_type"] == ""

    assert _legacy_names() == [], "转换之前不该有任何快照"
    done = store.migrate_scoring()
    assert done == 1
    assert len(_legacy_names()) == 1, "转换前必须先留一份旧库快照"

    row = _rules_row(event_id)
    assert (row["value_type"], row["value_label"], row["better"]) == ("time", "用时", "low")
    assert row["metric"] == "time", "旧口径名要跟着同步"


async def test_upgrade_is_idempotent():
    """已经是新口径的届不该被反复「升级」，也不该反复落快照。"""
    if not store.current_id:
        await store.start()
    event_id = store.current_id
    _demote_to_legacy(event_id, metric="score")
    assert store.migrate_scoring() == 1
    assert store.migrate_scoring() == 0
    assert len(_legacy_names()) == 1


async def test_legacy_score_upgrades_to_integer_high():
    """老数据是 score：升级结果必须是「自然数 + 得分 + 数值高胜」（历史行为）。"""
    if not store.current_id:
        await store.start()
    event_id = store.current_id
    _demote_to_legacy(event_id, metric="score")

    assert store.migrate_scoring() == 1
    row = _rules_row(event_id)
    assert (row["value_type"], row["value_label"], row["better"]) == ("integer", "得分", "high")


async def test_snapshot_carries_the_old_numbers():
    """快照里存的是**转换前**的库：三件套为空、只有旧口径。"""
    if not store.current_id:
        await store.start()
    event_id = store.current_id
    await store.update({"rounds": [], "players": []})
    _demote_to_legacy(event_id, metric="time")

    meta = legacy.snapshot(reason="unit")
    path = legacy.path_of(meta["name"])
    assert path is not None
    import zipfile

    with zipfile.ZipFile(path) as zf, zf.open(legacy.DB_MEMBER) as handle:
        tmp = path.with_suffix(".tmp.sqlite")
        tmp.write_bytes(handle.read())
    conn = sqlite3.connect(str(tmp))
    try:
        row = conn.execute(
            "SELECT value_type, metric FROM event_rules WHERE event_id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()  # 不关连接，Windows 上删不掉这个文件
    try:
        assert row[0] == "", "快照要保留转换前的样子（三件套为空）"
        assert row[1] == "time"
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 接口：只读留存，不给还原
# --------------------------------------------------------------------------- #
async def test_api_lists_snapshots_without_a_restore_route(admin_client):
    if not store.current_id:
        await store.start()
    legacy.snapshot(reason="unit", note="接口用例")

    res = await admin_client.get("/api/legacy-backups")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["backups"]) == 1
    assert "不提供还原" in body["note"]

    # 没有还原接口：POST 到同一个地址只能是 405（路由根本不存在）
    res = await admin_client.post("/api/legacy-backups/whatever/restore")
    assert res.status_code == 405
