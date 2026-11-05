"""调试模式下的机器人沙盒接口（``/api/debug/bot``）。

四个用途，都只在 ``NTE_DEBUG=1`` 时存在（否则一律 404）：

* ``GET  /api/debug/bot``        快照：已记录的消息、推送类型表、可选的届次；
* ``POST /api/debug/bot/say``    **模拟群友发一句话**：认命令 → 走真实链路取回复
  （文本 + 卡片图），两条消息都记进缓冲；
* ``POST /api/debug/bot/build``  按推送类型直接组装一次（不发送），并记进沙盒；
* ``POST /api/debug/bot/text``   把一段自由文本按发送规则分段记进沙盒
  （用来验「太长会怎么切」）；
* ``POST /api/debug/bot/clear``  清空。

为什么组装要复用 ``qqbot_api.build_message``：另写一套预览逻辑，早晚会与真正发出去的
那一份不一样——那样这个页面就失去意义了。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from . import botlab, card, helpcard, qqbot
from .auth import Session
from .logging_conf import get_logger
from .qqbot_api import _require_push, _site, build_message
from .security import require_server
from .store import store

log = get_logger("botlab_api")

router = APIRouter(prefix="/api/debug/bot", tags=["debug"])


def _require_debug() -> None:
    """没开调试模式就当这个路由不存在（生产环境不该有这块面板）。"""
    if not botlab.active():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("")
async def api_state(session: Session = Depends(require_server)) -> dict[str, Any]:
    """沙盒页面初始化：已记录的消息 + 推送类型 + 可选届次 + 出图能力。"""
    _require_debug()
    snapshot = botlab.snapshot()
    events = await store.list_events()
    snapshot.update(
        {
            "currentId": store.current_id,
            "events": [
                {"id": item.get("id"), "name": item.get("name"), "status": item.get("status")}
                for item in events
            ],
            # 卡片图是沙盒里最想看的东西：画不出来就别装作有
            "imageAvailable": card.available(),
            "settings": qqbot.public_settings(store.qqbot_settings()),
        }
    )
    return snapshot


@router.post("/say")
async def api_say(
    request: Request,
    payload: dict[str, Any] = Body(...),  # noqa: B008
    session: Session = Depends(require_server),
) -> dict[str, Any]:
    """模拟「群友发了一句话」：认命令 → 取回复 → 双方都记进缓冲。

    回复内容走 :func:`app.qqbot_api.build_message`，与真实推送同一份代码——
    模拟器里看到的文本与卡片，就是群里会收到的那一份。
    """
    _require_debug()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="说点什么再发")
    event_id = str(payload.get("eventId") or "").strip()
    scene = str(payload.get("scene") or "group")

    user = botlab.record(kind="say", parts=[text], title="你", scene=scene, source="user", role="user")
    action, value = botlab.parse(text)

    if action == "kind":
        await _require_push(session, event_id)
        data = await build_message(request, kind=value, event_id=event_id, page=1)
        image = None
        if data.get("card"):
            info = data["card"]
            image = {"url": info.get("url"), "hash": info.get("hash"), "caption": info.get("caption")}
        reply = botlab.record(
            kind=value,
            parts=data.get("parts") or [],
            image=image,
            mentions=data.get("mentions") or [],
            scene=scene,
            source="preview",
            role="bot",
        )
    elif action == "help":
        # 真实群里「比赛帮助」是**先发帮助图**（见插件 cmd_help），图上就是这张命令表。
        # 图没有（还没生成 / 没装出图侧车）才只发文字——这里把两者都给出来，
        # 模拟群里正好一次看全。
        image = None
        if helpcard.default_path().is_file():
            image = {"url": f"{_site(request)}/help.jpg", "caption": "比赛机器人 · 使用说明"}
        reply = botlab.record(
            kind="help",
            parts=[botlab.help_text()],
            image=image,
            scene=scene,
            role="bot",
        )
    elif action == "identity":
        reply = botlab.record(
            kind="help",
            parts=[
                (
                    f"「{value}」要在真实群里发：站点侧按 QQ / 成员身份认人，"
                    "模拟器不伪造身份，所以这条只做说明。"
                )
            ],
            scene=scene,
            role="bot",
        )
    else:
        reply = botlab.record(kind="help", parts=[botlab.unknown_text()], scene=scene, role="bot")
    return {"ok": True, "user": user, "replies": [reply], "action": action}


@router.post("/build")
async def api_build(
    request: Request,
    payload: dict[str, Any] = Body(...),  # noqa: B008
    session: Session = Depends(require_server),
) -> dict[str, Any]:
    """按某个「推送类型」组装一次（**不发送**），并记进沙盒。"""
    _require_debug()
    kind = str(payload.get("kind") or "event").strip().lower()
    if kind not in qqbot.KIND_META:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' / '.join(qqbot.KIND_META)}")
    event_id = str(payload.get("eventId") or "").strip()
    await _require_push(session, event_id)

    data = await build_message(
        request,
        kind=kind,
        event_id=event_id,
        ref=str(payload.get("ref") or "").strip(),
        page=max(1, int(payload.get("page") or 1)),
    )
    image = None
    if data.get("card"):
        info = data["card"]
        image = {"url": info.get("url"), "hash": info.get("hash"), "caption": info.get("caption")}
    message = botlab.record(
        kind=kind,
        parts=data.get("parts") or [],
        image=image,
        mentions=data.get("mentions") or [],
        scene=str(payload.get("scene") or "group"),
        source="preview",
        role="bot",
    )
    log.debug("沙盒组装 | kind=%s | 段数=%d | 有图=%s", kind, len(message["parts"]), bool(image))
    return {
        "ok": True,
        "message": message,
        "meta": {"pages": data.get("pages"), "page": data.get("page"), "mentionText": data.get("mentionText")},
    }


@router.post("/text")
async def api_text(
    payload: dict[str, Any] = Body(...),  # noqa: B008
    session: Session = Depends(require_server),
) -> dict[str, Any]:
    """把一段自由文本按**发送规则**分段记进沙盒（验「太长会怎么切」）。"""
    _require_debug()
    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    limit = int(store.qqbot_settings().get("maxChars") or 1200)
    parts = qqbot.split_message(text, limit)
    message = botlab.record(
        kind="text",
        parts=parts,
        title=str(payload.get("title") or "自由文本"),
        scene=str(payload.get("scene") or "group"),
        source="text",
    )
    return {"ok": True, "message": message, "limit": limit}


@router.post("/clear")
async def api_clear(session: Session = Depends(require_server)) -> dict[str, Any]:
    """清空沙盒缓冲。"""
    _require_debug()
    return {"ok": True, "removed": botlab.clear()}
