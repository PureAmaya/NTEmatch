"""数据备份路由（**仅服务器管理员**）。

| 接口 | 说明 |
| --- | --- |
| `GET    /api/backups` | 备份列表 + 自动备份设置 + 下次执行时间 |
| `POST   /api/backups` | 立刻打一份备份 |
| `PUT    /api/backups/settings` | 改自动备份设置（开关 / 间隔 / 保留份数） |
| `GET    /api/backups/{name}/download` | 下载某一份（`?token=` 可带会话） |
| `DELETE /api/backups/{name}` | 删除某一份 |
| `POST   /api/backups/upload` | **上传备份并还原**（请求体就是原始 zip 字节） |

上传刻意不收 multipart：直接用 ``application/zip`` 原始请求体，省掉
``python-multipart`` 依赖，前端把 File 原样 POST 过来即可。

还原是**破坏性**操作：服务端会先自动打一份「还原前」的安全备份，还原完成后
**注销全部会话**（成员凭据可能已经变了），前端据此提示重新登录。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from . import backup
from .auth import Session, auth
from .logging_conf import get_logger
from .security import require_server
from .store import store

log = get_logger("backup")

router = APIRouter(prefix="/api", tags=["backup"])


@router.get("/backups")
async def api_backups(_: Session = Depends(require_server)) -> dict[str, Any]:
    """备份列表与自动备份设置（仅服务器管理员）。"""
    items = await asyncio.to_thread(backup.list_backups)
    return {"ok": True, "backups": items, **backup.status()}


@router.post("/backups")
async def api_backup_create(session: Session = Depends(require_server)) -> dict[str, Any]:
    """立刻打一份备份（原因记为 ``manual``）。"""
    try:
        meta = await asyncio.to_thread(backup.create_backup, "manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"备份失败：{exc}") from exc
    log.warning("服务器管理员手动备份 | 操作者=%s | %s", session.name or session.uid or "?", meta["name"])
    return {"ok": True, "backup": meta, "backups": await asyncio.to_thread(backup.list_backups)}


@router.put("/backups/settings")
async def api_backup_settings(
    payload: dict[str, Any] = Body(...),  # noqa: B008  (FastAPI 依赖注入惯例)
    _: Session = Depends(require_server),
) -> dict[str, Any]:
    """改自动备份设置：``enabled`` / ``intervalHours`` / ``keep``。"""
    try:
        settings = await asyncio.to_thread(backup.update_settings, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **backup.status(), "settings": settings}


@router.get("/backups/{name}/download")
async def api_backup_download(name: str, _: Session = Depends(require_server)) -> FileResponse:
    """下载一份备份（走 `?token=` 也可以，方便直接用链接下载）。"""
    try:
        path = await asyncio.to_thread(backup.resolve, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"没有这份备份：{name}") from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/backups/{name}/restore")
async def api_backup_restore(name: str, _: Session = Depends(require_server)) -> dict[str, Any]:
    """用已有的一份备份**还原**（覆盖当前数据；先自动打一份安全备份）。

    还原完成后全量重载内存状态并**注销全部会话**（成员凭据可能已不同），
    返回 ``reauth=true``，前端据此清掉本地会话并重新登录。
    """
    try:
        path = await asyncio.to_thread(backup.resolve, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"没有这份备份：{name}") from exc
    try:
        result = await asyncio.to_thread(backup.restore_file, path, safety=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"还原失败：{exc}") from exc

    await store.reload_all("restore-backup")
    revoked = auth.revoke_all()
    log.warning(
        "已从备份还原 | 来源=%s | 会话已注销=%d | 安全备份=%s",
        name,
        revoked,
        (result.get("safety") or {}).get("name") or "无",
    )
    return {
        "ok": True,
        "restored": result.get("manifest") or {},
        "safety": result.get("safety"),
        "avatars": result.get("avatars") or 0,
        "revoked": revoked,
        "reauth": True,
        "backups": await asyncio.to_thread(backup.list_backups),
    }


@router.delete("/backups/{name}")
async def api_backup_delete(name: str, _: Session = Depends(require_server)) -> dict[str, Any]:
    """删除一份备份。"""
    try:
        await asyncio.to_thread(backup.delete_backup, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"没有这份备份：{name}") from exc
    return {"ok": True, "backups": await asyncio.to_thread(backup.list_backups)}


@router.post("/backups/upload")
async def api_backup_upload(
    request: Request,
    name: str = Query(default="", description="原始文件名（仅用于留档命名）"),
    _: Session = Depends(require_server),
) -> dict[str, Any]:
    """上传一份备份并**还原**（请求体为原始 zip 字节）。

    还原会覆盖当前全部数据：先自动打安全备份，再替换数据库与头像目录，
    最后全量重载内存状态并**注销所有会话**（返回 ``reauth=true``）。
    """
    data = await request.body()
    try:
        result = await asyncio.to_thread(backup.restore_upload, data, original=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"还原失败：{exc}") from exc

    # 数据换了一批：全量重载内存状态，并让所有旧会话失效（成员凭据可能已不同）
    await store.reload_all("restore-backup")
    revoked = auth.revoke_all()
    log.warning(
        "已从上传备份还原 | 文件=%s | 会话已注销=%d | 安全备份=%s",
        result.get("uploaded") or "(临时)",
        revoked,
        (result.get("safety") or {}).get("name") or "无",
    )
    return {
        "ok": True,
        "restored": result.get("manifest") or {},
        "safety": result.get("safety"),
        "uploaded": result.get("uploaded") or "",
        "avatars": result.get("avatars") or 0,
        "revoked": revoked,
        # 凭据换了，前端据此清掉本地会话并重新登录
        "reauth": True,
        "backups": await asyncio.to_thread(backup.list_backups),
    }
