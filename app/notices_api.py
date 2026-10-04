"""公告（通知）、Markdown 预览、图片上传与站点信息。

两级作用域：

* ``event``：某一届的**赛事通知**（赛事管理员或服务器管理员发布）；
* ``server``：**服务器通知**（只有服务器管理员能发），打开任何路由都会弹。

三条规矩集中在这里，别的模块不必重复：

1. **赛事管理员只能碰自己那届**的公告（与其它管理接口一致，靠 ``ownerUid`` 判定）；
2. **隐藏届**的公告对公众不可见（服务器管理员照常可见）；
3. 公告正文只收 Markdown 原文，**渲染在服务端做**（严格白名单，见 app/markdown.py）——
   前端拿到的是已经洗过的 HTML，不需要引入 MD 库，也不会两端渲染不一致。

服务器级通知与「服务器信息」都要求服务器管理员；图片上传要求赛事管理员以上
（上传本身就带写权限的意味，不能让访客当作免费图床）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import markdown, media
from .logging_conf import get_logger
from .models import NTEModel
from .security import (
    Session,
    optional_session,
    require_event,
    require_event_owned,
    require_server,
)
from .store import store

log = get_logger("notice")

router = APIRouter(tags=["notices"])

#: 卡片摘要长度：够看清「讲了什么」，又不至于把列表撑开
SUMMARY_CHARS = 120
#: 正文上限：公告是给人读的，不是文件柜（图片走上传，不占正文）
MAX_BODY_CHARS = 20_000
MAX_TITLE_CHARS = 120


class NoticePayload(NTEModel):
    """新建 / 更新公告。``scope=server`` 时只有服务器管理员可用。"""

    scope: str = "event"
    event_id: str = ""
    title: str = ""
    body: str = ""


class TextPayload(NTEModel):
    """一段 Markdown（预览 / 赛事信息 / 服务器信息共用）。"""

    text: str = ""


class MediaPayload(NTEModel):
    """图片：前端读成 data:URL 再传，服务端按魔数校验后落盘。"""

    data_url: str = ""


def _clean_scope(value: str) -> str:
    return "server" if str(value or "").strip().lower() == "server" else "event"


async def _event_row(event_id: str) -> dict[str, Any]:
    for item in await store.list_events():
        if item["id"] == event_id:
            return item
    return None  # type: ignore[return-value]


async def _event_scope(session: Session | None, event_id: str, *, write: bool) -> dict[str, Any]:
    """赛事级作用域的通用校验：届存在、可见性、（写时）归属。"""
    row = await _event_row(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"第 {event_id} 届不存在")
    visible = not row.get("hidden") or (session is not None and session.can_manage_events)
    if not visible:
        raise HTTPException(status_code=404, detail="该届赛事不存在")
    if write:
        assert session is not None  # 调用方保证（require_event 已注入）
        require_event_owned(session, str(row.get("ownerUid") or ""))
    return row


def _body_guard(title: str, body: str) -> tuple[str, str]:
    clean_title = " ".join(str(title or "").split())[:MAX_TITLE_CHARS]
    clean_body = str(body or "").replace("\r\n", "\n").strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="通知需要标题")
    if not clean_body:
        raise HTTPException(status_code=400, detail="通知内容不能为空")
    if len(clean_body) > MAX_BODY_CHARS:
        raise HTTPException(status_code=400, detail=f"通知内容超过 {MAX_BODY_CHARS} 字，请精简或拆成多条")
    return clean_title, clean_body


def _card(row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    """列表卡片 / 详情共用的输出格式（正文只给摘要，详情才带渲染后的 HTML）。"""
    out = {
        "id": row["id"],
        "scope": row["scope"],
        "eventId": row.get("eventId", ""),
        "title": row.get("title", ""),
        "author": row.get("author", ""),
        "createdAt": row.get("createdAt", ""),
        "updatedAt": row.get("updatedAt", ""),
        "summary": markdown.to_text(row.get("body", ""), SUMMARY_CHARS),
    }
    if full:
        out["body"] = row.get("body", "")
        out["html"] = markdown.render(row.get("body", ""))
    return out


# --------------------------------------------------------------------------- #
# 公告
# --------------------------------------------------------------------------- #
@router.get("/api/notices")
async def api_list_notices(
    scope: str = "event",
    event_id: str = "",
    page: int = 1,
    size: int = 4,
    session: Session | None = Depends(optional_session),  # noqa: B008
) -> dict[str, Any]:
    """公告列表（分页，最新在前）。公开只读。"""
    scope = _clean_scope(scope)
    if scope == "server":
        eid = ""
    else:
        eid = (event_id or "").strip() or store.current_id
        await _event_scope(session, eid, write=False)
    data = await store.list_notices(scope, eid, page=page, size=size)
    data["items"] = [_card(item) for item in data["items"]]
    return {"ok": True, "scope": scope, "eventId": eid, **data}


@router.get("/api/notices/{notice_id}")
async def api_get_notice(
    notice_id: str,
    session: Session | None = Depends(optional_session),  # noqa: B008
) -> dict[str, Any]:
    """单条公告（含渲染后的 HTML）。公开只读。"""
    row = await store.get_notice(notice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    if row["scope"] == "event":
        await _event_scope(session, row.get("eventId", ""), write=False)
    return {"ok": True, "notice": _card(row, full=True)}


@router.post("/api/notices")
async def api_create_notice(
    payload: NoticePayload, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """发布通知。赛事通知要「自己那届」，服务器通知要服务器管理员。"""
    scope = _clean_scope(payload.scope)
    if scope == "server":
        if not session.is_server:
            raise HTTPException(status_code=403, detail="服务器通知仅限服务器管理员发布")
        eid = ""
    else:
        eid = (payload.event_id or "").strip() or store.current_id
        await _event_scope(session, eid, write=True)
    title, body = _body_guard(payload.title, payload.body)
    row = await store.save_notice(
        scope=scope,
        event_id=eid,
        title=title,
        body=body,
        author=session.name or session.label,
    )
    return {"ok": True, "notice": _card(row, full=True)}


@router.put("/api/notices/{notice_id}")
async def api_update_notice(
    notice_id: str, payload: NoticePayload, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """编辑通知（作用域不能改：改了就相当于换了个发布对象）。"""
    row = await store.get_notice(notice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    if row["scope"] == "server":
        if not session.is_server:
            raise HTTPException(status_code=403, detail="服务器通知仅限服务器管理员修改")
    else:
        await _event_scope(session, row.get("eventId", ""), write=True)
    title, body = _body_guard(payload.title, payload.body)
    saved = await store.save_notice(
        scope=row["scope"],
        event_id=row.get("eventId", ""),
        notice_id=notice_id,
        title=title,
        body=body,
        author=row.get("author") or session.name or session.label,
    )
    return {"ok": True, "notice": _card(saved, full=True)}


@router.delete("/api/notices/{notice_id}")
async def api_delete_notice(
    notice_id: str, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """删除通知。"""
    row = await store.get_notice(notice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    if row["scope"] == "server":
        if not session.is_server:
            raise HTTPException(status_code=403, detail="服务器通知仅限服务器管理员删除")
    else:
        await _event_scope(session, row.get("eventId", ""), write=True)
    await store.delete_notice(notice_id)
    return {"ok": True, "id": notice_id}


# --------------------------------------------------------------------------- #
# Markdown 预览
# --------------------------------------------------------------------------- #
@router.post("/api/md/preview")
async def api_md_preview(
    payload: TextPayload, _: Session = Depends(require_event)
) -> dict[str, Any]:
    """渲染预览：与发布走**同一个**渲染器，所以「预览什么样、发布就什么样」。

    要求登录是因为它会把文本渲染成 HTML——不能让匿名访客把它当免费渲染服务。
    """
    text = str(payload.text or "")[:MAX_BODY_CHARS]
    return {"ok": True, "html": markdown.render(text), "summary": markdown.to_text(text, SUMMARY_CHARS)}


# --------------------------------------------------------------------------- #
# 图片上传与读取
# --------------------------------------------------------------------------- #
@router.post("/api/media")
async def api_media_upload(
    payload: MediaPayload, _: Session = Depends(require_event)
) -> dict[str, Any]:
    """上传公告图片（data:URL），返回同源地址。按内容哈希命名，天然可长缓存。"""
    try:
        saved = media.save_data_url(payload.data_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **saved}


@router.get("/api/media/{name}")
async def api_media_file(name: str) -> FileResponse:
    """读取公告图片。

    文件名是内容哈希 → **可以放心发一年期 immutable**：源图换了就是新文件名，
    不存在「缓存不刷新」。带 ``nosniff`` 保证浏览器不会把它当脚本执行。
    """
    path = media.resolve(name)
    if path is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path,
        media_type=media.mime_for(path),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/media")
async def api_media_stats(_: Session = Depends(require_server)) -> dict[str, Any]:
    """图片占用统计（服务器管理页展示）。"""
    return {"ok": True, **media.stats()}


# --------------------------------------------------------------------------- #
# 服务器信息（站点级 Markdown）
# --------------------------------------------------------------------------- #
@router.get("/api/server/info")
async def api_server_info_read() -> dict[str, Any]:
    """服务器信息（公开只读）：站点说明 / 关于本站。"""
    text = store.server_info()
    return {"ok": True, "text": text, "html": markdown.render(text)}


@router.put("/api/server/info")
async def api_server_info_write(
    payload: TextPayload, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """保存服务器信息（仅服务器管理员）。"""
    text = str(payload.text or "")[:MAX_BODY_CHARS]
    saved = await store.set_server_info(text, actor=f"web:server:{session.uid or 'server'}")
    return {"ok": True, "text": saved, "html": markdown.render(saved)}


# --------------------------------------------------------------------------- #
# 赛事信息（Markdown）——「结束后只读」的那条规矩
# --------------------------------------------------------------------------- #
@router.post("/api/event/info/preview")
async def api_event_info_preview(
    payload: TextPayload, _: Session = Depends(require_event)
) -> dict[str, Any]:
    """赛事信息预览（与通知共用渲染器，单独给一个入口是为了让权限更好读）。"""
    return {"ok": True, "html": markdown.render(str(payload.text or "")[:MAX_BODY_CHARS])}


@router.get("/api/event/info")
async def api_event_info_read() -> dict[str, Any]:
    """当前届的赛事信息（Markdown 原文 + 渲染结果）。公开只读。"""
    cfg = store.snapshot()
    text = cfg.event.rules_text
    closed = cfg.event.status == "closed"
    return {
        "ok": True,
        "eventId": store.current_id,
        "text": text,
        "html": markdown.render(text),
        # 已结束的届：信息只读（通知仍可发），前端据此把编辑按钮变成只读提示
        "editable": not closed,
        "closed": closed,
    }
