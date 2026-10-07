"""存储层：字段往返、旧库升级、操作日志。

这里的重点是**新字段能否真的存回来**：数据库映射漏一个键不会报错，
只会安静地退回默认值——那种 bug 只有往返测试抓得住。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import db
from app.models import UiConfig
from app.store import store


def test_data_dir_is_isolated():
    """整套测试必须跑在临时目录里，绝不碰仓库里的 config/nte.sqlite3。"""
    root = Path(os.environ["NTE_DATA_DIR"]).resolve()
    assert root in Path(store._db_path).resolve().parents or str(store._db_path).startswith(str(root))
    assert root != Path.cwd() / "config"


def test_rules_and_ui_columns_round_trip(tmp_path, make_config):
    """计分三件套 / metric / og_image 存进去要能原样读出来。"""
    path = tmp_path / "round.sqlite"
    db.init_db(path)
    cfg = make_config(value_type="time", value_label="用时", better="low")
    cfg.ui = UiConfig(og_image="/static/share.png")

    with db.connect(path) as conn:
        db.save_event(conn, "e001", cfg.dump())
        conn.commit()
        back = db.load_event(conn, "e001")

    assert back["rules"]["valueType"] == "time"
    assert back["rules"]["valueLabel"] == "用时"
    assert back["rules"]["better"] == "low"
    assert back["rules"]["metric"] == "time", "旧口径名跟着同步，老版本读得回来"
    assert back["ui"]["ogImage"] == "/static/share.png"


def test_roster_flag_round_trips(tmp_path, make_config):
    """「名单是显式指定过的」这一位要存得住（``participantsSet``）。

    它只决定一件事：**空名单**到底是「一份空名单」还是「没指定 → 全员参与」。
    以前这一位只活在内存里，于是重启之后「全不选后保存」会变回全员参与；
    自助报名取消到最后一个人时也一样——名单空了就等于没取消。
    """
    from app.models import Config

    path = tmp_path / "roster.sqlite"
    db.init_db(path)
    cfg = make_config()
    cfg.participants = []
    cfg.participants_set = True

    with db.connect(path) as conn:
        db.save_event(conn, "e001", cfg.dump())
        conn.commit()
        back = db.load_event(conn, "e001")

    assert back["participantsSet"] is True
    assert Config.model_validate(back).participants_set is True


def test_old_database_gets_new_columns(tmp_path):
    """旧库升级路径：缺列 → 跑一次 init_db → 列补齐（不需要手工迁移）。"""
    path = tmp_path / "old.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("ALTER TABLE event_rules DROP COLUMN value_type")
        conn.execute("ALTER TABLE event_rules DROP COLUMN value_label")
        conn.execute("ALTER TABLE event_rules DROP COLUMN better")
        conn.execute("ALTER TABLE event_ui DROP COLUMN accent_custom")
        conn.execute("ALTER TABLE event_ui DROP COLUMN og_image")
        conn.execute("ALTER TABLE events DROP COLUMN participants_set")
        conn.commit()

    db.init_db(path)  # = 服务启动时走的那条路

    with db.connect(path) as conn:
        rules_cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_rules)")}
        ui_cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_ui)")}
        event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    assert {"value_type", "value_label", "better", "metric"} <= rules_cols
    assert {"accent_custom", "og_image"} <= ui_cols
    assert "participants_set" in event_cols


def test_retired_columns_are_dropped(tmp_path):
    """已移除的概念（替补类别 / 系列赛 BO）对应的列会被清掉，不留死列。"""
    path = tmp_path / "retired.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("ALTER TABLE event_rules ADD COLUMN include_substitutes INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE event_rules ADD COLUMN best_of INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    db.init_db(path)

    with db.connect(path) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_rules)")}
    assert "include_substitutes" not in cols
    assert "best_of" not in cols


@pytest.mark.parametrize(
    ("legacy_metric", "expected"),
    [
        # 老数据的两种取值必须各自映射回历史行为：映射错了，名次会集体翻转
        ("score", ("integer", "得分", "high")),
        ("time", ("time", "用时", "low")),
    ],
)
def test_old_event_with_only_metric_is_derived(tmp_path, make_config, legacy_metric, expected):
    """只有旧口径 ``metric`` 的库：读回来要自动拆成三件套，且数值一个字节不动。"""
    from app.models import Config

    path = tmp_path / f"legacy-{legacy_metric}.sqlite"
    db.init_db(path)
    cfg = make_config()
    with db.connect(path) as conn:
        db.save_event(conn, "legacy", cfg.dump())
        # 模拟「这三个字段出现之前的库」：值退回默认，只剩旧口径 metric
        conn.execute("UPDATE event_rules SET value_type = '', value_label = '', better = ''")
        conn.execute("UPDATE event_rules SET metric = ?", (legacy_metric,))
        conn.commit()
        back = db.load_event(conn, "legacy")

    rules = Config.model_validate(back).rules
    assert (rules.value_type, rules.value_label, rules.better) == expected
    assert rules.metric == legacy_metric


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
