"""鉴权依赖（FastAPI 层）。

把「会话 → 权限」的判定集中在这里，供 :mod:`app.main` 与 :mod:`app.members`
共用，避免权限规则散落在各处：

* ``current_session``：任何有效登录（服务器管理员 / 赛事管理员 / 普通成员）；
* ``require_admin``  ：同上，保留旧名以兼容既有路由；
* ``require_server`` ：仅服务器管理员；
* ``require_event``  ：赛事管理员或服务器管理员。

**会话 token 只认请求头 `X-NTE-Token`**（见 :func:`session_token`）：
查询串里的 ``?token=`` 会原样进反向代理 / CDN 的访问日志，而且是「一个链接就能
改数据 / 下载文件」——下载与导出改成前端带请求头取 blob（见 ``core.downloadFile``）。
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from .auth import Session, auth
from .store import store

SESSION_HEADER = "X-NTE-Token"


def session_token(x_nte_token: str | None) -> str:
    """从请求头取会话 token（**不接受查询串**，理由见模块开头）。"""
    return (x_nte_token or "").strip()


async def current_session(x_nte_token: str | None = Header(default=None)) -> Session:
    """解析并校验会话；无效 / 过期回 401。

    用 :meth:`AuthManager.resolve` 而不是内存查表：热更新换代后新进程的内存里
    没有旧会话，它会去库里找一次（找到就放回内存），所以**更新不会把人踢下线**。
    """
    session = await auth.resolve(session_token(x_nte_token))
    if session is None:
        raise HTTPException(status_code=401, detail="登录无效或已过期，请重新登录")
    return session


async def require_admin(session: Session = Depends(current_session)) -> Session:
    """任何已登录用户（保留旧名，等价于 current_session）。"""
    return session


async def require_server(session: Session = Depends(current_session)) -> Session:
    """仅服务器管理员。"""
    if not session.is_server:
        raise HTTPException(status_code=403, detail="该操作仅限服务器管理员")
    return session


async def require_event(session: Session = Depends(current_session)) -> Session:
    """赛事管理员或服务器管理员（进入赛事管理的前置门槛）。"""
    if not session.can_manage_events:
        raise HTTPException(status_code=403, detail="需要赛事管理员或服务器管理员权限")
    return session


def event_owned_by(session: Session, owner_uid: str) -> bool:
    """服务器管理员放行；赛事管理员只能管理自己创建的届。

    ``owner_uid`` 为空（历史数据 / 无主）时，赛事管理员一律无权——避免误改。
    """
    if session.is_server:
        return True
    return bool(session.uid) and session.uid == owner_uid


def require_event_owned(session: Session, owner_uid: str) -> None:
    """对「当前届 / 目标届」做归属校验，不通过直接回 403。"""
    if not event_owned_by(session, owner_uid):
        raise HTTPException(
            status_code=403,
            detail="只能管理自己创建的赛事；如需管理其它届，请联系服务器管理员",
        )


def current_event_owner() -> str:
    """当前届的归属（供依赖注入后的接口做校验）。"""
    return store.snapshot().event.owner_uid


async def require_current_event(session: Session = Depends(require_event)) -> Session:
    """赛事管理接口的默认门槛：赛事管理员需拥有**当前届**（服务器管理员放行）。

    既有的事件管理接口都是「按当前届」设计的（没有届次参数），因此这里统一
    对当前届做归属校验，避免赛事管理员误改别人的届。
    """
    require_event_owned(session, current_event_owner())
    return session


async def optional_session(x_nte_token: str | None = Header(default=None)) -> Session | None:
    """可选会话：未登录返回 ``None``（用于需要「登录可见更多」的只读接口）。

    同样走 :meth:`AuthManager.resolve`：登录状态要能活过下一次热更新换代。
    """
    return await auth.resolve(session_token(x_nte_token))
