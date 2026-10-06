"""旧数据快照：**升级前**的原样留存，只供下载，不提供还原。

与全站备份（:mod:`app.backup`）的区别只有一句话：备份是「能还原回去的现在」，
这里是「新版本已经读不动的过去」。因此：

* 真正检测到旧结构时**自动**落一份，之后不再重复（条件本身就是标记）；
* **不提供还原入口**：把旧结构塞回新版本只会得到一份读不动的数据，反而更危险；
* 与正式备份**分开存放**（``backups/legacy/``），免得被「自动清理旧备份」顺手删掉；
* 解压出来就是一份完整的旧库（``config/nte.sqlite3``），用任何 SQLite 工具都能打开。

它存在的唯一理由：万一新版本的自动转换把某届读错了，原始数据还在。
"""

from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .backup import BACKUP_DIR, snapshot_db
from .logging_conf import get_logger

log = get_logger("legacy")

#: 旧数据快照目录（与正式备份同级，但独立子目录）
LEGACY_DIR = BACKUP_DIR / "legacy"

#: 快照格式标识：写进 manifest，便于以后认出来源
FORMAT = "nte-legacy/1"
MANIFEST_MEMBER = "manifest.json"
#: 与正式备份用同一个成员名：解压出来就是一份可直接打开的旧库
DB_MEMBER = "config/nte.sqlite3"

#: 文件名白名单：只允许我们自己生成的形态，杜绝路径穿越
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,118}\.zip$")


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005


def _safe_reason(reason: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (reason or "upgrade").strip().lower()).strip("-")
    return slug[:24] or "upgrade"


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()  # noqa: DTZ005


def _counts(db_path: Path) -> dict[str, int]:
    """旧库里有多少届 / 多少选手 / 多少场比赛（面板上要显示「这份东西里有什么」）。

    注意 ``sqlite3.connect()`` 的 ``with`` 只管事务、**不关连接**：这里必须显式
    ``close()``，否则 Windows 上文件一直被占着，后面的 ``unlink`` 会直接失败。
    """
    out: dict[str, int] = {}
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        for key, sql in (
            ("events", "SELECT COUNT(*) FROM events"),
            ("players", "SELECT COUNT(*) FROM players"),
            ("rounds", "SELECT COUNT(*) FROM rounds"),
        ):
            try:
                out[key] = int(conn.execute(sql).fetchone()[0])
            except (sqlite3.Error, TypeError, IndexError):
                out[key] = 0
    except sqlite3.Error as exc:
        log.warning("统计旧库内容失败（忽略）| %s", exc)
        return {"events": 0, "players": 0, "rounds": 0}
    finally:
        if conn is not None:
            conn.close()
    return out


def snapshot(*, reason: str = "upgrade", note: str = "") -> dict[str, Any]:
    """把**当前**数据库原样打一份快照。返回它的元信息。

    这是升级路径上的最后一个保险：调用方必须在**任何转换写入之前**调用它。
    """
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)
    name = f"nte-legacy-{_stamp()}-{_safe_reason(reason)}.zip"
    target = LEGACY_DIR / name
    tmp_db = LEGACY_DIR / f".{name}.tmpdb"
    from .store import store

    try:
        snapshot_db(store.path, tmp_db)
        manifest = {
            "format": FORMAT,
            "app": "nte-match",
            "createdAt": _now_iso(),
            "reason": reason or "upgrade",
            "note": note,
            "readonly": True,
            "hint": "升级前的旧数据快照：仅供留存与下载，当前版本不提供还原",
            "database": DB_MEMBER,
            **_counts(tmp_db),
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, DB_MEMBER)
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest, ensure_ascii=False, indent=2))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    meta = describe(target)
    log.warning(
        "已留存旧数据快照 | %s | %.1f KB | 原因=%s | 届=%d 选手=%d 比赛=%d",
        meta["name"],
        meta["size"] / 1024,
        meta["reason"],
        meta["events"],
        meta["players"],
        meta["rounds"],
    )
    return meta


def describe(path: Path) -> dict[str, Any]:
    """一个快照文件的元信息（清单读不出来时也要能列表，不抛异常）。"""
    meta: dict[str, Any] = {
        "name": path.name,
        "size": path.stat().st_size if path.is_file() else 0,
        "createdAt": "",
        "reason": "",
        "note": "",
        "events": 0,
        "players": 0,
        "rounds": 0,
    }
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
        for key in ("createdAt", "reason", "note", "events", "players", "rounds"):
            if key in manifest:
                meta[key] = manifest[key]
    except (OSError, KeyError, ValueError) as exc:
        log.warning("旧数据快照的清单读不出来 | %s | %s", path.name, exc)
        # 本地无时区（与站内其它时间戳一致），因此不传 tz
        stamp = datetime.fromtimestamp(path.stat().st_mtime)  # noqa: DTZ006
        meta["createdAt"] = stamp.replace(microsecond=0).isoformat()
    return meta


def list_snapshots() -> list[dict[str, Any]]:
    """全部旧数据快照（新的在前）。"""
    if not LEGACY_DIR.is_dir():
        return []
    items = [describe(path) for path in LEGACY_DIR.glob("*.zip") if path.is_file()]
    return sorted(items, key=lambda item: str(item.get("name") or ""), reverse=True)


def path_of(name: str) -> Path | None:
    """按文件名取快照路径；名字不合法或文件不存在返回 ``None``（防路径穿越）。"""
    if not _NAME_RE.match(name or ""):
        return None
    path = LEGACY_DIR / name
    return path if path.is_file() else None


def delete(name: str) -> bool:
    """删除一份旧数据快照。"""
    path = path_of(name)
    if path is None:
        return False
    path.unlink(missing_ok=True)
    log.warning("已删除旧数据快照 | %s", name)
    return True
