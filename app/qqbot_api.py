"""QQ 机器人（AstrBot）推送路由。

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /api/qqbot` | 服务器管理员 | 推送配置（**不含 API Key 明文**，只回 `hasKey`）+ 目标 UMO |
| `PUT /api/qqbot` | 服务器管理员 | 改配置（`apiKey` 传空串 = 不改） |
| `POST /api/qqbot/test` | 服务器管理员 | 往群里发一条测试消息（验证地址 / Key / UMO / @ 方式） |
| `GET /api/qqbot/preview` | 服务器管理员 / 该届赛事管理员 | 预览要发什么（**不发送**） |
| `POST /api/qqbot/push` | 服务器管理员 / 该届赛事管理员 | 真正发出去（超长自动分段） |

推送类型（`kind`）：

* ``event``    —— 比赛信息（名字 / 赛制 / 时间 / 人数 / 简介 / 是否排名）
* ``live``     —— 当前直播（主直播间 + 正在推流的选手 / 成员机位与观看地址；**全局**）
* ``progress`` —— 赛程进展（已赛多少、正在打谁 vs 谁）
* ``call``     —— 一键 @ 参赛者到场准备
* ``result``   —— 比赛结果（冠军 / 榜 + 逐场比分）
* ``list``     —— 全部比赛列表（分页）
* ``detail``   —— 某一届的信息 + 进程 + 结果；带 ``ref`` 时细说那一场
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from . import live, logic, qqbot
from .auth import Session, hash_secret
from .logging_conf import get_logger
from .security import require_event, require_event_owned, require_server
from .store import store

log = get_logger("qqbot")

router = APIRouter(prefix="/api", tags=["qqbot"])


async def _owner_of(event_id: str) -> str:
    """某一届的归属 uid（找不到该届返回空串）。"""
    for item in await store.list_events():
        if item["id"] == event_id:
            return str(item.get("ownerUid") or "")
    return ""


async def _require_push(session: Session, event_id: str = "") -> str:
    """推送权限：服务器管理员放行任意届；赛事管理员只能推**自己创建**的届。

    返回最终要操作的届 id（``event_id`` 留空 = 当前届，即 ``store.current_id``）。
    """
    if not session.can_manage_events:
        raise HTTPException(status_code=403, detail="需要赛事管理员或服务器管理员权限")
    target = (event_id or "").strip() or store.current_id
    if session.is_server:
        return target
    require_event_owned(session, await _owner_of(target))
    return target


async def _context(event_id: str) -> tuple[Any, dict[str, Any]]:
    """取「某一届的配置 + 已渲染状态」。当前届直接吃内存快照，其它届只读载入。"""
    if not event_id or event_id == store.current_id:
        cfg = store.snapshot()
        return cfg, logic.build_state(cfg)
    try:
        cfg = await store.read_event(event_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"没有这一届：{event_id}") from exc
    # 往届回看：直播一律按关闭处理（与本站在线一致）
    return cfg, logic.build_state(cfg, historical=True)


@router.get("/qqbot/status")
async def api_qqbot_status(_: Session = Depends(require_event)) -> dict[str, Any]:
    """推送状态（赛事管理员可见）：够不够「能发」，**不含任何凭据**。

    赛事管理页的「推送到群」面板据此提示「还没配好 / 未启用」。
    """
    settings = store.qqbot_settings()
    ready = bool(settings.get("enabled") and settings.get("apiKey") and qqbot.resolved_umo(settings))
    return {
        "ok": True,
        "enabled": bool(settings.get("enabled")),
        "hasKey": bool(settings.get("apiKey")),
        "umo": qqbot.resolved_umo(settings),
        "atMode": settings.get("atMode"),
        "maxChars": settings.get("maxChars"),
        "ready": ready,
        # 限流额度（预览不占额度，所以赛事管理员看到的就是真实可用额度）
        "limit": await qqbot.limiter.snapshot(settings),
    }


@router.get("/qqbot")
async def api_qqbot(_: Session = Depends(require_server)) -> dict[str, Any]:
    """QQ 机器人推送配置（仅服务器管理员；**不回 API Key 明文**）。"""
    settings = store.qqbot_settings()
    return {
        "ok": True,
        "settings": qqbot.public_settings(settings),
        "kinds": list(qqbot.KINDS),
        "limit": await qqbot.limiter.snapshot(settings),
    }


@router.put("/qqbot")
async def api_qqbot_update(
    payload: dict[str, Any] = Body(...),  # noqa: B008  (FastAPI 依赖注入惯例)
    _: Session = Depends(require_server),
) -> dict[str, Any]:
    """更新推送配置（仅服务器管理员）。"""
    try:
        settings = await store.set_qqbot(payload, actor="web:qqbot")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "settings": qqbot.public_settings(settings)}


@router.post("/qqbot/bot-token")
async def api_qqbot_bot_token(_: Session = Depends(require_server)) -> dict[str, Any]:
    """生成（或重置）**只读查询 API 的令牌**（仅服务器管理员）。

    令牌给 AstrBot 插件配置用：服务端只存**加盐哈希**，明文**只在本次响应里出现一次**，
    之后只能重置、无法再查看。重置后旧令牌立即失效。
    """
    token = qqbot.new_bot_token()
    await store.set_qqbot(
        {"botApiTokenHash": hash_secret(token)}, actor="web:qqbot-token", internal=True
    )
    log.warning("查询 API 令牌已生成（旧令牌立即失效）")
    return {"ok": True, "token": token, "note": "仅此一次显示，请立即复制到插件配置里"}


@router.delete("/qqbot/bot-token")
async def api_qqbot_bot_token_clear(_: Session = Depends(require_server)) -> dict[str, Any]:
    """清除查询 API 令牌（等于关闭只读查询 API）。"""
    await store.set_qqbot({"botApiTokenHash": ""}, actor="web:qqbot-token-clear", internal=True)
    log.warning("查询 API 令牌已清除，插件将无法再查询")
    return {"ok": True}


@router.post("/qqbot/test")
async def api_qqbot_test(
    payload: dict[str, Any] | None = Body(default=None),  # noqa: B008
    _: Session = Depends(require_server),
) -> dict[str, Any]:
    """发一条测试消息（默认带一个 @ 自己，用来验证 @ 方式是否生效）。"""
    settings = store.qqbot_settings()
    # 测试发送也会真的发消息，所以同样吃限流额度（预览不吃）
    allowed, reason, wait = await qqbot.limiter.acquire(settings)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason, headers={"Retry-After": str(wait)})
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        probe = qqbot.at_text([str((payload or {}).get("qq") or "10001")], settings)
        text = (
            f"{probe}【NTE 比赛】这是一条测试消息。\n"
            f"若你看到这条消息，说明地址 / API Key / 目标会话都通了。\n"
            f"@ 方式：{settings.get('atMode')}（若不是真的 @，把「@ 方式」改成 cq 再试）"
        )
    result = await qqbot.send_text(text, settings=settings)
    return {"ok": result["ok"], "detail": result["detail"], "umo": result["umo"], "sent": 1 if result["ok"] else 0}


def _payload(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "event").strip().lower()
    if kind not in qqbot.KINDS:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' / '.join(qqbot.KINDS)}")
    return {
        "kind": kind,
        "eventId": str(payload.get("eventId") or "").strip(),
        "ref": str(payload.get("ref") or "").strip(),
        "page": max(1, int(payload.get("page") or 1)),
    }


async def _build(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """构建消息（返回 ``(构建结果, 目标届 id)``）。"""
    args = _payload(payload)
    settings = store.qqbot_settings()
    cfg = None
    state: dict[str, Any] = {}
    # list 与 live 都是**全局**信息，不挑届次
    if args["kind"] not in ("list", "live"):
        cfg, state = await _context(args["eventId"])
    # 预览 / 推送「当前直播」时真探一次：管理员点预览就是要看此刻的真实状态
    live_info = await live.collect_live() if args["kind"] == "live" else None
    result = await asyncio.to_thread(
        qqbot.dispatch,
        args["kind"],
        settings=settings,
        cfg=cfg,
        state=state,
        events=await store.list_events(),
        ref=args["ref"],
        page=args["page"],
        members=store.members(),
        live_info=live_info,
    )
    return result, args["eventId"]


@router.get("/qqbot/preview")
async def api_qqbot_preview(
    kind: str = Query(default="event"),
    event_id: str = Query(default="", alias="eventId"),
    ref: str = Query(default=""),
    page: int = Query(default=1),
    session: Session = Depends(require_event),
) -> dict[str, Any]:
    """预览要发出去的内容（**不发送**），管理员可以先看再决定。"""
    await _require_push(session, event_id)
    result, _target = await _build({"kind": kind, "eventId": event_id, "ref": ref, "page": page})
    return {
        "ok": True,
        "parts": result["parts"],
        "pages": result["pages"],
        "page": result["page"],
        "settings": qqbot.public_settings(store.qqbot_settings()),
    }


@router.post("/qqbot/push")
async def api_qqbot_push(
    payload: dict[str, Any] = Body(...),  # noqa: B008
    session: Session = Depends(require_event),
) -> dict[str, Any]:
    """把消息发到群里（超长自动分段；未配置 / 未启用会明确报错）。"""
    await _require_push(session, str(payload.get("eventId") or ""))
    result, target = await _build(payload)
    settings = store.qqbot_settings()
    if not settings.get("enabled"):
        raise HTTPException(status_code=400, detail="未启用 QQ 机器人推送（到「服务器 → QQ 机器人」开启）")
    if not settings.get("apiKey"):
        raise HTTPException(status_code=400, detail="未配置 AstrBot API Key")
    if not qqbot.resolved_umo(settings):
        raise HTTPException(status_code=400, detail="未配置目标会话（群号 / UMO）")

    # 单次能分几段（先挡，不占额度）：一次动作把群刷屏比「发不出去」更糟
    max_parts = max(1, int(settings.get("maxParts") or 8))
    if len(result["parts"]) > max_parts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"本次要发 {len(result['parts'])} 段，超过单次上限 {max_parts} 段。"
                "请改用分页（比赛列表）、缩短内容，或在设置里调大上限。"
            ),
        )
    # 频率限制：最小间隔 + 每小时上限（预览不占额度，真正发送才占）
    allowed, reason, wait = await qqbot.limiter.acquire(settings)
    if not allowed:
        log.warning("推送被限流 | %s", reason)
        raise HTTPException(status_code=429, detail=reason, headers={"Retry-After": str(wait)})

    sent = await qqbot.send_parts(result["parts"], settings=settings)
    if not sent["ok"]:
        raise HTTPException(
            status_code=502,
            detail=f"发送失败（已发出 {sent.get('sent', 0)}/{sent.get('total', 1)} 条）：{sent['detail']}",
        )
    log.warning(
        "已向群推送 | 类型=%s | 届=%s | 文本段=%d | 分页=%d/%d | 操作者=%s",
        payload.get("kind"),
        target or store.current_id,
        sent.get("sent"),
        result["page"],
        result["pages"],
        session.name or session.uid or "?",
    )
    return {
        "ok": True,
        "sent": sent.get("sent"),
        "total": sent.get("total"),
        "pages": result["pages"],
        "page": result["page"],
        "umo": sent.get("umo") or "",
        "preview": result["parts"][0][:200],
    }
