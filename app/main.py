"""FastAPI 应用入口：REST API + WebSocket + 静态资源。

分层：``main`` 只做「路由 + 参数校验 + 调用 store/logic」，
业务规则集中在 ``logic``，持久化集中在 ``store``。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocketDisconnect

from . import avatars, league, live, logic, tournament
from .auth import auth, is_factory_key, sha256_hex
from .defaults import DEFAULT_ADMIN_KEY
from .logging_conf import get_logger, setup_logging
from .logic import build_state, joined_players, validate_config
from .models import MAX_SIDES, Channel, Config, NTEModel, Player, Round, SetScore, Team
from .store import PROJECT_ROOT, store
from .ws import hub

setup_logging()
log = get_logger("main")

STATIC_DIR = PROJECT_ROOT / "static"
SESSION_HEADER = "X-NTE-Token"

# --------------------------------------------------------------------------- #
# 边缘缓存（面向「德国源站 + EdgeOne 全球加速」的部署形态）
#
# 思路：静态资源带内容版本号 -> 可 immutable 长缓存，回源几乎为 0；
#       /api/** 一律 no-store，避免边缘把动态状态缓存住；
#       index.html 短缓存（no-cache），保证版本号变更后能立刻生效。
# 中间件用纯 ASGI 实现，不对响应体做缓冲，避免影响直播流式代理。
# --------------------------------------------------------------------------- #
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
_NO_STORE = "no-store"
_NO_CACHE = "no-cache"
_VERSIONED_STATIC_RE = re.compile(r"^/static/v/[0-9a-f]{6,}/(?P<rest>.+)$")


def _compute_asset_version() -> str:
    """按静态资源的相对路径 / 大小 / mtime 计算版本号。"""
    digest = hashlib.sha1()
    if STATIC_DIR.exists():
        for path in sorted(STATIC_DIR.rglob("*")):
            if path.is_file():
                stat = path.stat()
                digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
                digest.update(str(stat.st_size).encode())
                digest.update(str(stat.st_mtime_ns).encode())
    return digest.hexdigest()[:10]


def asset_version() -> str:
    """每次都按当前静态文件重算版本号。

    静态文件就十来个，stat 一遍的代价可以忽略，换来的是「改完前端刷新页面
    就生效」：版本变了 → 首页里注入的 ``/static/v/<版本>/…`` 跟着变 → 浏览器
    自然去取新包，**进程不重启也不会再继续发旧的 JS / CSS**。
    （以前只在 NTE_RELOAD=1 时才重算，开发时极易踩到「代码改了但页面还跑旧包」。）
    """
    return _compute_asset_version()


class EdgeCacheMiddleware:
    """还原版本化静态路径，并按路径类别写入缓存策略。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        mode: str | None = None
        match = _VERSIONED_STATIC_RE.match(path)
        if match is not None:
            scope = dict(scope)
            scope["path"] = "/static/" + match.group("rest")
            scope["raw_path"] = scope["path"].encode()
            mode = "immutable"
        elif path.startswith(("/api/", "/ws")):
            mode = "no-store"

        if mode is None:
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # 已有显式策略的路由（如头像）保持不动
                if "cache-control" not in headers:
                    headers["Cache-Control"] = _IMMUTABLE_CACHE if mode == "immutable" else _NO_STORE
            await send(message)

        await self.app(scope, receive, send_with_cache)


_index_cache: tuple[str, str] | None = None


def render_index() -> HTMLResponse:
    """输出注入了资源版本号的首页（进程内缓存，版本变化时失效）。"""
    global _index_cache
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>静态页面缺失</h1>", status_code=500)
    version = asset_version()
    if _index_cache is None or _index_cache[0] != version:
        html = page.read_text(encoding="utf-8").replace("/static/", f"/static/v/{version}/")
        _index_cache = (version, html)
        log.info("已注入静态资源版本 | version=%s", version)
    return HTMLResponse(_index_cache[1], headers={"Cache-Control": _NO_CACHE})


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class AuthPayload(NTEModel):
    key: str = ""


class TeamsFormPayload(NTEModel):
    """随机组队参数（seed 相同则结果相同）。"""

    seed: int | None = None
    team_size: int = 0             # 每队人数，0 = 沿用赛制配置（默认 2）
    merge_remainder: bool = False  # 零头平均并入前排队伍（不落下任何人）


class TeamsPayload(NTEModel):
    teams: list[Team] = Field(default_factory=list)


class TournamentPayload(NTEModel):
    """生成赛程参数；队伍不存在时会顺带随机组队。"""

    seed: int | None = None
    size: int = 0            # 淘汰赛规模（2 的幂），0 = 自动取最大可行值
    teams_per_match: int = 0      # 小组赛每场同场队伍数 2/3/4，0 = 沿用配置
    loser_bracket: bool | None = None  # None = 沿用配置；False = 单败（输一场即淘汰）
    reform: bool = False          # 先重新随机组队再排赛程（「快速创建分组」）
    team_size: int = 0            # 每队人数，0 = 沿用配置
    group_count: int = -1         # 小组数，-1 = 沿用配置，0 = 自动


class SchedulePayload(NTEModel):
    """积分制赛程生成参数。``mode``: rotate（动态轮换）/ fixed（固定队伍）。"""

    mode: str = "rotate"
    total_rounds: int = 0
    seed: int | None = None


class GroupPairingChange(NTEModel):
    """一局小组赛的新阵容：按 side 顺序给出队伍 ID。"""

    code: str
    team_ids: list[str] = Field(default_factory=list)


class GroupPairingsPayload(NTEModel):
    """开赛前手动调整小组赛对阵。``reset=True`` 时按算法重排（放弃手改）。"""

    rounds: list[GroupPairingChange] = Field(default_factory=list)
    reset: bool = False


class ScheduleAppendPayload(NTEModel):
    count: int = 1
    seed: int | None = None


class RoundSwapPayload(NTEModel):
    from_id: str
    to_id: str


class RoundLineupPayload(NTEModel):
    side: str
    player_ids: list[str] = Field(default_factory=list)


class FormatPayload(NTEModel):
    format: str = "tournament"


class RoundStatusPayload(NTEModel):
    status: str


class SideResultPayload(NTEModel):
    """一方（一支队伍）的比赛结果。``key`` 留空则按数组顺序取 A/B/C/D。"""

    key: str = ""
    score: int = 0            # 比分（2 队 = 局分；多队同场 = 该场得分）
    points: int = 0           # 小分 / 细则分（可选）
    team_id: str = ""
    player_ids: list[str] = Field(default_factory=list)


class RoundResultPayload(NTEModel):
    """录入比赛结果——三种填法都支持，服务端「能填就自动判定」：

    * ``sets``：各局小分（最自然，自动推出局分与总得分），仅 2 队有意义；
    * ``sides``：各方比分 / 得分（多队同场用这个）；
    * ``scoreA`` / ``scoreB``：早期字段，仍兼容。

    ``winner`` 留空即自动判定；显式指定则以其为准。
    """

    winner: str = ""
    note: str = ""
    sets: list[SetScore] = Field(default_factory=list)
    sides: list[SideResultPayload] = Field(default_factory=list)
    score_a: int | None = None
    score_b: int | None = None
    duration_minutes: int | None = None
    # 时间：None = 不指定（沿用已有值 / 用当前时间补全）；空串 = 清空
    started_at: str | None = None
    finished_at: str | None = None


class RoundLivePayload(NTEModel):
    """本场直播开关与直播选手提示。"""

    enabled: bool = False
    note: str = ""


class RoundWalkoverPayload(NTEModel):
    """判某一方弃权（长期没人 / 人数不足），其余各方自动晋级。"""

    side: str = ""            # A / B / C / D
    reason: str = "弃权"


class RoundTimesPayload(NTEModel):
    """单独登记比赛时间；字段为 None 表示不改动，空串表示清除。"""

    scheduled_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class AvatarUploadPayload(NTEModel):
    data_url: str = ""


class EventCreatePayload(NTEModel):
    name: str = ""
    copy_roster: bool = False
    format: str = "tournament"     # league=积分制 / tournament=锦标赛制


class EventMetaPayload(NTEModel):
    name: str = ""
    status: str = ""


class AdminKeyPayload(NTEModel):
    key: str = ""
    store_hash: bool = True


class ParticipantsPayload(NTEModel):
    """本届参与名单。``player_ids`` 为空表示未指定（视为全员参与）。"""

    player_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 鉴权依赖
# --------------------------------------------------------------------------- #
async def require_admin(request: Request, x_nte_token: str | None = Header(default=None)) -> str:
    token = x_nte_token or request.query_params.get("token")
    if not auth.check(token):
        raise HTTPException(status_code=401, detail="管理会话无效或已过期，请重新输入 KEY")
    return token or ""


# --------------------------------------------------------------------------- #
# 状态组装
# --------------------------------------------------------------------------- #
def build_public_state(cfg: Config) -> dict[str, Any]:
    """公开状态 = 业务状态 + 当前届次标识（多届赛事用）。

    ``channels``（成员频道）是**全局**的：跟当前看哪一届无关，所以不走
    ``build_state``，而是每次从这里挂上去。
    """
    state = build_state(cfg)
    state["eventId"] = store.current_id
    state["eventName"] = cfg.event.name or cfg.event.title
    state["eventStatus"] = cfg.event.status
    state["channels"] = logic.channel_views(cfg, store.channels())
    # 频道板块的公告（全局，纯展示）：放异环相关的说明 / 活动文案
    state["channelNotice"] = store.channel_notice()
    return state


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()

    async def on_config_change(cfg: Config, source: str) -> None:
        await hub.broadcast_state(build_public_state(cfg))
        for issue in validate_config(cfg):
            log.warning("配置提示 | source=%s | %s", source, issue)

    store.on_change(on_config_change)
    # 启动时先算一次，保证新连接的客户端立刻拿到数据
    await hub.broadcast_state(build_public_state(store.snapshot()))
    # 直播探测按需触发（前端在直播 / 频道页请求 /api/live/health 时才探一次），
    # 因此这里不启动任何常驻任务，没人看直播时后端不做任何探测。
    cfg = store.snapshot()
    log.info("=" * 68)
    log.info("NTE 比赛平台已启动 | 当前届: %s (%s)", cfg.event.name, store.current_id)
    log.info("数据库: %s", store.path)
    log.info("本机访问: http://127.0.0.1:%s", os.getenv("NTE_PORT", "8000"))
    log.info("=" * 68)
    if is_factory_key(cfg.admin):
        log.warning("-" * 68)
        log.warning("初始管理 KEY：%s", DEFAULT_ADMIN_KEY)
        log.warning("首次登录后请到「管理端 → 管理 KEY」修改；修改后此处不再显示。")
        log.warning("以后若忘记 KEY：停止服务后执行 `uv run python -m app --reset-key`。")
        log.warning("-" * 68)
    else:
        log.info(
            "管理 KEY 已自定义（此处不再显示）；忘记时可停止服务后执行 `uv run python -m app --reset-key` 重置。"
        )
    try:
        yield
    finally:
        await store.stop()
        await avatars.aclose()
        # 先收掉还没跑完的探测任务，再关连接池：否则它可能在关池的瞬间发起请求
        await live.stop_refresher()
        await live.aclose()
        log.info("服务已停止")


app = FastAPI(
    title="NTE 比赛",
    description="NTE 比赛（异环）通用赛事平台：自动分组 / 积分结算 / 实时排行 / 直播推流",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

_origins = os.getenv("NTE_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",") if o.strip()] or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Location"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(EdgeCacheMiddleware)

app.include_router(live.router)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 400:
        log.debug("HTTP %s | %s %s | %s", exc.status_code, request.method, request.url.path, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail, "status": exc.status_code},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    log.info("参数校验失败 | %s %s | %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "请求参数不合法", "detail": json.loads(json.dumps(exc.errors(), default=str))},
    )


@app.exception_handler(ValueError)
async def business_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """业务校验失败（人数不足、时间不合法等）统一回 400 JSON。

    兜底用：漏掉 try/except 的校验不会再变成没有响应体的 500，
    前端始终能拿到 ``{error}`` 文案。
    """
    log.info("业务校验失败 | %s %s | %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": str(exc), "status": 400},
    )


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    started = time.perf_counter()
    cfg = store.snapshot()
    state = build_public_state(cfg)
    state["live"] = live.stream_endpoints()
    # 以下几项**只读缓存**（探测由直播 / 频道页按需触发，见 live.kick_refresh）：
    # 本接口是页面首屏的必经之路，绝不能因为媒体服务器不可达而卡住。
    #
    # 主直播间（默认流名）有没有人在推流；None = 查不到（API 未配置 / 不可达）
    state["live"]["streaming"] = await live.main_stream_ready()
    # 「正在推流」以**媒体服务器上报**为准（MediaMTX /v3/paths/list 的 ready）：
    # 只有真的有人在推的机位才会被标成直播中；查不到就是没有。
    state["livePlayers"] = await live.streaming_player_ids(cfg)
    # 成员频道里正在推流的（全局）：前端据此给「直播中」标记
    state["liveChannels"] = await live.streaming_channel_ids()
    state["liveStatus"] = await live.live_status_view()
    state["server"] = {
        "ws": hub.size,
        "revision": cfg.revision,
        "ts": cfg.updated_at,
        "eventId": store.current_id,
    }
    # 这个接口是页面首屏的必经之路，超过 1s 就留一条日志便于排查
    # （直播探测已改为后台执行，这里慢基本只剩数据库 / 组装开销）。
    elapsed = time.perf_counter() - started
    if elapsed > 1.0:
        log.warning("状态组装耗时较长 | %.2fs | revision=%d", elapsed, cfg.revision)
    return state


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    cfg = store.snapshot()
    return {
        "ok": True,
        "revision": cfg.revision,
        "players": len(cfg.players),
        "participants": len(joined_players(cfg)),
        "participantsSet": bool(cfg.participants),
        "rounds": len(cfg.rounds),
        "ws": hub.stats(),
        "avatarCache": avatars.cache_stats(),
        "issues": validate_config(cfg),
    }


@app.get("/api/diagnostics")
async def api_diagnostics(_: str = Depends(require_admin)) -> dict[str, Any]:
    cfg = store.snapshot()
    return {
        "databasePath": str(store.path),
        "eventId": store.current_id,
        "eventName": cfg.event.name or cfg.event.title,
        "eventStatus": cfg.event.status,
        "eventCount": await store.event_count(),
        "revision": cfg.revision,
        "updatedAt": cfg.updated_at,
        "ws": hub.stats(),
        "avatarCache": avatars.cache_stats(),
        "live": live.stream_endpoints(),
        "issues": validate_config(cfg),
        "adminKeyMode": "sha256" if cfg.admin.key_sha256 else "plain",
    }


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
@app.post("/api/auth")
async def api_auth(payload: AuthPayload) -> dict[str, Any]:
    cfg = store.snapshot()
    if not auth.verify_key(payload.key, cfg.admin):
        log.warning("管理 KEY 校验失败，拒绝登录")
        raise HTTPException(status_code=401, detail="KEY 不正确")
    session = auth.issue()
    return {"ok": True, "token": session.token, "expiresAt": int(session.expires_at)}


@app.post("/api/auth/logout")
async def api_logout(x_nte_token: str | None = Header(default=None)) -> dict[str, Any]:
    auth.revoke(x_nte_token)
    return {"ok": True}


@app.get("/api/auth/check")
async def api_auth_check(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {"ok": True}


@app.post("/api/admin/key")
async def api_admin_key(payload: AdminKeyPayload, _: str = Depends(require_admin)) -> dict[str, Any]:
    """更新管理 KEY。默认只写 sha256，不留明文；更新后注销全部会话。"""
    raw = (payload.key or "").strip()
    if len(raw) < 6:
        raise HTTPException(status_code=400, detail="管理 KEY 至少 6 位")
    patch = (
        {"admin": {"key": "", "keySha256": sha256_hex(raw)}}
        if payload.store_hash
        else {"admin": {"key": raw, "keySha256": ""}}
    )
    await store.update(patch, actor="web:admin-key")
    revoked = auth.revoke_all()
    log.warning("管理 KEY 已更新 | 模式=%s | 已注销会话=%d", "sha256" if payload.store_hash else "plain", revoked)
    return {"ok": True, "mode": "sha256" if payload.store_hash else "plain", "reauth": True}


# --------------------------------------------------------------------------- #
# 赛事届次（多届赛事：记录 / 查看 / 管理）
# --------------------------------------------------------------------------- #
@app.get("/api/events")
async def api_events() -> dict[str, Any]:
    """届次列表（公开）：名称、状态、时间、规模与冠军。"""
    return {"current": store.current_id, "events": await store.list_events()}


@app.get("/api/events/{event_id}/state")
async def api_event_state(event_id: str) -> dict[str, Any]:
    """只读查看某一届的完整战绩（不影响当前届）。"""
    try:
        cfg = await store.read_event(event_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 往届回看：比赛一律按「没有直播」渲染（结束的比赛不可能在直播）
    state = build_state(cfg, historical=True)
    state["eventId"] = event_id
    state["eventName"] = cfg.event.name or cfg.event.title
    state["eventStatus"] = cfg.event.status
    state["readOnly"] = event_id != store.current_id
    # 成员频道是全局的：回看往届时也照样展示（它们不属于任何一届）
    state["channels"] = logic.channel_views(cfg, store.channels())
    state["liveChannels"] = await live.streaming_channel_ids()
    state["channelNotice"] = store.channel_notice()
    return state


@app.post("/api/events")
async def api_event_create(
    payload: EventCreatePayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """新建一届并切换过去；可选沿用当前届的名单与规则，并选择赛制。"""
    fmt = (payload.format or "").strip()
    if fmt not in ("", "league", "tournament"):
        raise HTTPException(status_code=400, detail="赛制只能是 league（积分制）或 tournament（锦标赛制）")
    cfg = await store.create_event(
        payload.name, copy_roster=payload.copy_roster, fmt=fmt or "tournament"
    )
    log.warning("已新建届次 | id=%s | 赛制=%s", store.current_id, cfg.rules.format)
    return {
        "ok": True,
        "eventId": store.current_id,
        "name": cfg.event.name,
        "format": cfg.rules.format,
    }


@app.post("/api/format")
async def api_set_format(payload: FormatPayload, _: str = Depends(require_admin)) -> dict[str, Any]:
    """切换本届赛制。两套规则的赛程互不通用，因此切换会清空现有对局（比分一并清除）。"""
    fmt = (payload.format or "").strip()
    if fmt != store.snapshot().rules.format:
        # 赛制切换会清空赛程，属于结构性改动 → 开赛后禁止
        _require_unlocked("赛制")
    if fmt not in ("league", "tournament"):
        raise HTTPException(status_code=400, detail="赛制只能是 league（积分制）或 tournament（锦标赛制）")
    if fmt == store.snapshot().rules.format:
        return {"ok": True, "format": fmt, "revision": store.revision, "changed": False}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "rules": {**data.get("rules", {}), "format": fmt}, "rounds": []}

    cfg = await store.mutate(_mutate, actor="web:format")
    log.warning("已切换赛制 | 届=%s | format=%s", store.current_id, fmt)
    return {
        "ok": True,
        "format": fmt,
        "changed": True,
        "revision": cfg.revision,
        "state": build_public_state(cfg),
    }


@app.post("/api/events/{event_id}/switch")
async def api_event_switch(event_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """把某一届设为当前进行中的赛事。"""
    try:
        cfg = await store.switch_event(event_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "eventId": store.current_id, "name": cfg.event.name}


@app.patch("/api/events/{event_id}")
async def api_event_meta(
    event_id: str, payload: EventMetaPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """重命名某一届，或标记 draft / active / closed。"""
    try:
        entry = await store.update_event_meta(event_id, {"name": payload.name, "status": payload.status})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "event": entry, "current": store.current_id}


@app.delete("/api/events/{event_id}")
async def api_event_delete(event_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """删除一届（至少保留一届）。"""
    try:
        await store.delete_event(event_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "current": store.current_id}


# --------------------------------------------------------------------------- #
# 管理端专用：隐私字段与推流地址
#
# 公开状态里**不含** UUID / QQ / 推流流名 / WHIP 推流地址；
# 管理端登录后从这里单独取，避免把推流凭据广播给所有在线客户端。
# --------------------------------------------------------------------------- #
@app.get("/api/private")
async def api_private(_: str = Depends(require_admin)) -> dict[str, Any]:
    """选手隐私字段 + 推流 / 播放地址（仅管理端）。

    推流标识规则：**只有选手自己的唯一流名**（``tom``），地址整届固定不变
    ——``https://live.shiyora.net:8889/tom/whip``，换比赛不用改。
    对局里只回「本场有哪些选手的机位」，不含任何按比赛编号的推流地址。
    """
    cfg = store.snapshot()
    stream = cfg.stream
    by_id = {p.id: p for p in cfg.players}

    players: dict[str, Any] = {}
    keys_seen: dict[str, list[str]] = {}
    for player in cfg.players:
        if player.stream_key:
            keys_seen.setdefault(player.stream_key, []).append(player.display_name)
        key = logic.player_stream_key(player)
        players[player.id] = {
            "id": player.id,
            "uuid": player.uuid,
            "qq": player.qq,
            "streamKey": player.stream_key,
            "note": player.note,
            # 该选手自己的固定地址（两套协议都有）：换比赛不用重新推
            "endpoints": logic.key_endpoints(stream, key) if key else {},
            "push": logic.push_endpoints(stream, key) if key else {},
        }

    rounds: dict[str, Any] = {}
    for rnd in cfg.rounds:
        cast: list[dict[str, Any]] = []
        for pid in logic.round_player_ids(rnd):
            player = by_id.get(pid)
            if player is None or not player.stream_key:
                continue
            pkey = logic.player_stream_key(player)
            cast.append(
                {
                    "playerId": pid,
                    "name": player.name or pid,
                    "key": pkey,
                    # 该选手自己固定的地址（不随比赛变化，含推流凭据，仅管理端）
                    "endpoints": logic.key_endpoints(stream, pkey),
                    "push": logic.push_endpoints(stream, pkey),
                    "play": logic.play_endpoints(stream, pkey),
                }
            )
        # 对局只回「本场有哪些选手的机位」——推流地址只按选手区分，
        # 没有按比赛编号的推流地址，选手换比赛不用改地址
        rounds[rnd.code or str(rnd.index)] = {
            "label": rnd.label or rnd.code,
            "status": rnd.status,
            "live": bool(rnd.live),
            "liveNote": rnd.live_note,
            "cast": cast,
        }

    # 成员频道（日常直播）：同样是隐私与推流凭据，只在管理端下发
    channels: dict[str, Any] = {}
    for channel in store.channels():
        ckey = logic.clean_key(channel.stream_key)
        channels[channel.id] = {
            "id": channel.id,
            "qq": channel.qq,
            "streamKey": channel.stream_key,
            "endpoints": logic.key_endpoints(stream, ckey) if ckey else {},
            "push": logic.push_endpoints(stream, ckey) if ckey else {},
        }

    return {
        "players": players,
        "channels": channels,
        "rounds": rounds,
        # **完整**直播配置（含 WHIP 推流地址、HLS 根地址与控制 API 凭据）：
        # 公开状态里这些字段被白名单剥掉了，管理端表单必须从这里取，
        # 否则表单是空的、一保存就把根地址清成空字符串。
        "stream": stream.dump(),
        # 推流 / 观看的协议列定义（WebRTC / HLS），前端据此渲染复制表格
        "protocols": logic.protocol_sets(stream),
        "baseUrl": stream.base_url,
        "enabled": stream.enabled,
        # 未指定选手时的兜底推流地址（默认流名）
        "defaultPush": logic.push_endpoints(stream, stream.stream_key),
        # 流名重复检查（保存时会拦，这里兜住历史数据）
        "duplicates": {k: v for k, v in keys_seen.items() if len(v) > 1},
    }


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
_PROTECTED_PATCH_KEYS = {"revision", "updatedAt", "version"}


def event_locked() -> bool:
    """比赛是否已开始（赛制与参赛名单冻结）。"""
    return bool(store.snapshot().event.locked)


def _require_unlocked(what: str = "赛制与参赛名单") -> None:
    """比赛开始后拒绝改动赛制 / 名单 / 组队 / 赛程重建。

    直播开关、替补换人、录分与时间登记都不走这里——它们随时可用。
    """
    if event_locked():
        raise HTTPException(
            status_code=409,
            detail=(
                f"比赛已开始，{what}已锁定，不能再调整。"
                "如需修改，请先在「比赛状态」面板里解除锁定（需二次确认）。"
            ),
        )


# 这些字段只能通过 /api/event/start 与 /api/event/unlock 改动（强制二次确认）
_LOCK_PATCH_KEYS = ("locked", "lockedAt")


@app.put("/api/config")
async def api_update_config(
    request: Request,
    patch: dict[str, Any] = Body(...),  # noqa: B008  (FastAPI 依赖注入惯例)
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    clean = {k: v for k, v in patch.items() if k not in _PROTECTED_PATCH_KEYS}
    if not clean:
        raise HTTPException(status_code=400, detail="没有需要更新的内容")
    # 赛制与参赛名单在开赛后冻结；其它配置（直播、界面、赛事信息文案）随时可改
    for key, label in (("rules", "赛制"), ("participants", "参赛名单")):
        if key in clean:
            _require_unlocked(label)
    # 赛事信息里的起止时间统一校验并规整（结束时间留空 = 尚未结束/待定）
    event_patch = clean.get("event")
    if isinstance(event_patch, dict):
        current = store.snapshot().event
        try:
            start = logic.check_time(event_patch.get("startTime", current.start_time), "开赛时间")
            end = logic.check_time(event_patch.get("endTime", current.end_time), "结束时间")
            logic.check_order(start, end, "开赛时间", "结束时间")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 锁定状态只能用 /api/event/start 与 /api/event/unlock 改，避免绕过二次确认
        kept = {
            k: v
            for k, v in event_patch.items()
            if k not in _LOCK_PATCH_KEYS and k not in ("locked_at",)
        }
        clean["event"] = {**kept, "startTime": start, "endTime": end}
    # 批量保存选手时同样要保证推流流名唯一，否则两位选手会推到同一个地址
    players_patch = clean.get("players")
    if isinstance(players_patch, list):
        seen_keys: dict[str, str] = {}
        for item in players_patch:
            key = str((item or {}).get("streamKey") or "").strip()
            if not key:
                continue
            who = str((item or {}).get("name") or (item or {}).get("id") or "选手")
            if key in seen_keys:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"推流流名「{key}」重复（{seen_keys[key]} 与 {who}），"
                        "请为每位选手指定互不相同的流名"
                    ),
                )
            seen_keys[key] = who
    updated = await store.update(clean, actor="web:config")
    return {"ok": True, "revision": updated.revision, "state": build_public_state(updated)}


class EventStartPayload(NTEModel):
    """开赛确认。``confirm`` 必须为 True——前端弹窗里勾选后才置位，防手滑。"""

    confirm: bool = False
    set_start_time: bool = True     # 开赛时间留空时用当前时间补上


class EventUnlockPayload(NTEModel):
    """解除锁定（同样要二次确认）。"""

    confirm: bool = False
    reason: str = ""


def _event_lock_patch(data: dict[str, Any], locked: bool, at: str = "") -> dict[str, Any]:
    """写入事件的锁定状态（保持其它字段不动）。"""
    event = dict(data.get("event") or {})
    event["locked"] = locked
    event["lockedAt"] = at
    if locked:
        # 开赛视为届状态进入「进行中」；已经标记结束的届不去覆盖
        if event.get("status") in (None, "", "draft"):
            event["status"] = "active"
        # 开赛时间留空就用锁定时刻补上，界面上的「何时开始」才不会是空的
        if not str(event.get("startTime") or "").strip():
            event["startTime"] = at
    return {**data, "event": event}


@app.post("/api/event/start")
async def api_event_start(
    payload: EventStartPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """开始比赛：**锁定赛制与参赛名单**（需二次确认）。

    锁定后仍然可以做这些事：

    * 开 / 关每场比赛的直播推流；
    * 替补换人——换上的人即使不在参与名单里也会**自动加入**；
    * 录分、改时间、重置比分、赛事信息与界面配置。

    会被拒绝的是结构性改动：赛制切换、每队/每场人数、败者组开关、
    参赛名单、重新组队、赛程重建与清空（都会回 409 并说明原因）。
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="开始比赛需要二次确认（未勾选确认）")
    cfg_now = store.snapshot()
    if cfg_now.event.locked:
        return {
            "ok": True,
            "revision": cfg_now.revision,
            "locked": True,
            "changed": False,
            "lockedAt": cfg_now.event.locked_at,
            "warnings": [],
        }
    warnings: list[str] = []
    if not cfg_now.rounds:
        warnings.append("还没有生成赛程：锁定后赛程重建会被拒绝，建议先生成赛程再开赛。")
    if cfg_now.rules.format == "tournament" and not cfg_now.teams:
        warnings.append("锦标赛制还没有组队：锁定后组队会被拒绝，建议先随机组队再开赛。")
    if not joined_players(cfg_now):
        warnings.append("本届参与名单是空的（且没有可用选手），请先确认名单。")

    from .store import now_iso

    at = now_iso()
    cfg = await store.mutate(
        lambda data: _event_lock_patch(data, True, at), actor="web:event-start"
    )
    log.warning(
        "比赛已开始（赛制与名单锁定） | 届=%s | 时间=%s | 赛制=%s | 选手=%d | 队伍=%d | 对局=%d",
        store.current_id,
        at,
        cfg.rules.format,
        len(cfg.players),
        len(cfg.teams),
        len(cfg.rounds),
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "locked": True,
        "changed": True,
        "lockedAt": at,
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


@app.post("/api/event/unlock")
async def api_event_unlock(
    payload: EventUnlockPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """解除锁定：恢复对赛制 / 名单 / 组队 / 赛程的修改权限（需二次确认）。

    已录入的比分与结果不会因此丢失；只是重新允许结构性调整。
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="解除锁定需要二次确认（未勾选确认）")
    cfg_now = store.snapshot()
    if not cfg_now.event.locked:
        return {
            "ok": True,
            "revision": cfg_now.revision,
            "locked": False,
            "changed": False,
            "warnings": [],
        }
    cfg = await store.mutate(
        lambda data: _event_lock_patch(data, False, ""), actor="web:event-unlock"
    )
    log.warning(
        "已解除比赛锁定（可再次调整赛制与名单） | 届=%s | 原因=%s",
        store.current_id,
        (payload.reason or "").strip() or "（未填写）",
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "locked": False,
        "changed": True,
        "warnings": [],
        "state": build_public_state(cfg),
    }


@app.post("/api/reload")
async def api_reload(_: str = Depends(require_admin)) -> dict[str, Any]:
    cfg = await store.reload(reason="web")
    return {"ok": True, "revision": cfg.revision}


@app.get("/api/export")
async def api_export(_: str = Depends(require_admin)) -> Response:
    """导出当前届为 JSON（备份 / 迁移用；也可作为导入他处的快照）。"""
    cfg = store.snapshot()
    payload = json.dumps(cfg.dump(), ensure_ascii=False, indent=2)
    filename = f"{store.current_id}-{cfg.event.name or 'event'}.json"
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# --------------------------------------------------------------------------- #
# 选手
# --------------------------------------------------------------------------- #
@app.post("/api/players")
async def api_upsert_player(payload: Player, _: str = Depends(require_admin)) -> dict[str, Any]:
    player = payload.model_copy()
    if not player.id:
        existing = {p.id for p in store.snapshot().players}
        seq = 1
        while f"p{seq:02d}" in existing:
            seq += 1
        player.id = f"p{seq:02d}"
    if not player.name:
        raise HTTPException(status_code=400, detail="选手名称不能为空")
    # 推流流名必须唯一：重复会让两位选手推到同一个地址（串流），保存前先查库
    if player.stream_key:
        clash = next(
            (
                p
                for p in store.snapshot().players
                if p.id != player.id and p.stream_key == player.stream_key
            ),
            None,
        )
        if clash is not None:
            raise HTTPException(
                status_code=400,
                detail=f"推流流名「{player.stream_key}」已被 {clash.display_name} 使用，请换成互不相同的流名",
            )

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        players = data.setdefault("players", [])
        dumped = player.dump()
        for idx, item in enumerate(players):
            if item.get("id") == player.id:
                players[idx] = {**item, **dumped}
                return data
        players.append(dumped)
        return data

    cfg = await store.mutate(_mutate, actor="web:player-upsert")
    return {"ok": True, "revision": cfg.revision, "player": player.dump()}


@app.delete("/api/players/{player_id}")
async def api_delete_player(player_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """删除选手（比赛开始后禁止，改由替补换人调整）。

    新增 / 编辑选手**不受锁定限制**——替补可能是一位全新的人，
    需要先建好档案才能换上。
    """
    _require_unlocked("选手名单（删除）")

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        data["players"] = [p for p in data.get("players", []) if p.get("id") != player_id]
        for rnd in data.get("rounds", []):
            for side in _raw_sides(rnd):
                side["playerIds"] = [pid for pid in side.get("playerIds", []) if pid != player_id]
        for team in data.get("teams", []):
            team["playerIds"] = [pid for pid in team.get("playerIds", []) if pid != player_id]
        return data

    cfg = await store.mutate(_mutate, actor="web:player-delete")
    log.info("已删除选手 %s", player_id)
    return {"ok": True, "revision": cfg.revision}


# --------------------------------------------------------------------------- #
# 本届参与名单（报名池可预先录入所有人，每届再勾选实际参与者）
# --------------------------------------------------------------------------- #
@app.post("/api/participants")
async def api_set_participants(
    payload: ParticipantsPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """保存本届参与选手，并按新名单自动重排未开赛对局。

    * ``playerIds`` 传空数组表示「未指定」，视为全员参与；
    * ``reconcile`` 为 ``false`` 时只改名单、不动赛程；
    * 已完成 / 已锁定的对局永远不会被改动。

    比赛开始后名单冻结（替补由换人接口自动加入，不走这里）。
    """
    _require_unlocked("参赛名单")
    try:
        cfg, warnings = await store.set_participants(payload.player_ids, actor="web:participants")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    chosen = [p.id for p in joined_players(cfg)]
    log.info("参与名单已保存 | 届=%s | 参与=%d/%d 人", store.current_id, len(chosen), len(cfg.players))
    return {
        "ok": True,
        "revision": cfg.revision,
        "participants": chosen,
        "count": len(chosen),
        "explicit": bool(cfg.participants),
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


# --------------------------------------------------------------------------- #
# 成员频道（日常 / 非比赛直播；全局，跨届共享）
#
# 与赛事届次无关，因此**不受开赛锁定影响**，也不需要切换届次。
# --------------------------------------------------------------------------- #
@app.post("/api/channels")
async def api_channel_save(payload: Channel, _: str = Depends(require_admin)) -> dict[str, Any]:
    """新增 / 更新一个成员频道。

    * 流名（``streamKey``）**全局唯一**：与选手以及其它频道都不能重复（否则串流）；
    * 未给 id 时自动分配 ``c01`` 这类编号；
    * 成员频道跨届共享，开赛锁定不会拦住它。
    """
    channel = payload.model_copy()
    if not channel.name.strip():
        raise HTTPException(status_code=400, detail="频道名不能为空")
    key = logic.clean_key(channel.stream_key)
    if key:
        clash_player = next(
            (p for p in store.snapshot().players if logic.clean_key(p.stream_key) == key), None
        )
        if clash_player is not None:
            raise HTTPException(
                status_code=400,
                detail=f"流名「{key}」已被选手 {clash_player.display_name} 使用，频道请换一个互不相同的流名",
            )
        clash_channel = next(
            (
                c
                for c in store.channels()
                if c.id != channel.id and logic.clean_key(c.stream_key) == key
            ),
            None,
        )
        if clash_channel is not None:
            raise HTTPException(
                status_code=400,
                detail=f"流名「{key}」已被频道 {clash_channel.display_name} 使用，请换成互不相同的流名",
            )
    saved = await store.save_channel(channel, actor="web:channel-save")
    return {
        "ok": True,
        "id": saved.id,
        "channel": logic.channel_view(store.snapshot(), saved),
        "state": build_public_state(store.snapshot()),
    }


class ChannelNoticePayload(NTEModel):
    """频道板块的公告文案（全局，纯展示，可留空清掉）。"""

    text: str = ""


@app.put("/api/channels/notice")
async def api_channel_notice(
    payload: ChannelNoticePayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """设置「频道」板块的公告 / 异环相关内容（全局，与届次无关）。"""
    text = await store.set_channel_notice(payload.text, actor="web:channel-notice")
    return {"ok": True, "notice": text, "state": build_public_state(store.snapshot())}


@app.delete("/api/channels/{channel_id}")
async def api_channel_delete(channel_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """删除一个成员频道。"""
    removed = await store.delete_channel(channel_id, actor="web:channel-delete")
    if not removed:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return {"ok": True, "state": build_public_state(store.snapshot())}


# --------------------------------------------------------------------------- #
# 赛程
# --------------------------------------------------------------------------- #
@app.post("/api/teams/auto")
async def api_teams_auto(payload: TeamsFormPayload, _: str = Depends(require_admin)) -> dict[str, Any]:
    """按本届参与名单随机分配队友，生成固定队伍（会清空现有赛程）。

    锦标赛制必须组队；积分制的「固定队伍」模式也使用这里的队伍。
    比赛开始后禁止重新组队（替补请用「替补换人」）。
    """
    _require_unlocked("组队")
    seed = payload.seed if payload.seed is not None else random.randrange(1_000_000)
    try:
        cfg, warnings = await store.form_teams(
            seed=seed,
            team_size=payload.team_size or None,
            merge_remainder=payload.merge_remainder,
            actor="web:teams-auto",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "revision": cfg.revision,
        "seed": seed,
        "teamSize": cfg.rules.team_size,
        "teams": [t.dump() for t in cfg.teams],
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


@app.put("/api/teams")
async def api_teams_update(payload: TeamsPayload, _: str = Depends(require_admin)) -> dict[str, Any]:
    """手动调整固定队伍成员；队伍结构变化时清空赛程以免对阵失效。

    比赛开始后禁止整体重排队伍（单个替补请用 ``/api/teams/{id}/substitute``）。
    """
    _require_unlocked("队伍成员")
    teams = [Team.model_validate(t) for t in payload.teams]
    known = {p.id for p in store.snapshot().players}
    seen: set[str] = set()
    for team in teams:
        if not team.player_ids:
            raise HTTPException(status_code=400, detail=f"{team.label or team.id} 还没有队员")
        unknown = [pid for pid in team.player_ids if pid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"{team.label} 含未知选手: {', '.join(unknown)}")
        dup = [pid for pid in team.player_ids if pid in seen]
        if dup:
            raise HTTPException(status_code=400, detail=f"选手重复出现在多支队伍: {', '.join(dup)}")
        seen.update(team.player_ids)

    before_ids = {t.id for t in store.snapshot().teams}
    dropped = before_ids != {t.id for t in teams}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        merged = {**data, "teams": [t.dump() for t in teams]}
        if dropped:
            merged["rounds"] = []
        return merged

    cfg = await store.mutate(_mutate, actor="web:teams-update")
    warnings = ["队伍结构已变化，原赛程已清空，请重新生成赛程。"] if dropped else []
    return {
        "ok": True,
        "revision": cfg.revision,
        "teams": [t.dump() for t in cfg.teams],
        "count": len(cfg.teams),
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


class TeamSubstitutePayload(NTEModel):
    """队伍内替补换人：把 ``from_id`` 换成 ``to_id``（1:1，队伍规模不变）。"""

    from_id: str
    to_id: str
    mark_substitute: bool = False    # 把换上的人标记为「替补」


@app.post("/api/teams/{team_id}/substitute")
async def api_team_substitute(
    team_id: str, payload: TeamSubstitutePayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """替补换人（锦标赛制）：**不重建赛程**，只换掉队伍里的一个人。

    与「组队台」的区别：

    * 队伍规模不变，因此既有的对阵结构依然有效，赛程不会被清空；
    * 该队**未结算**对局里的出场阵容会同步更新；
      已结算的对局保留当时实际出场的阵容与比分（只在返回里提示）；
    * 换上的人若不在本届参与名单中，会**自动加入**（这正是替补的用法）；
    * 比赛开始后依然可用（替补不受锁定限制）。
    """
    cfg_now = store.snapshot()
    team = next((t for t in cfg_now.teams if t.id == team_id), None)
    if team is None:
        raise HTTPException(
            status_code=404,
            detail=f"队伍 {team_id} 不存在（积分制请在对局里点选手换人）",
        )
    if payload.from_id == payload.to_id:
        raise HTTPException(status_code=400, detail="换下与换上不能是同一位选手")
    if payload.from_id not in team.player_ids:
        raise HTTPException(
            status_code=400, detail=f"被换下的选手不在这支队伍里（{team.label or team.id}）"
        )
    players = {p.id: p for p in cfg_now.players}
    if payload.to_id not in players:
        raise HTTPException(status_code=404, detail="替补选手不存在，请先在「选手名单」里新增")
    owner = next((t for t in cfg_now.teams if payload.to_id in t.player_ids), None)
    if owner is not None:
        raise HTTPException(
            status_code=400, detail=f"该选手已在「{owner.label or owner.id}」队中，不能重复加入"
        )

    added: list[str] = []
    kept: list[str] = []

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        roster: list[str] = []
        for item in data.get("teams", []):
            if item.get("id") != team_id:
                continue
            ids = list(item.get("playerIds") or [])
            ids[ids.index(payload.from_id)] = payload.to_id
            item["playerIds"] = ids
            roster = ids
        for rnd in data.get("rounds", []):
            for side in _raw_sides(rnd):
                if side.get("teamId") != team_id:
                    continue
                if rnd.get("status") == "done":
                    # 已经打完的比赛保留当时上场的阵容，不被后来的替补改写
                    kept.append(str(rnd.get("code") or rnd.get("index")))
                    continue
                side["playerIds"] = list(roster)
        added.extend(_ensure_participants(data, [payload.to_id]))
        if payload.mark_substitute:
            for item in data.get("players", []):
                if item.get("id") == payload.to_id:
                    item["substitute"] = True
        return data

    cfg = await store.mutate(_mutate, actor="web:team-substitute")
    out_name = players[payload.from_id].display_name
    in_name = players[payload.to_id].display_name
    log.warning(
        "替补换人 | 届=%s | 队伍=%s | %s → %s | 自动加入名单=%s | 已结算对局保留=%d",
        store.current_id,
        team.label or team.id,
        out_name,
        in_name,
        _names_of(added),
        len(kept),
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "teamId": team_id,
        "from": {"id": payload.from_id, "name": out_name},
        "to": {"id": payload.to_id, "name": in_name, "substitute": payload.mark_substitute},
        "addedToParticipants": _names_of(added),
        "keptRounds": kept,
        "state": build_public_state(cfg),
    }


@app.post("/api/tournament/generate")
async def api_tournament_generate(
    payload: TournamentPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """生成完整赛程：小组赛轮转 + 淘汰赛（覆盖现有对局与比分）。

    * ``size``：淘汰赛规模（2 的幂且不超过队伍数），留 0 自动取最大可行值；
    * ``teamsPerMatch``：小组赛每场同场队伍数（2/3/4）；
    * ``loserBracket``：开 = 双败淘汰，关 = 输一场即淘汰。
    """
    if store.snapshot().rules.format != "tournament":
        raise HTTPException(status_code=400, detail="锦标赛赛程仅用于锦标赛制，请先切换赛制")
    _require_unlocked("赛程重建")
    seed = payload.seed if payload.seed is not None else random.randrange(1_000_000)
    try:
        cfg, warnings = await store.generate_tournament(
            seed=seed,
            size=payload.size or None,
            teams_per_match=payload.teams_per_match or None,
            loser_bracket=payload.loser_bracket,
            reform=payload.reform,
            team_size=payload.team_size or None,
            group_count=payload.group_count if payload.group_count >= 0 else None,
            actor="web:tournament",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "revision": cfg.revision,
        "seed": seed,
        "count": len(cfg.rounds),
        "teams": len(cfg.teams),
        "size": cfg.rules.knockout_size,
        "teamsPerMatch": cfg.rules.teams_per_match,
        "loserBracket": cfg.rules.loser_bracket,
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


# --------------------------------------------------------------------------- #
# 小组赛对阵：开赛前手动调整（换对手 / 恢复默认）
#
# 只动「哪支队在那场的哪一侧」，不动场次数与编号：同一组同一轮里每支队仍然
# 只打一场，因此赛程结构、轮次标题、直播机位都照旧；交换只影响「谁碰谁」。
# --------------------------------------------------------------------------- #
def _group_key_of(rnd: dict[str, Any]) -> str:
    """小组赛对局的组名：优先看编号 ``G-A-1-1``，其次看标签 ``A 组 · …``。"""
    parts = str(rnd.get("code") or "").split("-")
    if len(parts) >= 2 and parts[0] == "G":
        return parts[1] or "A"
    label = str(rnd.get("label") or "").split(" ")[0].replace("组", "")
    return label or "A"


def _set_side_team(side: dict[str, Any], team: dict[str, Any]) -> None:
    """把一侧换成一支队：**阵容与显示名都要跟着换**（否则标签还是原来那支队）。"""
    side["teamId"] = str(team.get("id") or "")
    side["playerIds"] = [str(pid) for pid in (team.get("playerIds") or [])]
    side["label"] = str(team.get("short") or team.get("name") or team.get("id") or "")


def _group_rounds_by_key(rounds: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按组名归拢小组赛对局，组内按（轮次 → 场次）排序。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for rnd in rounds:
        if rnd.get("stage") == "group":
            out.setdefault(_group_key_of(rnd), []).append(rnd)
    for items in out.values():
        items.sort(key=lambda r: (int(r.get("bracketRound") or 0), int(r.get("slot") or 0)))
    return out


def _reset_group_pairings(data: dict[str, Any]) -> int:
    """把小组赛对阵恢复成算法默认排法（放弃手改），返回被改动的场次数。"""
    teams = {str(t.get("id")): t for t in (data.get("teams") or []) if t.get("id")}
    per_match = max(2, min(MAX_SIDES, int((data.get("rules") or {}).get("teamsPerMatch") or 2)))
    by_group = _group_rounds_by_key(data.get("rounds") or [])
    # 每组的队伍按**配置里的顺序**取：与生成赛程时的输入顺序一致，排出来就是默认表
    order: dict[str, list[str]] = {}
    for team in data.get("teams") or []:
        tid = str(team.get("id") or "")
        if tid:
            order.setdefault(str(team.get("group") or "A"), []).append(tid)
    changed = 0
    for key, items in by_group.items():
        plan = tournament.group_pairings(order.get(key) or [], per_match)
        if len(plan) != len(items):
            continue  # 数据与算法不一致（理论上不会）：保持原样更安全
        for rnd, match in zip(items, plan):
            sides = rnd.get("sides") or []
            if len(sides) != len(match):
                continue
            if [str(s.get("teamId") or "") for s in sides] != list(match):
                changed += 1
            for side, tid in zip(sides, match):
                _set_side_team(side, teams.get(tid, {}))
    return changed


def _validate_group_rounds(rounds: list[dict[str, Any]]) -> None:
    """同一组同一轮里每支队最多出场一次（客户端只做对调，正常不会触发）。"""
    seen: dict[tuple[str, int], set[str]] = {}
    for rnd in rounds:
        if rnd.get("stage") != "group":
            continue
        key = _group_key_of(rnd)
        slot = (key, int(rnd.get("bracketRound") or 0))
        bucket = seen.setdefault(slot, set())
        for side in rnd.get("sides") or []:
            tid = str(side.get("teamId") or "")
            if not tid:
                continue
            if tid in bucket:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} 组第 {slot[1]} 轮里 {tid} 出现了两次：同一轮每支队只能打一场",
                )
            bucket.add(tid)


@app.post("/api/tournament/group-pairings")
async def api_group_pairings(
    payload: GroupPairingsPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """**开赛前**手动调整小组赛对阵（换对手 / 恢复默认）。

    * 只允许「还没开打」时调：已锁定返回 409，小组赛已有任何结果返回 400；
    * 一次提交只换阵容，不改场次数与编号——同一组同一轮里每支队仍只打一场；
    * ``reset=true`` 按分组算法重排回默认（放弃手改）。
    """
    cfg_now = store.snapshot()
    if cfg_now.rules.format != "tournament":
        raise HTTPException(status_code=400, detail="小组赛对阵只用于锦标赛制")
    _require_unlocked("小组赛对阵")
    group_now = [r for r in cfg_now.rounds if r.stage == "group"]
    if not group_now:
        raise HTTPException(status_code=400, detail="本届还没有小组赛，请先「生成赛程」")
    played = next((r for r in group_now if tournament.round_has_result(r)), None)
    if played is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"小组赛已经开打（{played.label or played.code} 已有结果），对阵不能再改；"
                "要改请先「重置」这场比赛。"
            ),
        )
    if not payload.reset and not payload.rounds:
        raise HTTPException(status_code=400, detail="没有要调整的对阵")

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        rounds = data.get("rounds") or []
        teams = {str(t.get("id")): t for t in (data.get("teams") or []) if t.get("id")}
        by_code = {str(r.get("code") or ""): r for r in rounds if r.get("stage") == "group"}
        if payload.reset:
            _reset_group_pairings(data)
        for change in payload.rounds:
            rnd = by_code.get(change.code)
            if rnd is None:
                raise HTTPException(status_code=404, detail=f"小组赛对局 {change.code} 不存在")
            sides = rnd.get("sides") or []
            if len(change.team_ids) != len(sides):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{change.code} 需要 {len(sides)} 支队伍"
                        f"（收到 {len(change.team_ids)} 支），场次数与编号不能改"
                    ),
                )
            key = _group_key_of(rnd)
            for side, tid in zip(sides, change.team_ids):
                team = teams.get(tid)
                if team is None:
                    raise HTTPException(status_code=404, detail=f"队伍 {tid} 不存在")
                if str(team.get("group") or "A") != key:
                    raise HTTPException(
                        status_code=400, detail=f"队伍 {tid} 不在 {key} 组，不能排进这一组"
                    )
                _set_side_team(side, team)
        _validate_group_rounds(rounds)
        return data

    cfg = await store.mutate(_mutate, actor="web:group-pairings")
    log.info(
        "小组赛对阵已调整 | 届=%s | 手改 %d 局 | 恢复默认=%s",
        store.current_id,
        len(payload.rounds),
        payload.reset,
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "reset": bool(payload.reset),
        "changed": len(payload.rounds),
        "state": build_public_state(cfg),
    }


@app.post("/api/tournament/preview")
async def api_tournament_preview(
    payload: TournamentPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """**只读**预估赛程结构（不写库）：参赛人数 → 队伍数 → 小组 / 淘汰赛规模 → 场次。

    用于「快速创建分组」弹窗的实时预览：人少就自动少分组、淘汰赛从 8 强起步；
    人多则拉长赛程、从 16 / 32 强起步。
    """
    return logic.tournament_plan(
        store.snapshot(),
        team_size=payload.team_size or None,
        teams_per_match=payload.teams_per_match or None,
        loser_bracket=payload.loser_bracket,
        group_count=payload.group_count if payload.group_count >= 0 else None,
        knockout_size=payload.size or None,
    )


@app.post("/api/tournament/clear")
async def api_tournament_clear(_: str = Depends(require_admin)) -> dict[str, Any]:
    """清空赛程（保留固定队伍），用于重新编排。

    比赛开始后禁止——清空会把比分一起丢掉。
    """
    _require_unlocked("赛程清空")
    cfg = await store.clear_tournament(actor="web:tournament-clear")
    return {"ok": True, "revision": cfg.revision, "state": build_public_state(cfg)}


# --------------------------------------------------------------------------- #
# 积分制赛程（动态轮换 / 固定队伍 + 补赛 + 换人）
# --------------------------------------------------------------------------- #
def _require_league() -> None:
    if store.snapshot().rules.format != "league":
        raise HTTPException(status_code=400, detail="该接口仅用于积分制赛制，请先切换赛制")


@app.post("/api/schedule/generate")
async def api_schedule_generate(
    payload: SchedulePayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """积分制：生成动态轮换（或固定队伍）赛程，覆盖现有对局与比分。"""
    _require_league()
    _require_unlocked("赛程重建")
    try:
        cfg, warnings = await store.generate_league_schedule(
            mode=payload.mode, total_rounds=payload.total_rounds or None, seed=payload.seed
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    quality = league.quality_of_rounds(cfg.rounds)
    log.info(
        "生成积分制赛程 %d 局 | mode=%s | 重复搭档=%d",
        len(cfg.rounds),
        payload.mode,
        quality["partnerRepeats"],
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "count": len(cfg.rounds),
        "warnings": warnings,
        "quality": quality,
        "state": build_public_state(cfg),
    }


@app.post("/api/schedule/append")
async def api_schedule_append(
    payload: ScheduleAppendPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """积分制：追加补赛，优先安排出场次数最少的选手（不影响已有比分）。"""
    _require_league()
    count = max(1, min(int(payload.count or 1), 20))
    try:
        cfg, warnings = await store.append_league_rounds(count=count, seed=payload.seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "revision": cfg.revision,
        "added": count,
        "count": len(cfg.rounds),
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


@app.post("/api/rounds")
async def api_round_append(_: str = Depends(require_admin)) -> dict[str, Any]:
    """积分制：在赛程末尾追加一局空对局，供管理员手动编排。"""
    _require_league()

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        rounds = data.setdefault("rounds", [])
        idx = len(rounds) + 1
        rounds.append(
            {
                "index": idx,
                "code": f"L-{idx}",
                "stage": "league",
                "bracketRound": 1,
                "slot": idx,
                "status": "pending",
                "sides": [
                    {"playerIds": [], "score": 0, "points": 0, "rank": 0},
                    {"playerIds": [], "score": 0, "points": 0, "rank": 0},
                ],
            }
        )
        return data

    cfg = await store.mutate(_mutate, actor="web:round-append")
    log.info("已追加对局 | 当前共 %d 局", len(cfg.rounds))
    return {"ok": True, "revision": cfg.revision, "count": len(cfg.rounds)}


@app.delete("/api/rounds")
async def api_rounds_clear(_: str = Depends(require_admin)) -> dict[str, Any]:
    """清空**全部比赛**（两套赛制通用；比分一并丢弃，队伍与名单保留）。

    允许删到一场不剩——赛程为空是合法状态，之后可以重新生成。
    比赛开始后属于结构性改动，需先解除锁定。
    """
    _require_unlocked("赛程清空")
    before = len(store.snapshot().rounds)
    cfg = await store.clear_rounds(actor="web:rounds-clear")
    log.warning("已清空全部比赛 | 届=%s | 原场次=%d", store.current_id, before)
    return {
        "ok": True,
        "revision": cfg.revision,
        "removed": before,
        "count": 0,
        "state": build_public_state(cfg),
    }


@app.delete("/api/rounds/{ref}")
async def api_round_delete(ref: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """积分制：删除一局并重新编号，保持序号连续。"""
    _require_league()

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        rounds = [
            r
            for r in data.get("rounds", [])
            if str(r.get("code") or "") != ref and str(r.get("index")) != ref
        ]
        if len(rounds) == len(data.get("rounds", [])):
            raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")
        for pos, rnd in enumerate(rounds, start=1):
            rnd["index"] = pos
            rnd["code"] = f"L-{pos}"
            rnd["slot"] = pos
            rnd["label"] = ""      # 交由 round_view 按新序号生成
        data["rounds"] = rounds
        return data

    cfg = await store.mutate(_mutate, actor="web:round-delete")
    log.info("已删除对局 %s | 剩余 %d 局", ref, len(cfg.rounds))
    return {"ok": True, "revision": cfg.revision, "count": len(cfg.rounds)}


@app.post("/api/rounds/{ref}/swap")
async def api_round_swap(
    ref: str, payload: RoundSwapPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """积分制换人：把在场的 fromId 换成 toId。

    若 toId 在对面阵容中则两人互换，若不在本局则直接替换，
    因此「主替互换」与「跨队调换」都只用这一个接口。

    换上的人若不在本届参与名单里会**自动加入**（替补的常规用法）；
    比赛开始后依然可用（替补不受锁定限制）。
    """
    _require_league()
    cfg_now = store.snapshot()
    if payload.to_id not in {p.id for p in cfg_now.players}:
        raise HTTPException(status_code=404, detail="目标选手不存在")
    added: list[str] = []

    def apply(rnd: dict[str, Any]) -> None:
        sides = _raw_sides(rnd)
        from_side = next((s for s in sides if payload.from_id in s["playerIds"]), None)
        if from_side is None:
            raise HTTPException(status_code=400, detail="原选手不在本局阵容中")
        to_side = next((s for s in sides if payload.to_id in s["playerIds"]), None)
        if to_side is from_side:
            return
        if to_side is None:
            ids = from_side["playerIds"]
            ids[ids.index(payload.from_id)] = payload.to_id
        else:
            a_ids, b_ids = from_side["playerIds"], to_side["playerIds"]
            a_ids[a_ids.index(payload.from_id)] = payload.to_id
            b_ids[b_ids.index(payload.to_id)] = payload.from_id

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        _round_mutator(ref, apply)(data)
        # 替补不在参与名单里时自动加入（否则榜表与用户端名单会漏掉他）
        added.extend(_ensure_participants(data, [payload.to_id]))
        return data

    cfg = await store.mutate(_mutate, actor="web:round-swap")
    log.info(
        "对局 %s 换人 | %s -> %s | 自动加入名单=%s",
        ref,
        payload.from_id,
        payload.to_id,
        _names_of(added),
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "addedToParticipants": _names_of(added),
        "state": build_public_state(cfg),
    }


@app.post("/api/rounds/{ref}/lineup")
async def api_round_lineup(
    ref: str, payload: RoundLineupPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """积分制：直接设置某一侧的出场名单（用于空位补人 / 移出阵容）。

    新上场的选手若不在本届参与名单里会**自动加入**；开赛后依然可用。
    """
    _require_league()
    side_key = (payload.side or "A").upper()
    if side_key not in ("A", "B"):
        raise HTTPException(status_code=400, detail="side 只能是 A 或 B")
    cfg_now = store.snapshot()
    known = {p.id for p in cfg_now.players}
    unknown = [pid for pid in payload.player_ids if pid not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知选手: {', '.join(unknown)}")
    if len(payload.player_ids) > cfg_now.rules.team_size:
        raise HTTPException(
            status_code=400, detail=f"出场人数不能超过队伍人数 {cfg_now.rules.team_size}"
        )
    index = ord(side_key) - ord("A")

    def apply(rnd: dict[str, Any]) -> None:
        sides = _raw_sides(rnd)
        if index >= len(sides):
            raise HTTPException(status_code=400, detail="本局没有这一侧")
        others: set[str] = set()
        for i, side in enumerate(sides):
            if i != index:
                others |= set(side["playerIds"])
        conflict = others & set(payload.player_ids)
        if conflict:
            raise HTTPException(status_code=400, detail=f"选手已在其它队阵容: {', '.join(conflict)}")
        sides[index]["playerIds"] = list(payload.player_ids)

    added: list[str] = []

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        _round_mutator(ref, apply)(data)
        added.extend(_ensure_participants(data, list(payload.player_ids)))
        return data

    cfg = await store.mutate(_mutate, actor="web:round-lineup")
    if added:
        log.info("对局 %s 补人 | 自动加入名单=%s", ref, _names_of(added))
    return {
        "ok": True,
        "revision": cfg.revision,
        "addedToParticipants": _names_of(added),
        "state": build_public_state(cfg),
    }


def _find_round(cfg: Config, ref: str):
    """按对局编号（code，如 WB-1-2）或序号定位一场比赛。"""
    return next((r for r in cfg.rounds if r.code and r.code == ref), None) or next(
        (r for r in cfg.rounds if str(r.index) == ref), None
    )


def _raw_sides(rnd: dict[str, Any]) -> list[dict[str, Any]]:
    """原始字典中一场比赛的各方阵容（以 ``sides`` 为准，兼容旧 ``sideA``/``sideB``）。"""
    sides = rnd.get("sides")
    if not sides:
        sides = [rnd.get("sideA") or {}, rnd.get("sideB") or {}]
        rnd["sides"] = sides
    return sides


def _ensure_participants(data: dict[str, Any], ids: list[str]) -> list[str]:
    """把上场的替补**自动补进本届参与名单**，返回真正被加入的选手 ID。

    名单为空表示「未指定 = 全员参与」，此时无需补；补进去的选手从下一局起
    就算作本届参与者（会出现在排名榜与用户端名单里）。
    """
    current = list(data.get("participants") or [])
    if not current:
        return []
    added = [pid for pid in dict.fromkeys(ids) if pid and pid not in current]
    if added:
        data["participants"] = [*current, *added]
    return added


def _names_of(ids: list[str]) -> list[str]:
    """把选手 ID 换成姓名（用于接口回给前端的提示文案）。"""
    players = {p.id: p for p in store.snapshot().players}
    return [players[pid].display_name if pid in players else pid for pid in ids]


def _round_mutator(ref: str, fn):
    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        rounds = data.get("rounds", [])
        target = next((r for r in rounds if str(r.get("code") or "") == ref), None)
        if target is None:
            target = next((r for r in rounds if str(r.get("index")) == ref), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")
        fn(target)
        return data

    return _mutate


@app.post("/api/rounds/{ref}/status")
async def api_round_status(
    ref: str, payload: RoundStatusPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """切换比赛状态（pending / live / done）。"""
    status = payload.status
    if status not in ("pending", "live", "done"):
        raise HTTPException(status_code=400, detail="状态只能是 pending / live / done")

    def apply(rnd: dict[str, Any]) -> None:
        from .store import now_iso

        rnd["status"] = status
        if status == "live":
            rnd["startedAt"] = rnd.get("startedAt") or now_iso()
            rnd["finishedAt"] = ""
        elif status == "done":
            rnd["finishedAt"] = now_iso()
        else:
            rnd["startedAt"] = ""
            rnd["finishedAt"] = ""
            rnd["winner"] = ""
            rnd["sets"] = []
            for side in _raw_sides(rnd):
                side["score"] = 0
                side["points"] = 0
                side["rank"] = 0
                side["forfeit"] = False

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-status")
    return {"ok": True, "revision": cfg.revision}


_ROUND_TIME_FIELDS = (
    ("scheduled_at", "scheduledAt", "计划时间"),
    ("started_at", "startedAt", "开始时间"),
    ("finished_at", "finishedAt", "结束时间"),
)


@app.post("/api/rounds/{ref}/walkover")
async def api_round_walkover(
    ref: str, payload: RoundWalkoverPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """判某一方弃权（长期没人 / 人数不足）：该方垫底，其余各方自动晋级。

    * 2 方对阵：对手直接获胜并进入下一轮（淘汰赛会顺着对阵图继续推进）；
    * 3~4 方同场：只把弃权方移出名次竞争，其余队伍继续把这场打完；
    * 会往备注里写一条留痕（谁弃权、谁晋级），重置该场会一并清除。
    """
    cfg_now = store.snapshot()
    target = _find_round(cfg_now, ref)
    if target is None:
        raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")
    keys = [chr(ord("A") + i) for i in range(len(target.sides))]
    key = (payload.side or "").strip().upper()[:1]
    if key not in keys:
        raise HTTPException(status_code=400, detail=f"side 只能是 {' / '.join(keys)}")
    index = keys.index(key)
    reason = (payload.reason or "").strip() or "弃权"

    def apply(rnd: dict[str, Any]) -> None:
        from .store import now_iso

        raw = _raw_sides(rnd)
        others = [i for i in range(len(raw)) if i != index]
        if not others:
            raise HTTPException(status_code=400, detail="本场只有一方，无法判定弃权")
        raw[index]["forfeit"] = True
        raw[index]["score"] = 0
        raw[index]["points"] = 0
        label = raw[index].get("label") or f"{key} 方"
        stamp = f"[弃权] {label} {reason}"
        if len(others) == 1:
            # 对手直接获胜：进入下一轮 / 败者组由对阵图自动推导
            other = others[0]
            raw[index]["rank"] = 2
            raw[other]["rank"] = 1
            rnd["winner"] = chr(ord("A") + other)
            rnd["status"] = "done"
            rnd["startedAt"] = rnd.get("startedAt") or now_iso()
            rnd["finishedAt"] = now_iso()
            advance = raw[other].get("label") or "对方"
            stamp = f"{stamp} → {advance} 晋级"
        else:
            # 多方同场：其余队伍继续比赛，弃权方排到最后
            raw[index]["rank"] = len(raw)
            stamp = f"{stamp}（本场其余队伍继续）"
        existing = (rnd.get("note") or "").strip()
        if stamp not in existing:
            rnd["note"] = f"{stamp}｜{existing}" if existing else stamp

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-walkover")
    settled = _find_round(cfg, ref)
    log.warning(
        "对局 %s 判定弃权 | 方=%s | 原因=%s | winner=%s",
        ref,
        key,
        reason,
        settled.winner if settled else "",
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "side": key,
        "reason": reason,
        "winner": settled.winner if settled else "",
        "status": settled.status if settled else "",
        "state": build_public_state(cfg),
    }


@app.post("/api/rounds/{ref}/times")
async def api_round_times(
    ref: str, payload: RoundTimesPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """登记一场比赛的计划 / 开始 / 结束时间。

    * 只填开始时间 = 进行中，结束时间留空表示「待定」（这是最常见的用法）；
    * 登记结束时间不会自动结算——胜负仍由录入比分决定，
      否则会产生「已结束但没有胜者」的对局，卡住淘汰赛推进；
    * 清空开始与结束时间会把「进行中」退回「未开始」。
    """
    cfg_now = store.snapshot()
    target = _find_round(cfg_now, ref)
    if target is None:
        raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")

    patch: dict[str, str] = {}
    for attr, field, label in _ROUND_TIME_FIELDS:
        raw = getattr(payload, attr)
        if raw is None:
            continue
        try:
            patch[field] = logic.check_time(raw, label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not patch:
        raise HTTPException(status_code=400, detail="没有需要更新的时间")

    start = patch.get("startedAt", logic.normalize_time(target.started_at))
    end = patch.get("finishedAt", logic.normalize_time(target.finished_at))
    try:
        logic.check_order(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settled = target.winner in ("A", "B", "DRAW")
    has_start, has_end = logic.parse_time(start) is not None, logic.parse_time(end) is not None
    next_status = target.status
    warnings: list[str] = []
    if next_status == "pending" and has_start:
        next_status = "live"
    elif next_status == "live" and not has_start and not has_end:
        next_status = "pending"
    if has_end and not settled:
        warnings.append("已登记结束时间，但还没有比分：录入比分后才会结算晋级。")
    elif has_end and next_status != "done":
        next_status = "done"

    def apply(rnd: dict[str, Any]) -> None:
        for field, value in patch.items():
            rnd[field] = value
        rnd["status"] = next_status

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-times")
    log.info("对局 %s 时间已更新 | %s | status=%s", ref, patch, next_status)
    return {
        "ok": True,
        "revision": cfg.revision,
        "status": next_status,
        "times": patch,
        "warnings": warnings,
    }


@app.post("/api/rounds/{ref}/result")
async def api_round_result(
    ref: str, payload: RoundResultPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """录入比赛结果并结算。

    能填什么都行——各局小分、各方比分 / 得分、用时——服务端自动判定胜负与名次，
    胜者进入下一轮，败者按败者组开关决定进败者组还是直接淘汰。

    锦标赛制的总决赛一旦决出总冠军，本届会自动标记为「已结束」（只读），
    要在赛后改数据，先在管理页点一次「恢复进行」。
    """
    cfg_now = store.snapshot()
    target = _find_round(cfg_now, ref)
    if target is None:
        raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")
    # 锦标赛制看固定队伍是否就位；积分制的常规局只看是否已排出阵容
    if target.stage == "league":
        ready = all(site.player_ids for site in target.sides)
    else:
        ready = all(site.team_id for site in target.sides)
    if not ready:
        raise HTTPException(status_code=400, detail="本场对阵尚未确定（需等待上游比赛结果）")
    # 淘汰赛必须分出胜负（双败赛制不接受平局）；小组赛与积分制常规局可按规则允许平局
    allow_draw = cfg_now.rules.allow_draw and target.stage in ("group", "league")

    side_count = len(target.sides)
    valid_keys = [chr(ord("A") + i) for i in range(side_count)]
    explicit = (payload.winner or "").upper()
    if explicit and explicit not in (*valid_keys, "DRAW"):
        raise HTTPException(
            status_code=400, detail=f"winner 只能是 {' / '.join(valid_keys)} 或 DRAW"
        )
    for item in payload.sides:
        if item.score < 0 or item.points < 0:
            raise HTTPException(status_code=400, detail="比分与得分不能为负数")
    if (payload.score_a or 0) < 0 or (payload.score_b or 0) < 0:
        raise HTTPException(status_code=400, detail="比分不能为负数")
    if payload.duration_minutes is not None and payload.duration_minutes < 0:
        raise HTTPException(status_code=400, detail="用时不合法")

    # 时间：显式传入优先（空串 = 清空），未指定则沿用已有值 / 用当前时间补全
    try:
        given_start = (
            None if payload.started_at is None else logic.check_time(payload.started_at, "开始时间")
        )
        given_end = (
            None if payload.finished_at is None else logic.check_time(payload.finished_at, "结束时间")
        )
        logic.check_order(
            given_start if given_start is not None else target.started_at,
            given_end if given_end is not None else target.finished_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _write_entered(rnd: dict[str, Any]) -> None:
        """把前端填的内容写进原始字典（比分 / 得分 / 各局小分）。"""
        raw = _raw_sides(rnd)
        for order, item in enumerate(payload.sides):
            key = (item.key or chr(ord("A") + order)).upper()[:1]
            index = ord(key) - ord("A") if key else -1
            if not 0 <= index < len(raw):
                continue
            raw[index]["score"] = int(item.score)
            raw[index]["points"] = int(item.points)
            if item.team_id:
                raw[index]["teamId"] = item.team_id
            if item.player_ids:
                raw[index]["playerIds"] = list(item.player_ids)
        if payload.score_a is not None:
            raw[0]["score"] = int(payload.score_a)
        if payload.score_b is not None and len(raw) > 1:
            raw[1]["score"] = int(payload.score_b)
        rnd["sets"] = [item.dump() for item in payload.sets]

    def apply(rnd: dict[str, Any]) -> None:
        from .store import now_iso

        _write_entered(rnd)
        model = Round.model_validate(rnd)
        auto = tournament.judge_round(model, allow_draw=allow_draw)
        winner = explicit or auto
        if explicit and explicit != "DRAW" and explicit != auto:
            # 人工指定第 1 名：把指定方钉在 1，其余按得分顺序依次排 2、3、4
            keys = [chr(ord("A") + i) for i in range(len(model.sides))]
            winner_index = keys.index(explicit) if explicit in keys else -1
            if winner_index >= 0:
                others = sorted(
                    (i for i in range(len(model.sides)) if i != winner_index),
                    key=lambda i: (-model.sides[i].score, -model.sides[i].points),
                )
                model.sides[winner_index].rank = 1
                for offset, index in enumerate(others, start=2):
                    model.sides[index].rank = offset
            model.winner = winner
        if not winner:
            who = "并列第一" if side_count > 2 else "比分相同"
            hint = "请直接指定胜方" if not allow_draw else "请直接指定胜方或标记为平局"
            raise HTTPException(status_code=400, detail=f"{who}，无法判定晋级：{hint}")
        if winner == "DRAW" and not allow_draw:
            raise HTTPException(status_code=400, detail="当前规则不允许平局")

        raw = _raw_sides(rnd)
        for index, side in enumerate(model.sides):
            raw[index]["score"] = side.score
            raw[index]["points"] = side.points
            raw[index]["rank"] = side.rank
        rnd["winner"] = winner
        if payload.note:
            rnd["note"] = payload.note
        if payload.duration_minutes is not None:
            rnd["durationMinutes"] = int(payload.duration_minutes)
        rnd["status"] = "done"
        start_iso = given_start if given_start is not None else (rnd.get("startedAt") or now_iso())
        end_iso = given_end if given_end is not None else now_iso()
        # 开始时间填成未来时不要产出「负数用时」，直接用开始时间兜底
        pairs = (logic.parse_time(start_iso), logic.parse_time(end_iso))
        if pairs[0] is not None and pairs[1] is not None and pairs[1] < pairs[0]:
            end_iso = start_iso
        rnd["startedAt"] = start_iso
        rnd["finishedAt"] = end_iso

    # final：总冠军一决出就自动把这一届标记为「已结束」（见 logic.close_on_champion）
    cfg = await store.mutate(
        _round_mutator(ref, apply), actor="web:round-result", final=logic.close_on_champion
    )
    settled = _find_round(cfg, ref)
    champion = next((r for r in cfg.rounds if r.stage == "gf"), None)
    log.info(
        "对局 %s 结果已录入 | winner=%s | 各局=%d | 用时=%s",
        ref,
        settled.winner if settled else "",
        len(payload.sets),
        payload.duration_minutes,
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "winner": settled.winner if settled else "",
        "auto": not explicit,
        "ranking": [
            {"key": chr(ord("A") + i), "teamId": s.team_id, "label": s.label,
             "score": s.score, "points": s.points, "rank": s.rank}
            for i, s in enumerate(settled.sides)
        ] if settled else [],
        "finished": bool(champion and champion.status == "done" and champion.winner),
        # 这一次录入是否把这一届自动标记成了「已结束」（前端据此提示一句）
        "eventClosed": cfg.event.status == "closed" and cfg_now.event.status != "closed",
    }


@app.post("/api/rounds/{ref}/live")
async def api_round_live(
    ref: str, payload: RoundLivePayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """本场直播开关：是否为本场比赛推流，以及直播选手提示。"""

    def apply(rnd: dict[str, Any]) -> None:
        rnd["live"] = bool(payload.enabled)
        rnd["liveNote"] = (payload.note or "").strip()

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-live")
    log.info("对局 %s 直播开关 = %s | %s", ref, payload.enabled, payload.note)
    return {"ok": True, "revision": cfg.revision, "live": bool(payload.enabled)}


@app.post("/api/rounds/{ref}/reset")
async def api_round_reset(ref: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """重置一场比赛；依赖它的后续对局会自动作废（上游变了，下游重来）。"""

    def apply(rnd: dict[str, Any]) -> None:
        model = Round.model_validate(rnd)
        tournament.reset_round_result(model)
        for key in ("status", "winner", "sets", "durationMinutes", "startedAt", "finishedAt", "locked"):
            rnd[key] = model.dump()[key]
        raw = _raw_sides(rnd)
        for index, side in enumerate(model.sides):
            raw[index]["score"] = side.score
            raw[index]["points"] = side.points
            raw[index]["rank"] = side.rank
            raw[index]["forfeit"] = side.forfeit
        # 顺手去掉弃权留痕，避免重置后备注里还挂着「弃权 → 晋级」
        note = rnd.get("note") or ""
        if "[弃权]" in note:
            kept = [part for part in note.split("｜") if not part.strip().startswith("[弃权]")]
            rnd["note"] = "｜".join(part for part in kept if part.strip())

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-reset")
    return {"ok": True, "revision": cfg.revision}


# --------------------------------------------------------------------------- #
# 头像
# --------------------------------------------------------------------------- #
@app.post("/api/avatar/upload")
async def api_avatar_upload(
    payload: AvatarUploadPayload, _: str = Depends(require_admin)
) -> dict[str, Any]:
    """接收 data:URL 头像并落盘，返回同源可访问地址。"""
    try:
        url = avatars.save_data_url(payload.data_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "url": url}


@app.get("/api/avatar/file/{name}")
async def api_avatar_file(name: str) -> Response:
    """读取本地上传的头像（文件名白名单 + 内容哈希，天然防穿越）。"""
    path = avatars.resolve_local(name)
    if path is None:
        raise HTTPException(status_code=404, detail="头像不存在")
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=604800, immutable", "X-NTE-Avatar": "upload"},
    )


@app.get("/api/avatar/p/{player_id}")
async def api_avatar_player(
    player_id: str,
    size: int = Query(default=100),
    refresh: bool = Query(default=False),
) -> Response:
    """按**选手 ID** 取头像。

    用户端拿不到选手的 QQ，因此头像统一走这里；
    这样客户端请求里不会出现 QQ 号，不会暴露选手与 QQ 的关联。

    ``refresh=1`` 会跳过内存与磁盘缓存强制回源（换过 QQ 头像后用），
    响应头 ``X-NTE-Avatar`` 说明这次数据的来源：
    ``memory`` / ``disk`` / ``fetched`` / ``stale`` / ``placeholder``。
    """
    player = next((p for p in store.snapshot().players if p.id == player_id), None)
    if player is None:
        raise HTTPException(status_code=404, detail="选手不存在")
    if not player.qq:
        raise HTTPException(status_code=404, detail="该选手未配置 QQ 头像")
    if not avatars.is_valid_qq(player.qq):
        raise HTTPException(status_code=400, detail="选手的 QQ 号格式不正确")
    body, mime, source = await avatars.get_avatar(player.qq, size, player.name, refresh=refresh)
    return Response(
        content=body,
        media_type=mime,
        headers={
            # 刷新请求不能被浏览器缓存，否则点完还是旧图
            "Cache-Control": (
                "no-store" if refresh else "public, max-age=3600, stale-while-revalidate=86400"
            ),
            "X-NTE-Avatar": source,
        },
    )


@app.get("/api/avatar/c/{channel_id}")
async def api_avatar_channel(
    channel_id: str,
    size: int = Query(default=100),
    refresh: bool = Query(default=False),
) -> Response:
    """按**频道 ID** 取成员频道头像（与选手同一套代理，客户端看不到 QQ 号）。"""
    channel = next((c for c in store.channels() if c.id == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail="频道不存在")
    if not channel.qq:
        raise HTTPException(status_code=404, detail="该频道未配置 QQ 头像")
    if not avatars.is_valid_qq(channel.qq):
        raise HTTPException(status_code=400, detail="频道的 QQ 号格式不正确")
    body, mime, source = await avatars.get_avatar(channel.qq, size, channel.name, refresh=refresh)
    return Response(
        content=body,
        media_type=mime,
        headers={
            "Cache-Control": (
                "no-store" if refresh else "public, max-age=3600, stale-while-revalidate=86400"
            ),
            "X-NTE-Avatar": source,
        },
    )


@app.get("/api/avatar/{qq}")
async def api_avatar(
    qq: str,
    size: int = Query(default=100),
    name: str = Query(default=""),
    refresh: bool = Query(default=False),
) -> Response:
    if not avatars.is_valid_qq(qq):
        raise HTTPException(status_code=400, detail="QQ 号格式不正确")
    body, mime, source = await avatars.get_avatar(qq, size, name, refresh=refresh)
    return Response(
        content=body,
        media_type=mime,
        headers={
            "Cache-Control": (
                "no-store" if refresh else "public, max-age=3600, stale-while-revalidate=86400"
            ),
            "X-NTE-Avatar": source,
        },
    )


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    if not await hub.connect(websocket):
        return
    try:
        # hub.connect 已回放最近一次状态；仅当尚无缓存时才现算，避免连接时重复下发
        if not hub.has_state:
            await hub.send_state(websocket, build_public_state(store.snapshot()))
        while True:
            raw = await websocket.receive_text()
            if raw == "ping":
                await websocket.send_text('{"type":"pong"}')
            elif raw == "state":
                await hub.send_state(websocket, build_state(store.snapshot()))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("WebSocket 会话异常终止", exc_info=True)
    finally:
        await hub.disconnect(websocket)


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> Response:
    return render_index()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    """非 API 路径统一回落到首页，方便移动端直接收藏子路径。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    if not (STATIC_DIR / "index.html").exists():
        raise HTTPException(status_code=404, detail="页面不存在")
    return render_index()


__all__ = ["app"]
