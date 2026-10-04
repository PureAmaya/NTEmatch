"""全站数据备份：打包 / 列举 / 下载 / 上传还原 / 周期性自动备份。

一份备份就是一个 zip::

    manifest.json          备份元信息（格式版本 / 时间 / 原因 / 内容清单）
    config/nte.sqlite3     数据库快照（用 SQLite 官方 backup API 取一致性快照，
                           WAL 里尚未落盘的内容也一并带上）
    data/avatars/**        本地上传的头像

QQ 头像缓存（``data/avatar_cache``）**不进备份**：它可再生，下次请求自己补回来，
装进来只会白白撑大体积。

几点设计取舍：

* 备份是 **zip + 数据库快照**，不是「拷文件」——WAL 模式下直接复制 ``.sqlite3``
  会丢掉最近提交的事务，所以一律走 ``sqlite3.Connection.backup()``；
* 还原会**覆盖**当前数据，因此还原前会先自动打一份「还原前」的安全备份；
* 备份设置存在 ``backups/settings.json``（**不入库**）：这样还原一份旧备份
  不会把你现在的备份策略一起倒回去；
* 还原后内存状态由 ``store.reload_all()`` 重新载入，并且**注销全部会话**
  （成员凭据可能已经变了，旧会话不该继续有效）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import avatars, db
from .logging_conf import get_logger
from .store import BACKUP_ROOT, store

log = get_logger("backup")

# 备份文件存放目录（默认项目根下的 backups/，与 config/ data/ 平级；
# 由 NTE_DATA_DIR 统一挪走后也跟着走，见 store.DATA_ROOT）。
# 设置文件与备份文件同目录（``backups/settings.json``），因此改这一个常量就够了。
BACKUP_DIR = BACKUP_ROOT


def settings_path() -> Path:
    """备份设置文件路径（与备份同目录，**不入库**）。"""
    return BACKUP_DIR / "settings.json"

# 备份格式标识：manifest.format 必须与它相等，避免把别的 zip 当备份还原
FORMAT = "nte-backup/1"
# 上传体积上限（整站数据很小，256 MB 已是极宽松的兜底）
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# 备份数据库在 zip 里的路径
DB_MEMBER = "config/nte.sqlite3"
MANIFEST_MEMBER = "manifest.json"

# 文件名白名单：只允许我们自己生成的形态，杜绝路径穿越
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,118}\.zip$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,       # 是否开启周期性自动备份
    "intervalHours": 24,    # 间隔（小时）
    "keep": 7,              # 最多保留多少份（超出的按时间从旧到新删除）
    "lastRunAt": "",        # 上次自动备份完成时间（ISO）
    "lastFile": "",         # 上次自动备份文件名
}
SETTINGS_KEYS = tuple(DEFAULT_SETTINGS)


# --------------------------------------------------------------------------- #
# 设置（存在 backups/settings.json，不入库）
# --------------------------------------------------------------------------- #
def load_settings() -> dict[str, Any]:
    """读备份设置；文件缺失 / 坏掉一律回落到默认值。"""
    settings = dict(DEFAULT_SETTINGS)
    try:
        raw = settings_path().read_text("utf-8")
    except FileNotFoundError:
        return settings
    except OSError as exc:
        log.warning("备份配置读取失败，已回落到默认值 | %s", exc)
        return settings
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("备份配置无法解析，已回落到默认值")
        return settings
    if isinstance(data, dict):
        settings.update({k: v for k, v in data.items() if k in SETTINGS_KEYS})
    return settings


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """原子写回备份设置（先写临时文件再改名，避免写坏）。"""
    clean = {k: settings.get(k, DEFAULT_SETTINGS[k]) for k in SETTINGS_KEYS}
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, target)
    return clean


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """按 patch 更新设置（只认已知键，越界值一律夹紧）。"""
    patch = patch or {}
    clean: dict[str, Any] = {}
    if patch.get("enabled") is not None:
        clean["enabled"] = bool(patch["enabled"])
    if patch.get("intervalHours") is not None:
        clean["intervalHours"] = max(1, min(24 * 30, int(patch["intervalHours"])))
    if patch.get("keep") is not None:
        clean["keep"] = max(1, min(200, int(patch["keep"])))
    if not clean:
        raise ValueError("没有需要修改的配置项")
    settings = save_settings({**load_settings(), **clean})
    log.warning("备份设置已更新 | %s", clean)
    return settings


def _parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)  # 与站内其它时间一致，用本地无时区
    except ValueError:
        return None


def next_run_at(settings: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """下一次自动备份时间（未开启 / 已过期待跑则回空串或「尽快」）。"""
    settings = settings or load_settings()
    if not settings.get("enabled"):
        return ""
    last = _parse_iso(str(settings.get("lastRunAt") or ""))
    if last is None:
        return (now or datetime.now()).replace(microsecond=0).isoformat()  # noqa: DTZ005  (本地时间)
    target = last + timedelta(hours=max(1, int(settings.get("intervalHours") or 1)))
    return target.replace(microsecond=0).isoformat()


def is_due(settings: dict[str, Any], now: datetime | None = None) -> bool:
    """现在该不该跑一次自动备份。"""
    if not settings.get("enabled"):
        return False
    now = now or datetime.now()  # noqa: DTZ005
    last = _parse_iso(str(settings.get("lastRunAt") or ""))
    if last is None:
        return True
    return now - last >= timedelta(hours=max(1, int(settings.get("intervalHours") or 1)))


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #
def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005


def _safe_reason(reason: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (reason or "manual").strip().lower()).strip("-")[:24] or "manual"


def snapshot_db(src: Path, dst: Path) -> None:
    """用 SQLite 在线备份 API 取一致性快照（含 WAL 中未回写的已提交事务）。"""
    if not src.is_file():
        raise ValueError(f"数据库文件不存在：{src}")
    dst.unlink(missing_ok=True)
    source = sqlite3.connect(str(src), timeout=10.0)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def _avatar_files() -> list[Path]:
    root = avatars.AVATAR_DIR
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*")) if p.is_file()]


def create_backup(reason: str = "manual") -> dict[str, Any]:
    """打一份全站数据备份，返回它的元信息（同步阻塞，调用方放线程里跑）。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"nte-{_stamp()}-{_safe_reason(reason)}.zip"
    target = BACKUP_DIR / name
    tmp_db = BACKUP_DIR / f".{name}.tmpdb"
    try:
        snapshot_db(store.path, tmp_db)
        root = avatars.AVATAR_DIR
        files = _avatar_files()
        manifest = {
            "format": FORMAT,
            "createdAt": now_iso(),
            "reason": reason or "manual",
            "database": DB_MEMBER,
            "avatars": len(files),
            "app": "nte-match",
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, DB_MEMBER)
            for path in files:
                zf.write(path, f"data/avatars/{path.relative_to(root).as_posix()}")
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest, ensure_ascii=False, indent=2))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    meta = describe(target)
    log.warning(
        "已创建数据备份 | %s | %.1f KB | 原因=%s | 头像=%d",
        meta["name"],
        meta["size"] / 1024,
        reason,
        meta["avatars"],
    )
    return meta


def now_iso() -> str:
    """本地时区的 ISO 秒级时间戳（与站内其它时间戳同格式）。"""
    return datetime.now().replace(microsecond=0).isoformat()  # noqa: DTZ005


# --------------------------------------------------------------------------- #
# 列举 / 校验 / 删除
# --------------------------------------------------------------------------- #
def describe(path: Path) -> dict[str, Any]:
    """把一个备份文件描述成前端要的结构（清单读不出来也不影响列举）。"""
    manifest = read_manifest(path)
    stat = path.stat()
    created = manifest.get("createdAt") or datetime.fromtimestamp(  # noqa: DTZ006  (本地时间)
        stat.st_mtime
    ).replace(microsecond=0).isoformat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "createdAt": created,
        "reason": manifest.get("reason") or "",
        "avatars": int(manifest.get("avatars") or 0),
        "ok": bool(manifest),
    }


def read_manifest(path: Path) -> dict[str, Any]:
    """读备份清单；不是本站备份 / 读不动就回空字典。"""
    try:
        with zipfile.ZipFile(path) as zf:
            data = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def list_backups() -> list[dict[str, Any]]:
    """按时间倒序列出全部备份。"""
    if not BACKUP_DIR.is_dir():
        return []
    items = [describe(p) for p in BACKUP_DIR.glob("*.zip") if p.is_file()]
    items.sort(key=lambda it: (it["createdAt"], it["name"]), reverse=True)
    return items


def resolve(name: str) -> Path:
    """把「备份文件名」解析成安全路径（非法名 / 不存在分别抛 ValueError / FileNotFoundError）。"""
    clean = (name or "").strip()
    if not _NAME_RE.match(clean):
        raise ValueError("备份文件名不合法")
    path = BACKUP_DIR / clean
    if not path.is_file():
        raise FileNotFoundError(clean)
    return path


def delete_backup(name: str) -> bool:
    path = resolve(name)
    path.unlink()
    log.warning("已删除数据备份 | %s", path.name)
    return True


def prune(keep: int) -> list[str]:
    """只保留最新的 ``keep`` 份，其余按时间从旧到新删掉，返回被删的名单。"""
    keep = max(1, int(keep or 1))
    removed: list[str] = []
    for item in list_backups()[keep:]:
        try:
            resolve(item["name"]).unlink()
            removed.append(item["name"])
        except (OSError, ValueError):
            log.warning("清理旧备份失败 | %s", item["name"])
    if removed:
        log.warning("已按「保留 %d 份」清理旧备份 | %d 个 | %s", keep, len(removed), ", ".join(removed))
    return removed


def verify(path: Path) -> dict[str, Any]:
    """校验它是不是本站的备份，返回清单；不合格抛 ``ValueError``。"""
    if not zipfile.is_zipfile(path):
        raise ValueError("上传的不是有效的 zip 文件")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if DB_MEMBER not in names:
            raise ValueError("备份里没有数据库快照（config/nte.sqlite3）")
        # 防路径穿越 / 绝对路径：一个都不允许，一律拒收
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if info.filename.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(f"备份里包含不安全的路径：{info.filename}")
        try:
            manifest = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("备份缺少可用的 manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("备份格式不匹配（可能不是本站导出的备份）")
    return manifest


def _check_database(path: Path) -> None:
    """确认解出来的 sqlite 真的是本站的库（至少要能开、且有 events 表）。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"备份里的数据库无法打开：{exc}") from exc
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"备份里的数据库不是有效的 SQLite 文件：{exc}") from exc
    finally:
        conn.close()
    if not row:
        raise ValueError("备份里的数据库不是本站的数据（缺少 events 表）")


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #
def _replace_db(new_db: Path) -> None:
    """用新库替换当前数据库（先清 WAL / SHM，再原子改名）。"""
    target = store.path
    target.parent.mkdir(parents=True, exist_ok=True)
    # WAL / SHM 是「旧库」的旁支日志，留着会把新库污染回旧状态
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    staged = target.with_name(f"{target.name}.restoring")
    shutil.copy2(new_db, staged)
    os.replace(staged, target)
    # 幂等的结构升级：把老备份补齐到当前表结构（新增列等）
    db.init_db(target)


def _replace_avatars(src: Path) -> int:
    """用备份里的头像目录替换现有目录（备份里没有这一项就保持不动）。"""
    if not src.is_dir():
        return 0
    root = avatars.AVATAR_DIR
    root.parent.mkdir(parents=True, exist_ok=True)
    staged = root.with_name(f"{root.name}.restoring")
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(src, staged)
    shutil.rmtree(root, ignore_errors=True)
    os.replace(staged, root)
    return sum(1 for p in root.rglob("*") if p.is_file())


def restore_file(path: Path, *, safety: bool = True) -> dict[str, Any]:
    """用备份文件还原（**覆盖**当前数据）。

    ``safety=True`` 时先打一份「还原前」的安全备份——万一还原错了还能再倒回来。
    返回 ``{"manifest": ..., "safety": ...}``。
    """
    manifest = verify(path)
    safety_meta = create_backup("pre-restore") if safety else None
    work = Path(tempfile.mkdtemp(prefix="nte-restore-"))
    try:
        with zipfile.ZipFile(path) as zf:
            zf.extractall(work)
        new_db = work / "config" / "nte.sqlite3"
        _check_database(new_db)
        _replace_db(new_db)
        avatars_restored = _replace_avatars(work / "data" / "avatars")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.warning(
        "已从备份还原 | %s | 备份时间=%s | 头像=%d | 安全备份=%s",
        path.name,
        manifest.get("createdAt") or "?",
        avatars_restored,
        (safety_meta or {}).get("name") or "无",
    )
    return {"manifest": manifest, "safety": safety_meta, "avatars": avatars_restored}


def restore_upload(data: bytes, *, original: str = "", keep_upload: bool = True) -> dict[str, Any]:
    """用上传的字节流还原：先落盘成一个备份文件（便于追溯），再走 ``restore_file``。"""
    if not data:
        raise ValueError("上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"备份文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    base = _safe_reason(Path(original or "").stem or "upload")
    uploaded = BACKUP_DIR / f"nte-{_stamp()}-upload-{base}.zip"
    uploaded.write_bytes(data)
    try:
        # keep_upload=False 时（例如还原成功但不想留档）由调用方删掉
        result = restore_file(uploaded, safety=True)
    except BaseException:
        uploaded.unlink(missing_ok=True)
        raise
    result["uploaded"] = uploaded.name if keep_upload else ""
    return result


# --------------------------------------------------------------------------- #
# 周期性自动备份
# --------------------------------------------------------------------------- #
async def run_auto_backup() -> dict[str, Any] | None:
    """该跑就跑一次自动备份，并按「保留份数」清理；不满足条件返回 ``None``。"""
    settings = load_settings()
    if not is_due(settings):
        return None
    meta = await asyncio.to_thread(create_backup, "auto")
    settings = save_settings(
        {**settings, "lastRunAt": meta["createdAt"], "lastFile": meta["name"]}
    )
    await asyncio.to_thread(prune, int(settings.get("keep") or 1))
    return meta


async def auto_backup_loop(interval_seconds: int = 300) -> None:
    """后台巡检：每 ``interval_seconds`` 看一次「到点没」，到点就打一份。

    先查一次再睡——服务停了一段时间后重启，会立刻补上欠下的那一份。
    失败只记日志、绝不把循环带崩：下一轮还会再试。
    """
    settings = load_settings()
    if settings.get("enabled"):
        log.warning(
            "周期性自动备份已开启 | 每 %s 小时一次 | 保留 %s 份",
            settings.get("intervalHours"),
            settings.get("keep"),
        )
    while True:
        # CancelledError 继承自 BaseException，不会被这里吞掉，取消照常生效
        try:
            await run_auto_backup()
        except Exception:  # 后台任务：任何异常都不该终止循环，记一笔继续
            log.warning("自动备份本轮失败（忽略，稍后再试）", exc_info=True)
        await asyncio.sleep(interval_seconds)


def status() -> dict[str, Any]:
    """备份面板要的全部只读信息。"""
    settings = load_settings()
    return {
        "settings": settings,
        "nextRunAt": next_run_at(settings),
        "dir": str(BACKUP_DIR),
        "maxUploadMb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }
