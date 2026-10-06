"""旧数据快照路由（**仅服务器管理员**）。

| 接口 | 说明 |
| --- | --- |
| `GET    /api/legacy-backups` | 旧数据快照列表（只读留存，供下载） |
| `GET    /api/legacy-backups/{name}/download` | 下载某一份（**只认请求头**，同备份下载） |
| `DELETE /api/legacy-backups/{name}` | 删除某一份（确认没用之后再删） |

**没有还原接口**，这是刻意的：这些快照是「新版本已经读不动的旧结构」，
把它塞回新版本只会得到一份读不动的数据，反而更危险。要看内容就下载下来，
用任何 SQLite 工具打开（zip 里的 ``config/nte.sqlite3`` 就是一份完整旧库）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import legacy
from .auth import Session
from .logging_conf import get_logger
from .security import require_server

log = get_logger("legacy")

router = APIRouter(prefix="/api", tags=["legacy"])

#: 面板上的说明文案（前端直接用它，免得两边各写一份）
LEGACY_NOTE = (
    "升级前的旧数据快照：新版本会自动转换计分口径，转换**之前**原样留一份在这里。"
    "它们不能被当前版本使用，也不提供还原；需要看内容就下载下来，"
    "解压后的 config/nte.sqlite3 就是一份完整旧库。"
)


@router.get("/legacy-backups")
async def api_legacy_backups(_: Session = Depends(require_server)) -> dict[str, Any]:
    """旧数据快照列表（仅服务器管理员）。"""
    items = await asyncio.to_thread(legacy.list_snapshots)
    return {"ok": True, "backups": items, "note": LEGACY_NOTE}


@router.get("/legacy-backups/{name}/download")
async def api_legacy_download(name: str, _: Session = Depends(require_server)) -> FileResponse:
    """下载一份旧数据快照。"""
    path = await asyncio.to_thread(legacy.path_of, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"没有这份旧数据快照：{name}")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/legacy-backups/{name}")
async def api_legacy_delete(name: str, _: Session = Depends(require_server)) -> dict[str, Any]:
    """删除一份旧数据快照（删之前请确认里面的东西确实用不上了）。"""
    removed = await asyncio.to_thread(legacy.delete, name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"没有这份旧数据快照：{name}")
    log.warning("已删除旧数据快照 | %s", name)
    return {"ok": True, "backups": await asyncio.to_thread(legacy.list_snapshots)}
