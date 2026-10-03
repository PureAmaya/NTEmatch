"""给 QQ 机器人（AstrBot）插件用的**只读查询 API**。

配好令牌后，AstrBot 里那个配套插件（``integrations/astrbot_plugin_nte_match``）
就能把「比赛列表 / 进度 / 结果 / 详情」直接当成**群命令**用，也能注册成
**LLM 工具**让机器人自己调用。

| 接口 | 说明 |
| --- | --- |
| `GET /api/bot/ping` | 探活：确认地址与令牌对不对 |
| `GET /api/bot/manifest` | 能力清单（有哪些查询、各自能问什么） |
| `GET /api/bot/events` | 届次的结构化列表（给 LLM 工具用，比纯文本更好解析） |
| `GET /api/bot/query` | 直接拿到**可以原样发到群里**的纯文本（分段已切好） |

``/api/bot/query`` 的 ``kind`` 清单见 ``/api/bot/manifest``。其中 ``live``（当前直播）
是**全局**信息（不挑届次），并且会**真的探测一次**媒体服务器（最多等 3 秒）——
其它 kind 都只读内存 / 数据库。

鉴权：``Authorization: Bearer nte_xxx``（也支持 ``X-NTE-Token`` 与 ``?token=``）。
令牌在「服务器 → QQ 机器人」里生成，**服务端只存加盐哈希**，明文只在生成那一次显示。

全是只读接口，不会碰机器人、也不占推送额度。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from . import live, logic, qqbot
from .auth import verify_secret
from .logging_conf import get_logger
from .store import store

log = get_logger("botapi")

router = APIRouter(prefix="/api/bot", tags=["bot"])


def _bearer(header: str | None) -> str:
    """从 ``Authorization: Bearer xxx`` 里取令牌。"""
    raw = (header or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw


async def require_bot_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_nte_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """校验插件令牌；返回当前的 QQ 机器人设置（含只读查询所需的一切）。"""
    settings = store.qqbot_settings()
    stored = str(settings.get("botApiTokenHash") or "")
    if not stored:
        raise HTTPException(
            status_code=403,
            detail="查询 API 未启用：请先在「服务器 → QQ 机器人」里生成接口令牌",
        )
    token = _bearer(authorization) or (x_nte_token or "").strip() or request.query_params.get("token", "")
    if not token or not verify_secret(token, stored):
        log.warning("查询 API 令牌校验失败 | ip=%s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="令牌不正确")
    return settings


@router.get("/ping")
async def api_bot_ping(
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """探活：插件启动时调一次，确认地址 / 令牌都通。"""
    cfg = store.snapshot()
    events = await store.list_events()
    return {
        "ok": True,
        "service": "nte-match",
        "currentEvent": {"id": store.current_id, "name": cfg.event.name or cfg.event.title},
        "eventCount": len(events),
        "pushEnabled": bool(settings.get("enabled")),
    }


@router.get("/manifest")
async def api_bot_manifest(
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """能力清单：插件据此注册命令与 LLM 工具，避免两边各写一份说明。"""
    return {
        "ok": True,
        "kinds": [
            {"key": key, "label": label, "hint": hint}
            for key, (label, hint) in qqbot.KIND_META.items()
        ],
        "params": {
            "kind": "查询类型（见 kinds）",
            "eventId": "届次 id（如 e001）；留空 = 当前主赛事（仅 live 不用填）",
            "ref": "场次编号（如 L-1 / 八强赛-1）；仅 detail 用",
            "page": "页码，从 1 开始；仅 list 用",
            "at": "是否带 @ 片段（召集类）；插件自己发 At 组件时传 0",
        },
        # 与插件里的命令一一对应（插件是执行方，这里是给调试/其它接入方看的清单）。
        # 别名刻意收得比较宽：关掉大模型后，「能命中命令」就是唯一依靠（见 README）。
        "commands": [
            {"command": "比赛", "kind": "detail", "args": "—", "alias": ["当前比赛", "赛事", "ntematch"]},
            {
                "command": "比赛直播",
                "kind": "live",
                "args": "—",
                "alias": ["直播", "当前直播", "谁在播", "谁在直播", "直播间", "开播了吗", "ntelive"],
                "note": "全局信息（不挑届次）：主直播间 + 正在推流的选手 / 成员机位",
            },
            {"command": "比赛列表", "kind": "list", "args": "页码（可选）", "alias": ["全部比赛", "历届", "往届", "比赛目录", "有哪些比赛"]},
            {"command": "比赛届次", "kind": "—", "args": "—", "alias": ["届次列表", "比赛编号"], "note": "用 /api/bot/events"},
            {"command": "比赛信息", "kind": "event", "args": "届次（可选）", "alias": ["赛事信息", "比赛时间", "nteginfo"]},
            {"command": "比赛进度", "kind": "progress", "args": "届次（可选）", "alias": ["进度", "赛程", "赛程进度", "现在打谁", "打到哪了", "谁领先", "什么情况"]},
            {"command": "比赛下一场", "kind": "next", "args": "届次（可选）", "alias": ["下一场", "下场", "接下来", "现在打", "接着打谁", "等下打谁"]},
            {"command": "比赛结果", "kind": "result", "args": "届次（可选）", "alias": ["结果", "成绩", "比分", "赢了吗", "什么比分", "结果咋样"]},
            {"command": "比赛冠军", "kind": "champion", "args": "届次（可选）", "alias": ["冠军", "榜首", "谁赢了"]},
            {"command": "比赛名单", "kind": "roster", "args": "届次（可选）", "alias": ["参赛名单", "选手名单", "比赛选手", "队伍", "都有谁"]},
            {"command": "比赛详情", "kind": "detail", "args": "届次 + 场次（可选）", "alias": ["赛事详情", "场次详情", "单场"]},
            {"command": "比赛召集", "kind": "call", "args": "届次（可选）", "alias": ["召集参赛", "集合", "喊人"], "note": "@ 由插件用 At 组件发"},
            {"command": "比赛帮助", "kind": "—", "args": "—", "alias": ["赛事帮助", "比赛命令", "比赛功能", "ntehelp", "比赛help"]},
        ],
    }


@router.get("/events")
async def api_bot_events(
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """届次的结构化列表（已过滤隐藏项）。"""
    events = await store.list_events()
    return {
        "ok": True,
        "current": store.current_id,
        "events": [
            {
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "status": item.get("status"),
                "ranked": item.get("ranked") is not False,
                "sport": item.get("sport") or "",
                "brief": item.get("brief") or "",
                "players": item.get("players") or 0,
                "played": item.get("played") or 0,
                "rounds": item.get("rounds") or 0,
                "startTime": item.get("startTime") or "",
                "endTime": item.get("endTime") or "",
                "champion": item.get("champion") or "",
                "current": bool(item.get("current")),
            }
            for item in events
            if not item.get("hidden")
        ],
    }


@router.get("/participants")
async def api_bot_participants(
    event_id: str = Query(default="", alias="eventId"),
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """参与名单里能 @ 到的 QQ（插件据此用 ``At`` 组件发**真正的 @**）。

    走 HTTP 发消息时 @ 只能写进文本（AstrBot 的 OpenAPI 没有 at 段），但**插件跑在
    AstrBot 里面**，可以直接用消息组件——所以这条路才能真正 @ 到人。
    """
    target = (event_id or "").strip() or store.current_id
    try:
        cfg = store.snapshot() if target == store.current_id else await store.read_event(target)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    qqs = qqbot.participant_qqs(cfg, store.members())
    return {
        "ok": True,
        "eventId": target,
        "eventName": cfg.event.name or cfg.event.title,
        "qqs": qqs,
        "count": len(qqs),
        "atMode": settings.get("atMode"),
    }


@router.get("/query")
async def api_bot_query(
    kind: str = Query(default="event"),
    event_id: str = Query(default="", alias="eventId"),
    ref: str = Query(default=""),
    page: int = Query(default=1),
    at: bool = Query(default=True, description="召集类是否带 @ 片段（插件自己发 At 时传 0）"),
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """按类型组装**可直接发到群里**的纯文本。

    ``parts`` 是已经按长度切好的段（QQ 单条有上限），插件依次发出去即可；
    ``text`` 是拼起来的全文（给 LLM 看不分段）。文本已做**纯文本化**，不含 Markdown。
    """
    if not at:
        settings = {**settings, "atMode": "none"}
    key = (kind or "event").strip().lower()
    if key not in qqbot.KINDS:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' / '.join(qqbot.KINDS)}")
    target = (event_id or "").strip() or store.current_id
    cfg = None
    state: dict[str, Any] = {}
    # list 与 live 都是**全局**信息，不需要挑届次（live 是成员机位 / 主直播间的事）
    if key not in ("list", "live"):
        try:
            if target == store.current_id:
                cfg = store.snapshot()
                state = logic.build_state(cfg)
            else:
                cfg = await store.read_event(target)
                state = logic.build_state(cfg, historical=True)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    # 「当前直播」显式**真探一次**：这是有人此刻要答案的场合（最多等 3 秒，单飞锁防惊群）
    live_info = await live.collect_live() if key == "live" else None
    result = await asyncio.to_thread(
        qqbot.dispatch,
        key,
        settings=settings,
        cfg=cfg,
        state=state,
        events=await store.list_events(),
        ref=ref,
        page=page,
        members=store.members(),
        live_info=live_info,
    )
    return {
        "ok": True,
        "kind": key,
        "eventId": target,
        "eventName": (cfg.event.name or cfg.event.title) if cfg else "",
        "page": result["page"],
        "pages": result["pages"],
        "parts": result["parts"],
        "text": "\n\n".join(result["parts"]),
    }
