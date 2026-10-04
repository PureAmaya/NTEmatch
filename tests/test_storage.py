"""存储层：字段往返、旧库升级、操作日志。

这里的重点是**新字段能否真的存回来**：数据库映射漏一个键不会报错，
只会安静地退回默认值——那种 bug 只有往返测试抓得住。
"""

from __future__ import annotations

import os
from pathlib import Path

from app import db
from app.models import UiConfig
from app.store import store


def test_data_dir_is_isolated():
    """整套测试必须跑在临时目录里，绝不碰仓库里的 config/nte.sqlite3。"""
    root = Path(os.environ["NTE_DATA_DIR"]).resolve()
    assert root in Path(store._db_path).resolve().parents or str(store._db_path).startswith(str(root))
    assert root != Path.cwd() / "config"


def test_rules_and_ui_columns_round_trip(tmp_path, make_config):
    """best_of / metric / accent_custom / og_image 存进去要能原样读出来。"""
    path = tmp_path / "round.sqlite"
    db.init_db(path)
    cfg = make_config(best_of=3, metric="time")
    cfg.ui = UiConfig(accent="violet", accent_custom="#FF6A00", og_image="/static/share.png")

    with db.connect(path) as conn:
        db.save_event(conn, "e001", cfg.dump())
        conn.commit()
        back = db.load_event(conn, "e001")

    assert back["rules"]["bestOf"] == 3
    assert back["rules"]["metric"] == "time"
    assert back["ui"]["accentCustom"] == "#ff6a00"  # 统一小写
    assert back["ui"]["ogImage"] == "/static/share.png"


def test_old_database_gets_new_columns(tmp_path):
    """旧库升级路径：缺列 → 跑一次 init_db → 列补齐（不需要手工迁移）。"""
    path = tmp_path / "old.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("ALTER TABLE event_rules DROP COLUMN best_of")
        conn.execute("ALTER TABLE event_rules DROP COLUMN metric")
        conn.execute("ALTER TABLE event_ui DROP COLUMN accent_custom")
        conn.execute("ALTER TABLE event_ui DROP COLUMN og_image")
        conn.commit()

    db.init_db(path)  # = 服务启动时走的那条路

    with db.connect(path) as conn:
        rules_cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_rules)")}
        ui_cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_ui)")}
    assert {"best_of", "metric"} <= rules_cols
    assert {"accent_custom", "og_image"} <= ui_cols


def test_old_event_without_metric_reads_as_score(tmp_path, make_config):
    """老比赛读回来必须是「计分制」：补列默认值若不是历史行为，名次会集体翻转。"""
    path = tmp_path / "legacy.sqlite"
    db.init_db(path)
    cfg = make_config()
    with db.connect(path) as conn:
        db.save_event(conn, "legacy", cfg.dump())
        # 模拟「这个字段出现之前的库」：值退回默认
        conn.execute("UPDATE event_rules SET metric = 'score'")
        conn.commit()
        back = db.load_event(conn, "legacy")
    assert back["rules"]["metric"] == "score"


def test_notices_table_survives_old_databases(tmp_path):
    """公告表的两条升级路径：整张表不存在（老库）与缺少后加的 seq 列。"""
    path = tmp_path / "notices.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("DROP TABLE notices")  # 模拟「这个功能还不存在」的库
        conn.commit()
    db.init_db(path)
    with db.connect(path) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
    assert {"id", "scope", "event_id", "title", "body", "seq"} <= cols

    with db.connect(path) as conn:
        conn.execute("ALTER TABLE notices DROP COLUMN seq")  # 模拟早期建的表
        conn.commit()
    db.init_db(path)
    with db.connect(path) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
    assert "seq" in cols


async def test_activity_is_recorded_and_trimmed():
    """操作日志能写能读，并且只保留最近若干条（不会无限长）。"""
    db.init_db(store._db_path)
    for i in range(db.ACTIVITY_KEEP + 20):
        await store.log_activity(
            actor="冒烟", method="POST", path=f"/api/smoke/{i}", status=200, ts="2026-01-01 00:00:00"
        )
    rows = await store.activity(db.ACTIVITY_KEEP + 50)
    assert len(rows) == db.ACTIVITY_KEEP
    assert rows[0]["path"] == f"/api/smoke/{db.ACTIVITY_KEEP + 19}"  # 最新的在最前

    # 收尾：别把测试数据留在（临时的）日志里
    with db.connect(store._db_path) as conn:
        conn.execute("DELETE FROM activity WHERE actor = '冒烟'")
        conn.commit()


def test_activity_never_breaks_the_caller(monkeypatch):
    """日志写入失败必须被吞掉：它是观测手段，不能反过来把请求搞挂。"""
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "_record_activity_sync", boom)
    import asyncio

    asyncio.run(store.log_activity(actor="x", method="POST", path="/api/x", status=500))
    assert called["n"] == 1  # 真的试过，而且没有异常冒出去
