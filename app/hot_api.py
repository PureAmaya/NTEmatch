"""热更新（``/api/hot``）：看状态、点一下更新。

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /api/hot` | 服务器管理员 | 守护进程在不在、上次更新到哪一步、有没有待生效的改动 |
| `POST /api/hot/update` | 服务器管理员 | 请求一次更新：``reload``（只换代码）/ ``pull``（先 `git pull` 再换） |

设计上的两条取舍
----------------

* **只写一个请求文件，不直接动手**：真正换代的是持有监听套接字的那个守护进程
  （见 :mod:`app.hotrun`）。HTTP 进程既不认识父进程的 pid，也不该去开额外的内网端口
  ——开一个「能重启本站」的端口，就等于多了一个必须自己鉴权的攻击面。
  写文件这条路天然只有「已经在本站代码里跑」的东西才能用，而**权限在这一侧判**
  （服务器管理员），所以没有绕过一说；
* **更新是异步的**：点下去只回「已排入队列」，真正的换代在**几百毫秒后**发生
  （父进程 1~2 秒轮询一次）。这不是偷懒：换代要换掉正在服务这个请求的进程，
  同步等它做完就成了「自己等自己死」。管理端要紧的是**结果**，所以状态接口里
  有 ``phase`` / ``message`` / ``reloads``，点完刷新一下就能看到走到哪一步。
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from . import hot
from .auth import Session
from .logging_conf import get_logger
from .models import NTEModel
from .security import require_server

log = get_logger("hot")

router = APIRouter(prefix="/api/hot", tags=["hot"])


class HotRequest(NTEModel):
    """一次更新请求。``pull`` = 先 ``git pull`` 再换代（需要这台机器上有 git 且是仓库）。"""

    mode: str = "reload"


@router.get("/ping", include_in_schema=False)
async def api_hot_ping(request: Request) -> dict[str, Any]:
    """**给热更新守护用的**自查口：确认「这个端口上答话的确实是这一代」。

    不是给管理端用的（因此不出现在文档里，也不吃会话）：口令是父进程 spawn 时
    通过环境变量递进来的随机串，只有**那一代进程**知道。口令对不上就回 404
    ——与「没有这个路由」无法区分，不对外暴露任何信息。

    它解决的是一件要命的事：**「应用起来了」不等于「能收连接」**。真出过这种情况
    （Windows 的 proactor 循环接不了继承来的套接字），而那种时候父进程若以为换代
    成功、把旧进程停掉，服务就断了。父进程靠这个接口把这种进程拦下来。
    """
    token = hot.hot_token()
    got = request.headers.get(hot.PING_HEADER, "")
    if not token or not hmac.compare_digest(got, token):
        raise HTTPException(status_code=404)
    return {"ok": True, "pid": os.getpid()}


@router.get("")
async def api_hot_status(_: Session = Depends(require_server)) -> dict[str, Any]:
    """热更新状态（守护进程写的状态文件 + 本进程能判断的东西）。"""
    status = hot.public_status()
    status["ok"] = True
    status["ready"] = bool(status.get("supervised"))
    status["hint"] = (
        ""
        if status.get("supervised")
        else "当前进程不是由热更新守护启动的：用 `uv run python -m app hotrun` 启动才能热更新"
    )
    status["dir"] = str(hot.hot_dir())
    return status


@router.post("/update")
async def api_hot_update(
    session: Session = Depends(require_server),
    payload: HotRequest | None = Body(default=None),  # noqa: B008  (FastAPI 请求体惯例)
) -> dict[str, Any]:
    """请求一次热更新（``reload`` 只换代码，``pull`` 先拉代码）。

    没有守护进程时**明确报错**，而不是假装成功：用户点了没反应比看到一句
    「你没有开热更新守护」糟糕得多。
    """
    if not hot.supervised():
        raise HTTPException(
            status_code=400,
            detail=(
                "当前进程不是由热更新守护启动的，无法热更新。"
                "请改用 `uv run python -m app hotrun` 启动服务（旧进程会优雅退场）。"
            ),
        )
    mode = str((payload.mode if payload else "") or "reload").strip().lower()
    if mode not in ("reload", "pull"):
        raise HTTPException(status_code=400, detail="mode 只能是 reload（只换代码）或 pull（先拉代码）")
    actor = session.name or session.label or "?"
    hot.request(mode, actor)
    log.warning("已请求热更新 | 方式=%s | 操作者=%s", mode, actor)
    return {
        "ok": True,
        "mode": mode,
        "message": "已排入更新队列：几秒内完成，如果新版本起不来会保持现在的版本继续服务",
        "status": hot.public_status(),
    }
