"""FastAPI 应用入口：REST API + WebSocket + 静态资源。

分层：``main`` 只做「路由 + 参数校验 + 调用 store/logic」，
业务规则集中在 ``logic``，持久化集中在 ``store``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager, suppress
from html import escape
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

from . import (
    __version__,
    avatars,
    backup,
    backup_api,
    bot_api,
    card,
    credits,
    helpcard,
    hot,
    hot_api,
    league,
    legacy_api,
    live,
    logic,
    login_guard,
    media,
    metrics,
    notices_api,
    qqbot_api,
    remind,
    subs,
    tournament,
)
from . import members as members_api
from .auth import Session, auth
from .logging_conf import get_logger, setup_logging
from .logic import build_state, joined_players, validate_config
from .models import (
    MAX_SIDES,
    Channel,
    Config,
    NTEModel,
    Player,
    Round,
    SetScore,
    SubScope,
    Substitution,
    Team,
)
from .security import (
    optional_session,
    require_admin,
    require_current_event,
    require_event,
    require_event_owned,
    require_server,
)
from .store import PROJECT_ROOT, now_iso, store
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
#       index.html **绝不缓存**，保证版本号变更后能立刻生效。
# 中间件用纯 ASGI 实现，不对响应体做缓冲，避免影响直播流式代理。
# --------------------------------------------------------------------------- #
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
_NO_STORE = "no-store"
#: 未带版本号的静态路径：允许 304，但不许直接用缓存的旧副本
_REVALIDATE_CACHE = "no-cache"
_CACHE_BY_MODE = {
    "immutable": _IMMUTABLE_CACHE,
    "revalidate": _REVALIDATE_CACHE,
    "no-store": _NO_STORE,
}
_VERSIONED_STATIC_RE = re.compile(r"^/static/v/[0-9a-f]{6,}/(?P<rest>.+)$")

#: 首页（HTML）的缓存头，**必须**让边缘与浏览器每次都回源问一次。
#:
#: 为什么不能只写 ``no-cache``：版本号是**注入进 HTML** 的，而带版号的 CSS / JS 是
#: ``immutable``（一年）。一旦这份 HTML 被某个中间层（CDN / 反代 / 浏览器启发式缓存）
#: 留住了，它会连带把**一整年的旧样式**钉死——改完 CSS 刷新还是旧样子，且怎么强刷
#: 都没用（因为强刷只绕过本地缓存，绕不过 CDN）。
#: 所以这里把 CDN 认得更死的几条一起写上：``no-store`` + ``must-revalidate`` +
#: ``max-age=0`` + ``Pragma`` / ``Expires``（老中间层的兼容字段）。
_HTML_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


#: 资源版本号的缓存时长：目录里文件一多，**每个**首页请求都 stat 一遍就太浪费了；
#: 一秒足够让「改完刷新页面就生效」，也远小于任何人手速（见 :func:`asset_version`）。
ASSET_TTL = 1.0
#: 最近一次算出来的 ``(算的时刻, 版本号)``
_asset_cache: tuple[float, str] | None = None


def _compute_asset_version_uncached() -> str:
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


def asset_version(ttl: float = ASSET_TTL) -> str:
    """静态资源版本号（**带 TTL 的缓存**，见 :data:`ASSET_TTL`）。

    版本变了 → 首页里注入的 ``/static/v/<版本>/…`` 跟着变 → 浏览器自然去取新包，
    **进程不重启也不会再继续发旧的 JS / CSS**（热更新就靠它把前端一起换掉）。
    把「reload 才重算」改成「一直重算」是为了开发时不再踩「代码改了页面还跑旧包」。
    """
    global _asset_cache
    now = time.monotonic()
    if _asset_cache is not None and now - _asset_cache[0] < ttl:
        return _asset_cache[1]
    version = _compute_asset_version_uncached()
    _asset_cache = (now, version)
    return version


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
        elif path.startswith("/static/"):
            # 未带版本号的静态路径（手输 / 旧收藏）：绝不长缓存，但允许 304 —— 否则
            # 浏览器会按启发式规则自己缓存一份，改了文件也可能继续跑旧代码。
            mode = "revalidate"
        elif path.startswith(("/api/", "/ws")):
            mode = "no-store"

        if mode is None:
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # 已有显式策略的路由（如头像、公告图片）保持不动
                if "cache-control" not in headers:
                    headers["Cache-Control"] = _CACHE_BY_MODE.get(mode, _NO_STORE)
            await send(message)

        await self.app(scope, receive, send_with_cache)


#: 安全响应头（保守一组）：只加不会与站内自定义 HTML / 头像 / 直播流打架的那几条。
#: ``Strict-Transport-Security`` 刻意不在其中——它只在 HTTPS 下有意义，
#: 而且一旦浏览器记住就会把同域的 http 请求也强制升级，坑本地调试与内网部署。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN",
    # 本站**不使用**定位 / 麦克风 / 摄像头 / 支付（推流走 OBS 的 WHIP，不是浏览器采集），
    # 所以直接声明「用不到」：万一以后被注入脚本，它连权限提示都弹不出来。
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


class SecurityHeadersMiddleware:
    """一组**保守**的安全响应头（只在没有显式设置时补上）。

    刻意只加「不会与站内自定义 HTML / 头像 / 直播流打架」的那几条：

    * ``X-Content-Type-Options: nosniff``：上传物与静态文件不按内容被猜成脚本；
    * ``Referrer-Policy: no-referrer``：站内链接不带会话令牌了（下载走请求头，
      见 ``core.downloadFile``），但同源页面里仍可能有别的敏感 URL，一律不外带；
    * ``Content-Security-Policy: frame-ancestors 'self'``：防点击劫持。
      **只写这一条指令**——全量 CSP 会跟「自定义 HTML、QQ 头像、HLS/m3u8」
      互相打架，而 frame-ancestors 只管「谁能用 iframe 嵌我们」。
      另外**不要**顺手加 ``frame-src 'self'``：那只管我们嵌谁，而 B站 直播正是靠
      ``<iframe>`` 直嵌官方播放器（见 live.bili_embed_url），加了它直播页会白屏；
    * ``X-Frame-Options: SAMEORIGIN``：给不认 CSP 的老浏览器兜底。

    不加 ``Strict-Transport-Security``：它只在 HTTPS 下有意义，而且一旦浏览器
    记住就会把同域的 http 请求也强制升级——本地调试与内网部署会被它坑。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    if name.lower() not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AuditMiddleware:
    """把「写请求」记一条到操作日志：谁、什么时候、动了哪个接口。

    三条刻意的取舍：

    * **只记方法 + 路径 + 状态码，不记请求体**——请求体里会出现成员密钥、Bearer
      令牌、管理 KEY 这类值，抄进库就等于多存了一份敏感信息。
    * **跳过登录与媒体服务器回调**：前者带密钥，后者是 MediaMTX 每个推流会话都会
      打过来的（``/api/live/auth``），记下来只会把日志冲干净。
    * **只读接口一般不记**（量大又没有追查价值），但**机器人接口的鉴权失败要记**：
      那是「还有实例在用旧令牌」的唯一线索。
    """

    _SKIP = ("/api/auth", "/api/live/auth")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = str(scope.get("method", "GET")).upper()
        is_write = method not in ("GET", "HEAD", "OPTIONS")
        is_bot = path.startswith("/api/bot/")
        if not path.startswith("/api/") or (not is_write and not is_bot):
            await self.app(scope, receive, send)
            return
        if path.startswith(self._SKIP):
            await self.app(scope, receive, send)
            return

        captured = {"status": 0}

        async def remember(message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, remember)
        finally:
            # 响应已发完才写库：日志再慢也拖不住请求
            status = captured["status"]
            if is_write or status in (401, 403):
                await self._record(scope, method, path, status)

    @staticmethod
    async def _record(scope, method: str, path: str, status: int) -> None:
        try:
            token = ""
            for raw_key, raw_value in scope.get("headers") or []:
                if raw_key.decode("latin-1").lower() == SESSION_HEADER.lower():
                    token = raw_value.decode("latin-1")
                    break
            # resolve 而不是 get：审计日志里的「谁做的」也要认得换代之前签发的会话
            session = await auth.resolve(token) if token else None
            await store.log_activity(
                actor=(session.name or session.label) if session else "未登录",
                actor_uid=session.uid if session else "",
                method=method,
                path=path,
                status=status,
            )
        except Exception:  # pragma: no cover - 观测失败绝不能影响刚发出去的响应
            log.warning("操作日志记录失败 | %s %s", method, path, exc_info=True)


_index_cache: tuple[str, str] | None = None

# 独立页的标题 / 描述（分享到群里时预览卡片用的就是它们）
_STANDALONE_META: dict[str, tuple[str, str]] = {
    "channels": ("频道", "成员直播间与日常播台，点开直接看。"),
    "events": ("全部赛事", "新建 / 重命名 / 封存 / 删除届次。"),
    "admin": ("服务器管理", "成员、届次、备份、QQ 机器人与直播封禁。"),
    "user": ("我的", "个人资料、直播间名字与凭据轮换。"),
    "developer": ("开发者", "关于作者、联系方式与开源许可。"),
}
_TITLE_RE = re.compile(r"<title>.*?</title>", re.DOTALL)
_DESC_RE = re.compile(r'<meta name="description" content=".*?">', re.DOTALL)
_EVENT_STATUS_CN = {"draft": "筹备中", "active": "进行中", "closed": "已结束"}


def _abs_url(base: str, path: str) -> str:
    """站内相对路径 → 绝对地址。分享抓取（QQ / 微信）只认绝对 URL。"""
    clean = (path or "").strip()
    if clean.startswith(("http://", "https://")):
        return clean
    if not clean.startswith("/"):
        clean = "/" + clean.lstrip("/")
    return f"{(base or '').rstrip('/')}{clean}"


def _og_image() -> str:
    """分享图：界面配置里填了就用它，否则用内置那张 ``/og.png``。

    配置读不到（极端情况）也要能出图——分享卡片宁可样式旧一点，也不能没有图。
    """
    try:
        custom = str(store.snapshot().ui.og_image or "").strip()
    except Exception:  # pragma: no cover - 读配置失败不该把整页拖挂
        log.warning("读取分享图配置失败，回落到内置图", exc_info=True)
        custom = ""
    return custom or "/og.png"


async def page_meta(path: str, base_url: str = "") -> dict[str, str]:
    """这一屏的标题 / 描述 / 分享图（**服务端**算好）。

    前端本来也会改 ``document.title``，但分享到 QQ / 微信时抓取的是**原始 HTML**，
    所以标题与 og 标签必须在服务端就写对——否则任何链接的预览卡片都是首页那句。
    """
    site = store.site_name()
    seg = (path or "").strip("/").split("/")[0]
    title, desc = site, f"{site}：全部赛事与直播间的总入口。"
    if seg in _STANDALONE_META:
        # 独立页要**先判**：它们的段名（events / admin / user / channels / developer）
        # 同样长得像届次 ID，先查届次的话下面这张表永远轮不到——
        # 于是分享卡片一直用首页那句（这个顺序 bug 修过一次，别再换回来）。
        # 与前端路由一致：`parseRoute` 也是先认 PAGES 再当届次。
        label, text = _STANDALONE_META[seg]
        title, desc = f"{label} | {site}", text
    elif seg and re.fullmatch(r"[A-Za-z0-9_-]{2,40}", seg):
        try:
            for item in await store.list_events():
                if item["id"] == seg and not item.get("hidden"):
                    name = item.get("name") or seg
                    bits = [
                        _EVENT_STATUS_CN.get(str(item.get("status")), ""),
                        f"{item.get('players') or 0} 人",
                    ]
                    if item.get("champion"):
                        bits.append(f"榜首 {item['champion']}")
                    title = f"{name} | {site}"
                    desc = str(item.get("brief") or "").strip() or " · ".join(b for b in bits if b)
                    break
        except Exception:
            # 取不到就退回默认文案：首页是必经之路，绝不能让 meta 读失败把整页拖挂
            log.warning("赛事 meta 读取失败 | id=%s", seg, exc_info=True)
    return {
        "title": title,
        "desc": desc,
        "site": site,
        "image": _abs_url(base_url, _og_image()),
        "url": _abs_url(base_url, path or "/"),
    }


def _inject_meta(html: str, meta: dict[str, str]) -> str:
    """把标题 / 描述 / og 标签塞进（已缓存的）基础 HTML。"""
    title = escape(meta["title"])
    desc = escape(meta["desc"])
    site = escape(meta.get("site") or meta["title"])
    image = escape(meta.get("image") or "")
    link = escape(meta.get("url") or "")
    og = (
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{desc}">'
        f'<meta property="og:type" content="website">'
        f'<meta property="og:site_name" content="{site}">'
        f'<meta property="og:locale" content="zh_CN">'
    )
    if link:
        og += f'<meta property="og:url" content="{link}">'
    if image:
        og += f'<meta property="og:image" content="{image}">'
        if image.endswith("/og.png"):
            # 只有内置那张能保证尺寸；自定义图不替他声明宽高（免得卡片被裁歪）
            og += (
                '<meta property="og:image:width" content="1200">'
                '<meta property="og:image:height" content="630">'
            )
    # summary_large_image 才能把图铺成横向大卡（默认 summary 只有小方图）
    og += '<meta name="twitter:card" content="summary_large_image">'
    out = _TITLE_RE.sub(f"<title>{title}</title>", html, count=1)
    out = _DESC_RE.sub(f'<meta name="description" content="{desc}">', out, count=1)
    return out.replace("</head>", f"{og}</head>", 1)


def render_index(meta: dict[str, str]) -> HTMLResponse:
    """输出首页：注入了资源版本号 + 这一屏的标题 / og 标签。

    基础 HTML 按资源版本号做进程内缓存；标题与 og 是逐请求拼进去的（字符串操作，可忽略）。
    """
    global _index_cache
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>静态页面缺失</h1>", status_code=500)
    version = asset_version()
    if _index_cache is None or _index_cache[0] != version:
        html = page.read_text(encoding="utf-8").replace("/static/", f"/static/v/{version}/")
        _index_cache = (version, html)
        log.info("已注入静态资源版本 | version=%s", version)
    # 这份 HTML 绝不能进任何缓存（理由见 _HTML_CACHE_HEADERS）
    return HTMLResponse(_inject_meta(_index_cache[1], meta), headers=dict(_HTML_CACHE_HEADERS))


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

    * ``sets``：各轮成绩（最自然，自动推出大比分与总成绩），仅 2 队有意义；
    * ``sides``：各方成绩（多队同场用这个）；
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
    """届次元信息：重命名 / 改状态 / 隐藏。``hidden=None`` 表示不改动可见性。"""

    name: str = ""
    status: str = ""
    hidden: bool | None = None


class ParticipantsPayload(NTEModel):
    """本届参与名单。

    ``player_ids`` 传空数组表示「本届一个人都不参与」——一旦保存过，名单就是显式的，
    不会回落成「未指定 = 全员参与」（那正是「全不选后保存又变回全选」的根源）。
    """

    player_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 鉴权依赖（详见 app/security.py）
#
# ``require_admin``  任何已登录用户（成员 / 赛事管理员 / 服务器管理员）；
# ``require_event``  赛事管理员或服务器管理员；
# ``require_server`` 仅服务器管理员。
# 会话自带身份（uid / permission），接口据此做归属与权限判定。
# --------------------------------------------------------------------------- #


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
    # 站点名称（全局，服务器管理员设定）：顶栏 / 浏览器标签 / 主页都用它
    state["siteName"] = store.site_name()
    # 最新一条通知（**只有 id / 标题 / 时间**，正文按需再取）：
    # 前端据此判断「有没有没看过的新通知」并弹窗；正文不进广播，免得每次改比分都重发。
    state["notices"] = store.notice_heads()
    state["channels"] = logic.channel_views(cfg, store.channels())
    # 频道板块的公告（全局，纯展示）：放异环相关的说明 / 活动文案
    state["channelNotice"] = store.channel_notice()
    # 成员直播间（全局）：只要成员配了推流 ID 就出现在频道里，
    # 是否「直播中」以媒体服务器上报为准（前端据此点亮标记）。
    ready = live.ready_paths_snapshot() or set()
    # 只下发**启用中**的成员直播间（停用成员的会话与推流都已失效，不该出现在频道里）；
    # 停用成员仍可在 /admin 的成员管理里看到并重新启用（走 /api/members）。
    state["members"] = logic.member_views(
        cfg,
        [m for m in store.members() if m.active],
        bans=store.live_bans(),
        live_keys=ready,
        event_id=store.current_id,
    )
    # 生效中的直播封禁（公开）：直播间 / 成员卡据此显示封禁时间与理由
    state["liveBans"] = [logic.ban_view(b) for b in store.live_bans() if logic.ban_active(b)]
    # 服务器管理员注入的自定义 HTML（用于接入统计 / 数据采集）
    state["customHtml"] = store.custom_html()
    return state


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    # 登录会话落库并起一个「写回」巡检：热更新会换掉进程，会话必须活过换代，
    # 否则每更新一次就把所有人踢下线（见 app/auth.py 与 app/hot.py 的说明）。
    await auth.attach(store.path)

    async def on_config_change(cfg: Config, source: str) -> None:
        await hub.broadcast_state(build_public_state(cfg))
        for issue in validate_config(cfg):
            log.warning("配置提示 | source=%s | %s", source, issue)

    store.on_change(on_config_change)
    # 启动时先算一次，保证新连接的客户端立刻拿到数据
    await hub.broadcast_state(build_public_state(store.snapshot()))
    # 周期性自动备份：常驻巡检，到点才真的打包（没开启时只是每 5 分钟看一眼设置）
    backup_task = asyncio.create_task(backup.auto_backup_loop())
    # 赛前提醒：开赛前一天 / 前两小时在群里 @ 举办者（见 app/remind.py）。
    # 只在「聊天机器人推送开着」且举办者登记了 QQ 时才真的发得出去。
    remind_task = asyncio.create_task(remind.loop())
    # 直播常驻探测：**没人访问也一直在探**（间隔见 live.WATCH_INTERVAL），
    # 状态变了就通过 WebSocket 推给在线客户端；请求路径始终只读缓存、一秒都不等它。
    # 关停时由 live.stop_refresher() 统一收掉（见下面的 finally）。
    live.start_watcher()
    # QQ 机器人帮助图：启动时重画一份（产物不入库，见 app/helpcard.py）。
    # 放线程里做（画图 + 存盘约半秒），不拖慢启动；没装 Pillow 就只记一行日志。
    help_task = asyncio.create_task(asyncio.to_thread(helpcard.refresh))
    # 图片仓库收拢：把「旧布局」里单独存放的本地上传头像并进统一仓库，
    # 顺手删掉历史重复（同一张图以前会在头像 / 公告两个目录各存一份）。
    # **等它跑完**再往下走（放线程里做，不阻塞事件循环）：搬移过程中文件会有一瞬
    # 「两边都不在」，这时候若正好有备份在打包，就会漏掉那些图。收拢完就再也不会
    # 有可收的东西了（旧目录空了），所以这点等待只有升级后的第一次启动才有。
    try:
        await asyncio.to_thread(media.merge_legacy)
    except Exception:  # 图片读得到就行，收拢失败不该拦着服务起来
        log.warning("图片仓库收拢失败（不影响读取，下次启动再试）", exc_info=True)
    # 守护没了就跟着收摊：被硬杀（kill -9 / Windows terminate）的父进程没机会做清理，
    # 子进程不能变成「占着端口的孤儿」——那会让下一次启动绑不上（见 hot.watch_parent）。
    parent_task = asyncio.create_task(hot.watch_parent())
    cfg = store.snapshot()
    log.info("=" * 68)
    log.info("NTE 比赛平台已启动 | 当前届: %s (%s)", cfg.event.name, store.current_id)
    log.info("数据库: %s", store.path)
    log.info("本机访问: http://127.0.0.1:%s", os.getenv("NTE_PORT", "8000"))
    log.info("登录方式：成员密钥（服务器管理员忘记密钥可执行 `uv run python -m app --reset-key`）")
    log.info("=" * 68)
    # 一切就绪（数据库、后台任务、广播中心）——告诉热更新守护「可以停掉旧进程了」。
    # 这一行的位置就是「零中断」的关键：**调它之前**父进程绝不会动旧进程。
    if hot.supervised():
        # 应用侧的事实写一份给管理端（父进程那份自检看不到进程内部的事，见 app/hot.py）
        report = {
            "pid": os.getpid(),
            "loop": type(asyncio.get_running_loop()).__name__,
            "python": sys.version.split()[0],
            "sessions": auth.persisted,
            "online": auth.online,
            "cards": card.available(),
            "database": str(store.path),
            "at": time.time(),
        }
        hot.write_app_report(report)
        log.info(
            "热更新模式 | 事件循环=%s | 会话落库=%s | 卡片渲染=%s | Python=%s",
            report["loop"],
            "开" if report["sessions"] else "关",
            "开" if report["cards"] else "关（没装 Pillow，推送走纯文本）",
            report["python"],
        )
    if hot.notify_ready():
        log.info("已通知热更新守护：本进程可以开始服务（旧进程将被优雅停掉）")
    try:
        yield
    finally:
        backup_task.cancel()
        with suppress(asyncio.CancelledError):
            await backup_task
        remind_task.cancel()
        with suppress(asyncio.CancelledError):
            await remind_task
        # 帮助图那次渲染跑在线程里（线程没法取消）：等它收尾，别留下「任务未结束」的噪音
        with suppress(asyncio.CancelledError):
            await help_task
        parent_task.cancel()
        with suppress(asyncio.CancelledError):
            await parent_task
        # 会话落库的最后一笔：**在做完这件事之后**才停 store，免得写不进去
        await auth.detach()
        await store.stop()
        await avatars.aclose()
        # 先收掉还没跑完的探测任务，再关连接池：否则它可能在关池的瞬间发起请求
        await live.stop_refresher()
        await live.aclose()
        log.info("服务已停止")


# 交互式接口文档（/api/docs、/redoc、/api/openapi.json）：**默认关闭**。
# 它们会把全部接口、参数与数据模型摊开给任何访客看——自用站点并不需要，
# 而「知道有哪些接口」正是扫描器第一步。要对接 / 调试时设 NTE_DOCS=1 打开。
_DOCS_ON = os.getenv("NTE_DOCS", "").strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(
    title="NTE 比赛",
    description="NTE 比赛（异环）通用赛事平台：自动分组 / 积分结算 / 实时排行 / 直播推流",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/api/openapi.json" if _DOCS_ON else None,
)
if not _DOCS_ON:
    log.info("交互式接口文档已关闭（需要时设 NTE_DOCS=1 打开）")

# 跨域默认**关闭**：前端由本站同源提供，正常不需要任何 CORS。
# 确有跨域需求（前端单独部署在别的域名）时，用 NTE_CORS_ORIGINS 显式列出允许的来源；
# 不要用 ``*``——那等于允许任意站点携带凭据调用本 API。
_origins = [o.strip() for o in os.getenv("NTE_CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location"],
    )
    log.info("已启用跨域访问 | 允许来源=%s", ", ".join(_origins))
else:
    log.info("未配置 NTE_CORS_ORIGINS：仅允许同源访问（前端与 API 同域时无需配置）")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(EdgeCacheMiddleware)
# 安全响应头（保守一组，见 SecurityHeadersMiddleware 的取舍说明）
app.add_middleware(SecurityHeadersMiddleware)
# 操作日志（只记非 GET 的 /api 请求；见 AuditMiddleware 的取舍说明）
app.add_middleware(AuditMiddleware)

app.include_router(live.router)
app.include_router(members_api.router)
app.include_router(backup_api.router)
app.include_router(legacy_api.router)
app.include_router(qqbot_api.router)
app.include_router(bot_api.router)
app.include_router(notices_api.router)
app.include_router(hot_api.router)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 400:
        log.debug("HTTP %s | %s %s | %s", exc.status_code, request.method, request.url.path, exc.detail)
    # 保留 HTTPException 自带的响应头（如限流的 Retry-After），否则前端拿不到
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail, "status": exc.status_code},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    log.info("参数校验失败 | %s %s | %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "请求参数不合法", "detail": json.loads(json.dumps(exc.errors(), default=str))},
    )


def _error_message(exc: ValueError) -> str:
    """把校验异常整理成**一句话**。

    pydantic 的 ``ValidationError``（内部构造模型时抛出，如保存直播配置）是
    ``ValueError`` 的子类，``str()`` 出来是「1 validation error for Config\\
    stream.pushToken\\n  Value error, 推流令牌只能…」这种多行调试文本。
    这里只取第一条错误的正文，前端 toast 才读得懂。
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = (errors() or [{}])[0]
            msg = str(first.get("msg") or "").removeprefix("Value error, ").strip()
            if msg:
                return msg
        except Exception:  # pragma: no cover - 取不出来就退回整段文本
            log.debug("整理校验错误失败（忽略）", exc_info=True)
    return str(exc)


@app.exception_handler(ValueError)
async def business_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """业务校验失败（人数不足、时间不合法、流名含非法字符等）统一回 400 JSON。

    兜底用：漏掉 try/except 的校验不会再变成没有响应体的 500，
    前端始终能拿到 ``{error}`` 文案。
    """
    message = _error_message(exc)
    log.info("业务校验失败 | %s %s | %s", request.method, request.url.path, message)
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": message, "status": 400},
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
    # 以下几项**只读缓存**（探测由常驻任务一直刷，见 live.watch_loop）：
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
async def api_health(session: Session | None = Depends(optional_session)) -> dict[str, Any]:
    """存活探针（容器 HEALTHCHECK 要打，所以**必须公开**）。

    配置自检（``validate_config``）会说到具体选手名（「参与名单中的 X 未启用」），
    那是名单信息，只在管理端会话下才回——公开探针只给数量与计数。
    """
    cfg = store.snapshot()
    admin = bool(session and session.can_manage_events)
    return {
        "ok": True,
        "revision": cfg.revision,
        "players": len(cfg.players),
        "participants": len(joined_players(cfg)),
        "participantsSet": logic.has_custom_roster(cfg),
        "rounds": len(cfg.rounds),
        "ws": hub.stats(),
        "avatarCache": avatars.cache_stats(),
        "issues": validate_config(cfg) if admin else [],
        # 前端据此判断「我这一页跑的是不是最新的一版」：热更新换代后 WebSocket 会重连，
        # 前端在重连时比一次这个值，不一致就提示「点此刷新」（见 static/js/app.js）。
        # 它就是静态资源地址里那个版本号，本来就随每个页面发给所有访客，不算泄密。
        "assets": asset_version(),
        "app": __version__,
        "hot": hot.supervised(),
    }


@app.get("/api/diagnostics")
async def api_diagnostics(_: Session = Depends(require_event)) -> dict[str, Any]:
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
        # 公告图片占用（与头像缓存并列：这两个是唯一会自己长大的数据目录）
        "uploads": media.stats(),
        "live": live.stream_endpoints(),
        "issues": validate_config(cfg),
        # 还有几位成员的凭据是历史无盐格式（建议轮换）
        "legacyCredentials": len(store.legacy_credential_members()),
    }


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def _server_session(label: str) -> Session:
    """签发服务器管理员会话，并**绑定到那位唯一的服务器管理员成员**。

    服务器管理员只是「权限最高的成员」，本身也是一条成员记录（启动自检
    ``store.ensure_server_admin`` 保证它存在），所以会话直接带上他的 ``uid``：
    这样他也能用自己的 ``/user`` 页改资料 / 轮换密钥，``/api/me`` 也会回他自己的
    成员视图。万一记录缺失（正常不会）才退回无 uid 的 key 级会话。
    """
    admin = store.server_admin()
    if admin is None:
        return auth.issue(label=label, uid="", name="服务器管理员", permission="server_admin")
    return auth.issue(
        label=label, uid=admin.uid, name=admin.display_name, permission="server_admin"
    )


@app.post("/api/auth")
async def api_auth(payload: AuthPayload, request: Request) -> dict[str, Any]:
    """登录：**只认成员密钥**（随机生成、加盐哈希存储）。

    服务器管理员也只是「权限最高的成员」，用他自己的成员密钥登录即可；
    本站没有独立于成员之外的「主管理 KEY」——管理权限完全由登录后的身份决定。

    ``/api/auth`` 是**唯一**的暴力破解入口，因此这里挂了登录失败限制
    （类 fail2ban，见 ``login_guard`` 模块）：同一 IP 在时间窗内失败过多会被
    临时封禁；真实 IP 由反向代理配置决定（未配置可信代理时不信任转发头）。
    """
    settings = store.guard_settings()
    ip = login_guard.client_ip(request, settings)
    # 本机直连不设防：既不限流、也不计入失败（否则在本机手滑几次就把自己封了）
    local = login_guard.is_local(request, settings)
    if not local:
        wait = login_guard.blocked_seconds(ip, settings)
        if wait:
            log.warning("登录被限流拒绝 | ip=%s | 剩余=%ds", ip, wait)
            raise HTTPException(
                status_code=429,
                detail=f"登录失败次数过多，请 {wait} 秒后再试",
                headers={"Retry-After": str(wait)},
            )

    key = (payload.key or "").strip()
    # 1) 成员密钥：成员 / 赛事管理员 / 服务器管理员都用它登录
    member = store.member_by_key(key)
    if member is not None:
        if not member.active:
            if not local:
                login_guard.record_failure(ip, settings)
            raise HTTPException(status_code=403, detail="该成员已被停用，无法登录")
        login_guard.record_success(ip)
        session = auth.issue(
            label=f"member:{member.uid}",
            uid=member.uid,
            name=member.display_name,
            permission=member.permission,
        )
        log.warning("成员登录 | uid=%s | 权限=%s", member.uid, member.permission)
        return {
            "ok": True,
            "token": session.token,
            "expiresAt": int(session.expires_at),
            "uid": member.uid,
            "name": member.display_name,
            "permission": member.permission,
        }
    # 2) 没命中任何成员 → 登录失败。
    #    这里刻意**不再有**「主管理 KEY」这条路径：那套凭据已退休（见 README「登录与权限」）。
    banned = 0 if local else login_guard.record_failure(ip, settings)
    log.warning("登录失败：密钥不正确 | ip=%s%s", ip, f"（已封禁 {banned}s）" if banned else "")
    # 只回一句「密钥不正确」。**不在这里帮忙**：写明凭据类型等于告诉扫描器该猜什么，
    # 把 `--reset-key` 这类运维命令写进响应更是把服务端的手段摊给陌生人。
    # 找回方式属于登录页的文案（那里本来就有），不属于接口返回。
    raise HTTPException(status_code=401, detail="密钥不正确")


@app.post("/api/auth/local")
async def api_auth_local(request: Request) -> dict[str, Any]:
    """本机直连免登录：回环地址访问时直接签发服务器管理员会话。

    判定见 ``login_guard.is_local``（回环地址 + 无转发头 + Host 为本机名），
    且可在 `/admin → 登录限制` 里用「本机不设防」开关整体关闭。
    """
    settings = store.guard_settings()
    if not login_guard.is_local(request, settings):
        raise HTTPException(status_code=403, detail="仅本机（localhost）直连可用")
    session = _server_session("local")
    log.warning("本机直连免登录 | 已签发服务器管理员会话 | 成员=%s", session.uid or "(缺)")
    return {
        "ok": True,
        "local": True,
        "token": session.token,
        "expiresAt": int(session.expires_at),
        "uid": session.uid,
        "name": session.name,
        "permission": session.permission,
    }


@app.post("/api/auth/logout")
async def api_logout(x_nte_token: str | None = Header(default=None)) -> dict[str, Any]:
    auth.revoke(x_nte_token)
    return {"ok": True}


@app.get("/api/auth/check")
async def api_auth_check(session: Session = Depends(require_admin)) -> dict[str, Any]:
    """校验会话并回传身份（前端刷新后据此恢复权限显示）。"""
    member = store.member(session.uid) if session.uid else None
    return {
        "ok": True,
        "uid": session.uid,
        "name": session.name or (member.display_name if member else "服务器管理员"),
        "permission": session.permission,
        "isServer": session.is_server,
        "canManageEvents": session.can_manage_events,
    }


@app.post("/api/admin/key")
async def api_admin_key_disabled() -> dict[str, Any]:
    """已退休：主管理 KEY 没有了，凭据一律走「成员管理」。

    保留这条路由只为**给出明确答复**：老前端 / 老脚本调它时，得到的是一句
    「已移除，用成员密钥」，而不是 404 那种让人以为「是不是我地址写错了」的沉默。
    """
    raise HTTPException(status_code=410, detail="主管理 KEY 已移除：登录与管理都走成员密钥。")


# --------------------------------------------------------------------------- #
# 赛事届次（多届赛事：记录 / 查看 / 管理）
# --------------------------------------------------------------------------- #
@app.get("/api/events")
async def api_events(session: Session | None = Depends(optional_session)) -> dict[str, Any]:
    """届次列表：名称、状态、时间、规模与冠军。

    被「隐藏」的届次对访客不出现；服务器管理员登录后仍能看全（用于管理）。
    """
    events = await store.list_events()
    is_server = bool(session and session.is_server)
    if not is_server:
        events = [e for e in events if not e.get("hidden")]
    return {"current": store.current_id, "events": events}


async def _event_owner(event_id: str) -> str:
    """目标届的归属 uid（不存在则 404）。"""
    entry = next((e for e in await store.list_events() if e["id"] == event_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"第 {event_id} 届不存在")
    return str(entry.get("ownerUid") or "")


async def _require_event_ownership(session: Session, event_id: str) -> None:
    """赛事管理员只能操作自己创建的届；服务器管理员放行。"""
    if session.is_server:
        return
    require_event_owned(session, await _event_owner(event_id))


@app.get("/api/events/{event_id}/state")
async def api_event_state(
    event_id: str, session: Session | None = Depends(optional_session)
) -> dict[str, Any]:
    """只读查看某一届的完整战绩（不影响当前届）。"""
    try:
        cfg = await store.read_event(event_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 被隐藏的届：对访客当作不存在（服务器管理员仍可经链接查看）
    if cfg.event.hidden and not (session and session.is_server):
        raise HTTPException(status_code=404, detail=f"第 {event_id} 届不存在")
    # 往届回看：比赛一律按「没有直播」渲染（结束的比赛不可能在直播）
    state = build_state(cfg, historical=True)
    state["eventId"] = event_id
    state["eventName"] = cfg.event.name or cfg.event.title
    state["eventStatus"] = cfg.event.status
    state["siteName"] = store.site_name()
    state["readOnly"] = event_id != store.current_id
    # 成员频道是全局的：回看往届时也照样展示（它们不属于任何一届）
    state["channels"] = logic.channel_views(cfg, store.channels())
    state["liveChannels"] = await live.streaming_channel_ids()
    state["channelNotice"] = store.channel_notice()
    # 成员直播间同样是全局的；往届回看一律按「没有直播」渲染
    state["members"] = logic.member_views(
        cfg,
        [m for m in store.members() if m.active],
        bans=store.live_bans(),
        live_keys=set(),
        event_id=event_id,
        historical=True,
    )
    state["liveBans"] = [logic.ban_view(b) for b in store.live_bans() if logic.ban_active(b)]
    state["customHtml"] = store.custom_html()
    return state


@app.post("/api/events")
async def api_event_create(
    payload: EventCreatePayload, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """新建一届并切换过去；可选沿用当前届的名单与规则，并选择赛制。

    新建的届归属创建者：赛事管理员之后只能管理 / 删除自己创建的届；
    服务器管理员创建的届也归他自己，但他的身份能管全部届。
    """
    fmt = (payload.format or "").strip()
    if fmt not in ("", "league", "tournament"):
        raise HTTPException(status_code=400, detail="赛制只能是 league（积分制）或 tournament（锦标赛制）")
    cfg = await store.create_event(
        payload.name,
        copy_roster=payload.copy_roster,
        fmt=fmt,  # 留空 = 由 store 按排名模式自动选（娱乐赛事用积分制）
        owner_uid=session.uid,
    )
    log.warning(
        "已新建届次 | id=%s | 赛制=%s | 归属=%s",
        store.current_id,
        cfg.rules.format,
        session.uid or "(服务器)",
    )
    return {
        "ok": True,
        "eventId": store.current_id,
        "name": cfg.event.name,
        "format": cfg.rules.format,
        "ownerUid": session.uid,
    }


@app.post("/api/format")
async def api_set_format(payload: FormatPayload, _: Session = Depends(require_current_event)) -> dict[str, Any]:
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
async def api_event_switch(
    event_id: str, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """把某一届设为当前进行中的赛事。"""
    await _require_event_ownership(session, event_id)
    try:
        cfg = await store.switch_event(event_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "eventId": store.current_id, "name": cfg.event.name}


@app.patch("/api/events/{event_id}")
async def api_event_meta(
    event_id: str, payload: EventMetaPayload, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """重命名某一届、标记 draft / active / closed，或设置隐藏。"""
    await _require_event_ownership(session, event_id)
    patch: dict[str, Any] = {"name": payload.name, "status": payload.status}
    if payload.hidden is not None:
        patch["hidden"] = payload.hidden
    try:
        entry = await store.update_event_meta(event_id, patch)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "event": entry, "current": store.current_id}


@app.delete("/api/events/{event_id}")
async def api_event_delete(
    event_id: str, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """删除一届（至少保留一届）。"""
    await _require_event_ownership(session, event_id)
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
async def api_private(_: Session = Depends(require_current_event)) -> dict[str, Any]:
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
            # 关联的全局成员（选手就是成员）：管理端据此做「关联成员」下拉
            "memberUid": player.member_uid,
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
        # **管理端**直播配置（含 WHIP 推流地址、HLS 根地址与控制 API 账号）：
        # 公开状态里这些字段被白名单剥掉了，管理端表单必须从这里取，
        # 否则表单是空的、一保存就把根地址清成空字符串。
        # 但**API 密码明文不在这里**：本接口赛事管理员就能读，只回 hasApiPass 布尔
        # （见 logic.management_stream_config）。
        "stream": logic.management_stream_config(stream),
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
# 这些字段不能经 /api/config 改动：版本号 / 时间戳一律由服务端维护。
# 凭据**没有任何**可经这里改动的字段——成员密钥走成员管理的轮换接口，
# 主管理 KEY 那套已整体退休（见 README「登录与权限」）。
_PROTECTED_PATCH_KEYS = {"revision", "updatedAt", "version"}

#: 直播配置里**只有服务器管理员能改**的键。
#:
#: 全是**站点级**的媒体服务器设置（根地址 / API 地址与账号 / 凭据 / 默认流名 /
#: 推流令牌 / 封面 / 备注），只在「服务器 → 直播配置」里出现。直播没有「总开关」
#: （只要有赛事就允许直播，见 :class:`app.models.StreamConfig`），所以这里没有例外——
#: 任何 ``stream`` 补丁都只有服务器管理员能提交。
_STREAM_SERVER_KEYS = frozenset(
    {
        "provider",
        "baseUrl",
        "apiBase",
        "apiUser",
        "apiPass",
        "apiPassClear",
        "hlsBase",
        "streamKey",
        "pushToken",
        "mode",
        "verifyTls",
        "whipPush",
        "poster",
        "title",
        "note",
    }
)


def _apply_ui_patch(patch: dict[str, Any], session: Session) -> None:
    """界面配置只有服务器管理员能改。

    主题色 / 分享图 / 展示开关全是**站点级**的（整站共用一套外观），所以赛事管理页不再
    提供这块表单（见 ``static/js/admin.js``），写接口这边同样把关——别只靠前端藏。
    """
    if session.is_server:
        return
    raise HTTPException(
        status_code=403,
        detail="界面配置（主题色 / 分享图 / 展示开关）是站点级设置，只有服务器管理员能改。",
    )


def _apply_stream_patch(patch: dict[str, Any], session: Session) -> None:
    """直播配置补丁的两条规矩（**就地**改 ``patch``）。

    * **权限**：直播配置**整块**只有服务器管理员能改。它全是站点级的媒体服务器设置
      （根地址 / API 账号 / 凭据 / 推流令牌…），一处媒体服务器给整站所有届共用；
      而且直播没有总开关（只要有赛事就允许直播），所以赛事管理员这边没有任何
      可改的直播字段——别只靠前端藏表单；
    * **凭据**：API 密码的明文只留服务端——接口回给浏览器的是 ``hasApiPass`` 布尔
      （见 :func:`db.private_config`），所以这里「**留空 = 保持原值**」，
      要清空必须显式传 ``apiPassClear``。否则管理端一次无关的保存就把密码抹掉了，
      与 qqbot 的 API Key 是同一套规矩（那边叫 ``SECRET_KEYS``）。
    """
    if not session.is_server:
        raise HTTPException(
            status_code=403,
            detail="直播配置（媒体服务器地址 / API 账号 / 推流令牌…）是站点级设置，"
            "只有服务器管理员能改。",
        )
    # 凭据：明文只留服务端（浏览器拿到的是 hasApiPass），所以「留空 = 保持原值」——
    # 把键去掉就行：store.update 是**深合并**，没提到的键（含已存的密码）原样保留。
    # 要清空必须显式传 ``apiPassClear``。
    if patch.pop("apiPassClear", False):
        patch["apiPass"] = ""
    elif not str(patch.get("apiPass") or "").strip():
        patch.pop("apiPass", None)


def event_locked() -> bool:
    """比赛是否已开始（赛制与参赛名单冻结）。"""
    return bool(store.snapshot().event.locked)


def _require_unlocked(what: str = "赛制与参赛名单") -> None:
    """比赛开始后拒绝改动赛制 / 名单 / 组队 / 赛程重建。

    直播开关、对局替补、录分与时间登记都不走这里——它们随时可用。
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
    session: Session = Depends(require_current_event),
) -> dict[str, Any]:
    clean = {k: v for k, v in patch.items() if k not in _PROTECTED_PATCH_KEYS}
    if not clean:
        raise HTTPException(status_code=400, detail="没有需要更新的内容")
    # 直播配置：媒体服务器设置只有服务器管理员能改；API 密码「留空 = 不改」
    stream_patch = clean.get("stream")
    if isinstance(stream_patch, dict):
        _apply_stream_patch(stream_patch, session)
    # 界面配置同理（站点级外观）
    if isinstance(clean.get("ui"), dict):
        _apply_ui_patch(clean["ui"], session)
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
        # 「比赛开始后也能改，结束后只读」：赛后只放开通知，赛事信息不再让改。
        # 注意只在**真的改了内容**时才拒绝——前端保存别的字段时常会带上原值。
        if (
            current.status == "closed"
            and "rulesText" in kept
            and str(kept["rulesText"] or "") != current.rules_text
        ):
            raise HTTPException(
                status_code=400,
                detail="本届已结束：赛事信息只能查看；要发布内容请改用「赛事通知」",
            )
        clean["event"] = {**kept, "startTime": start, "endTime": end}
    # 娱乐模式（排名开关关闭）不判胜负：强制允许平局，录分时不必指定胜方
    if clean.get("event", {}).get("ranked") is False:
        rules_patch = clean.get("rules")
        if not isinstance(rules_patch, dict):
            rules_patch = {}
            clean["rules"] = rules_patch
        rules_patch["allowDraw"] = True
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
    payload: EventStartPayload, _: Session = Depends(require_current_event)
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
    payload: EventUnlockPayload, _: Session = Depends(require_current_event)
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
async def api_reload(_: Session = Depends(require_current_event)) -> dict[str, Any]:
    cfg = await store.reload(reason="web")
    return {"ok": True, "revision": cfg.revision}


@app.get("/api/export")
async def api_export(_: Session = Depends(require_current_event)) -> Response:
    """导出当前届为 JSON（备份 / 迁移用；也可作为导入他处的快照）。

    **不含任何凭据**：届配置里本来就没有凭据字段（成员密钥、Bearer 令牌都在
    ``members`` 表里，主管理 KEY 那套已整体退休），这里再把 ``members`` 兜底剥掉，
    确保导出的文件可以随便传阅。
    """
    cfg = store.snapshot()
    data = cfg.dump()
    data.pop("members", None)   # 兜底：万一哪天届配置里混进成员凭据
    payload = json.dumps(data, ensure_ascii=False, indent=2)
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
async def api_upsert_player(payload: Player, _: Session = Depends(require_current_event)) -> dict[str, Any]:
    player = payload.model_copy()
    if not player.id:
        existing = {p.id for p in store.snapshot().players}
        seq = 1
        while f"p{seq:02d}" in existing:
            seq += 1
        player.id = f"p{seq:02d}"
    if not player.name:
        raise HTTPException(status_code=400, detail="选手名称不能为空")
    # 推流流名只收 ASCII（含中文/空格的地址用不了，比较时还会抛异常）；
    # 非法字符直接报错，不静默丢字符
    player.stream_key = logic.check_stream_key(player.stream_key, "推流流名")
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

    # 编辑已有选手时若前端没回传关联成员，沿用原有 memberUid（避免改个名字就又建一位成员）
    existing_player = next((p for p in store.snapshot().players if p.id == player.id), None)
    if existing_player is not None and not player.member_uid and existing_player.member_uid:
        player = player.model_copy(update={"member_uid": existing_player.member_uid})
    # 「选手就是成员」：确保这位选手有对应的全局成员（无则自动建号，权限默认「成员」）
    member = await store.ensure_member_for_player(player)
    if member is not None:
        player = player.model_copy(update={"member_uid": member.uid})

    cfg = await store.mutate(_mutate, actor="web:player-upsert")
    return {
        "ok": True,
        "revision": cfg.revision,
        "player": player.dump(),
        "memberUid": player.member_uid,
    }


@app.delete("/api/players/{player_id}")
async def api_delete_player(player_id: str, _: Session = Depends(require_current_event)) -> dict[str, Any]:
    """删除选手（比赛开始后禁止，改由替补调整阵容）。

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
    payload: ParticipantsPayload, _: Session = Depends(require_current_event)
) -> dict[str, Any]:
    """保存本届参与选手，并按新名单自动重排未开赛对局。

    * ``playerIds`` 传空数组 = 保存一份**空名单**（本届无人参与），不是「未指定」；
    * 已完成 / 已锁定的对局永远不会被改动。

    比赛开始后名单冻结（替补由对局替补接口自动加入，不走这里）。
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
        "explicit": logic.has_custom_roster(cfg),
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


# --------------------------------------------------------------------------- #
# 成员频道（日常 / 非比赛直播；全局，跨届共享）
#
# 与赛事届次无关，因此**不受开赛锁定影响**，也不需要切换届次。
# --------------------------------------------------------------------------- #
@app.post("/api/channels")
async def api_channel_save(payload: Channel, _: Session = Depends(require_server)) -> dict[str, Any]:
    """新增 / 更新一个成员频道。

    * 流名（``streamKey``）**全局唯一**：与选手以及其它频道都不能重复（否则串流）；
    * 未给 id 时自动分配 ``c01`` 这类编号；
    * 成员频道跨届共享，开赛锁定不会拦住它。
    """
    channel = payload.model_copy()
    if not channel.name.strip():
        raise HTTPException(status_code=400, detail="频道名不能为空")
    # 流名只收 ASCII（同成员推流 ID）；非法字符直接报错。
    # 校验通过后把**规整值**写回，避免存下带首尾空格的流名。
    key = logic.check_stream_key(channel.stream_key, "频道流名")
    channel.stream_key = key
    if key:
        clash_member = next(
            (m for m in store.members() if logic.clean_key(m.stream_id) == key), None
        )
        if clash_member is not None:
            raise HTTPException(
                status_code=400,
                detail=f"流名「{key}」已被成员 {clash_member.display_name} 使用，频道请换一个互不相同的流名",
            )
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
    payload: ChannelNoticePayload, _: Session = Depends(require_server)
) -> dict[str, Any]:
    """设置「频道」板块的公告 / 异环相关内容（全局，与届次无关）。"""
    text = await store.set_channel_notice(payload.text, actor="web:channel-notice")
    return {"ok": True, "notice": text, "state": build_public_state(store.snapshot())}


class SiteNamePayload(NTEModel):
    """站点名称（全局，服务器管理员设定；留空 = 回落默认值）。"""

    name: str = ""


@app.put("/api/site/name")
async def api_site_name(
    payload: SiteNamePayload, _: Session = Depends(require_server)
) -> dict[str, Any]:
    """设置站点名称（仅服务器管理员）。

    它**不属于任何一届**（全局 meta）：顶栏、浏览器标签与主页都用它，
    所以改完立刻广播一次状态，所有在线页面当场换名字。
    """
    name = await store.set_site_name(payload.name, actor="web:site-name")
    return {"ok": True, "siteName": name, "state": build_public_state(store.snapshot())}


@app.delete("/api/channels/{channel_id}")
async def api_channel_delete(channel_id: str, _: Session = Depends(require_server)) -> dict[str, Any]:
    """删除一个成员频道。"""
    removed = await store.delete_channel(channel_id, actor="web:channel-delete")
    if not removed:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return {"ok": True, "state": build_public_state(store.snapshot())}


# --------------------------------------------------------------------------- #
# 赛程
# --------------------------------------------------------------------------- #
@app.post("/api/teams/auto")
async def api_teams_auto(payload: TeamsFormPayload, _: Session = Depends(require_current_event)) -> dict[str, Any]:
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
async def api_teams_update(payload: TeamsPayload, _: Session = Depends(require_current_event)) -> dict[str, Any]:
    """手动调整队伍（成员 / 队名 / 缩写 / 主题色 / 分组）；队伍增删时清空赛程。

    两条约定：

    * **没有成员的分组自动删除**：组队台保存时就会滤掉，接口这边同样兜一层
      （调用方不守规矩也不该留下一支空队伍——空队伍在赛程里是个永远打不了的席位）；
    * 队伍被增删（id 集合变化）会清空赛程与比分：旧对阵引用的是已经不在的队伍。

    比赛开始后禁止整体重排队伍（单个替补请用 ``/api/teams/{id}/substitute``）。
    """
    _require_unlocked("队伍成员")
    incoming = [Team.model_validate(t) for t in payload.teams]
    teams = [t for t in incoming if t.player_ids]
    dropped_empty = len(incoming) - len(teams)
    known = {p.id for p in store.snapshot().players}
    seen: set[str] = set()
    for team in teams:
        unknown = [pid for pid in team.player_ids if pid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"{team.label} 含未知选手: {', '.join(unknown)}")
        dup = [pid for pid in team.player_ids if pid in seen]
        if dup:
            raise HTTPException(status_code=400, detail=f"选手重复出现在多支队伍: {', '.join(dup)}")
        seen.update(team.player_ids)

    before_ids = {t.id for t in store.snapshot().teams}
    after_ids = {t.id for t in teams}
    dropped = before_ids != after_ids
    if dropped_empty:
        log.info(
            "保存队伍时丢弃 %d 个空分组 | 届=%s | 保留=%d",
            dropped_empty,
            store.current_id,
            len(teams),
        )

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        merged = {**data, "teams": [t.dump() for t in teams]}
        if dropped:
            merged["rounds"] = []
        return merged

    cfg = await store.mutate(_mutate, actor="web:teams-update")
    warnings = ["队伍有增删，原赛程与比分已清空，请重新生成赛程。"] if dropped else []
    return {
        "ok": True,
        "revision": cfg.revision,
        "teams": [t.dump() for t in cfg.teams],
        "count": len(cfg.teams),
        "droppedEmpty": dropped_empty,
        "warnings": warnings,
        "state": build_public_state(cfg),
    }


class TeamSubstitutePayload(NTEModel):
    """队伍换人：把 ``from_id`` 换成 ``to_id``（1:1，队伍规模不变）。"""

    from_id: str
    to_id: str


@app.post("/api/teams/{team_id}/substitute")
async def api_team_substitute(
    team_id: str, payload: TeamSubstitutePayload, _: Session = Depends(require_current_event)
) -> dict[str, Any]:
    """队伍换人（固定队伍）：**不重建赛程**，只换掉队伍里的一个人。

    用于「到不齐人」：换上的人若不在本届参与名单中会**自动加入**，比赛开始后也能用。

    与「组队台」的区别：队伍规模不变，因此既有的对阵结构依然有效，赛程不会被清空；
    该队**未结算**对局里的出场阵容会同步更新，已结算的对局保留当时阵容与比分。

    注意：这是**整队换人**（往后所有比赛都用新阵容）。只换某一场、或从某一场起换人，
    请用积分制的对局替补（``POST /api/rounds/{ref}/substitute``）。
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
        return data

    cfg = await store.mutate(_mutate, actor="web:team-substitute")
    out_name = players[payload.from_id].display_name
    in_name = players[payload.to_id].display_name
    log.warning(
        "队伍换人 | 届=%s | 队伍=%s | %s → %s | 自动加入名单=%s | 已结算对局保留=%d",
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
        "to": {"id": payload.to_id, "name": in_name},
        "addedToParticipants": _names_of(added),
        "keptRounds": kept,
        "state": build_public_state(cfg),
    }


@app.post("/api/tournament/generate")
async def api_tournament_generate(
    payload: TournamentPayload, _: Session = Depends(require_current_event)
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
    payload: GroupPairingsPayload, _: Session = Depends(require_current_event)
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
    payload: TournamentPayload, _: Session = Depends(require_current_event)
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
async def api_tournament_clear(_: Session = Depends(require_current_event)) -> dict[str, Any]:
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
    payload: SchedulePayload, _: Session = Depends(require_current_event)
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
    payload: ScheduleAppendPayload, _: Session = Depends(require_current_event)
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
async def api_round_append(_: Session = Depends(require_current_event)) -> dict[str, Any]:
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
                    {"playerIds": [], "score": metrics.MISSING, "points": 0, "rank": 0},
                    {"playerIds": [], "score": metrics.MISSING, "points": 0, "rank": 0},
                ],
            }
        )
        return data

    cfg = await store.mutate(_mutate, actor="web:round-append")
    log.info("已追加对局 | 当前共 %d 局", len(cfg.rounds))
    return {"ok": True, "revision": cfg.revision, "count": len(cfg.rounds)}


@app.delete("/api/rounds")
async def api_rounds_clear(_: Session = Depends(require_current_event)) -> dict[str, Any]:
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
async def api_round_delete(ref: str, _: Session = Depends(require_current_event)) -> dict[str, Any]:
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


class RoundSubstitutionPayload(NTEModel):
    """对局替补：把 ``from_id`` 换成 ``to_id``，``scope`` 决定影响哪些比赛。"""

    from_id: str
    to_id: str
    scope: SubScope = "round"


@app.post("/api/rounds/{ref}/substitute")
async def api_round_substitute(
    ref: str, payload: RoundSubstitutionPayload, session: Session = Depends(require_current_event)
) -> dict[str, Any]:
    """积分制替补：把某一场的 ``fromId`` 换成 ``toId``，按范围生效。

    * ``round`` 仅这场比赛；``rest`` 这场比赛**及其之后**；``event`` 全场；
    * **已结算的对局不改写**：打完的比赛保留当时实际出场的阵容与比分（在 ``lockedRounds`` 里列出）；
    * 替补在该场已经上场时跳过那一场（``conflictRounds``），免得同一个人两边都是他；
    * 同一位选手在同一范围**只会有一处替补**：再次指定即为改人，最终只保留最后一次；
    * 换上的人若不在本届参与名单里会**自动加入**；比赛开始后依然可用。
    """
    _require_league()
    cfg_now = store.snapshot()
    players = {p.id: p for p in cfg_now.players}
    from_id = (payload.from_id or "").strip()
    to_id = (payload.to_id or "").strip()
    scope: SubScope = payload.scope if payload.scope in ("round", "rest", "event") else "round"
    if from_id not in players:
        raise HTTPException(status_code=404, detail="被换下的选手不存在")
    if to_id not in players:
        raise HTTPException(status_code=404, detail="替补选手不存在，请先在「选手名单」里新增")
    if from_id == to_id:
        raise HTTPException(status_code=400, detail="被换下的选手与替补不能是同一位")
    from_name = players[from_id].display_name
    to_name = players[to_id].display_name

    anchor = ""
    if scope != "event":
        target = _find_round(cfg_now, ref)
        if target is None:
            raise HTTPException(status_code=404, detail=f"对局 {ref} 不存在")
        anchor = target.code or str(target.index)
    probe = Substitution(from_id=from_id, to_id=to_id, scope=scope, anchor=anchor)
    # 「在不在场上」按**原始阵容**判断（见 subs.original_lineup）：替补已经改写过了阵容，
    # 改人时原来的选手早就不在场上了，只看当前阵容会把他自己判成「不在阵容里」。
    if not subs.appears_in(cfg_now.dump(), probe):
        raise HTTPException(
            status_code=400,
            detail=f"{from_name} 不在所选范围的任何阵容里，安排替补没有意义",
        )

    actor = session.name or session.uid or "admin"
    added: list[str] = []
    info: dict[str, list[str]] = {}
    saved_id = ""

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal info, saved_id
        existing = subs.load(data)
        # 同一选手 + 同一范围：先把上一处换回去，再应用新的（最终只留最后一次）
        old = next(
            (s for s in existing if subs.same_slot(s, from_id=from_id, scope=scope, anchor=anchor)),
            None,
        )
        if old is not None:
            subs.revert(data, old)
        fresh = Substitution(
            id=old.id if old is not None else subs.new_id(existing),
            from_id=from_id,
            to_id=to_id,
            scope=scope,
            anchor=anchor,
            created_at=now_iso(),
            created_by=actor,
        )
        info = subs.apply(data, fresh)
        if not info["changed"]:
            if info["locked"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"所选范围内的比赛都已结算（{'、'.join(info['locked'])}），"
                        "按约定不改写已打完的阵容"
                    ),
                )
            if info["conflict"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{to_name} 已经在本场阵容里，换了会出现同一个人两边都是他；"
                        "请先把他移出，或换一位替补"
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail=f"{from_name} 在未结算的比赛里都没有上场，没有可替换的位置",
            )
        # 记录只保留有效项 + 新的这一条；旧记录若换了人则原 id 复用（列表里不会出现两条）
        data["substitutions"] = [
            s.dump() for s in existing if s.id != fresh.id
        ] + [fresh.dump()]
        added.extend(_ensure_participants(data, [to_id]))
        saved_id = fresh.id
        return data

    cfg = await store.mutate(_mutate, actor="web:round-substitute")
    log.warning(
        "已安排替补 | 届=%s | %s → %s | 范围=%s | 起点=%s | 改动对局=%s | 保留已结算=%s | 冲突跳过=%s",
        store.current_id,
        from_name,
        to_name,
        scope,
        anchor or "(全场)",
        "、".join(info["changed"]),
        "、".join(info["locked"]) or "无",
        "、".join(info["conflict"]) or "无",
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "id": saved_id,
        "from": {"id": from_id, "name": from_name},
        "to": {"id": to_id, "name": to_name},
        "scope": scope,
        "scopeLabel": logic.SUB_SCOPE_LABEL.get(scope, scope),
        "changedRounds": info["changed"],
        "lockedRounds": info["locked"],
        "conflictRounds": info["conflict"],
        "addedToParticipants": _names_of(added),
        "state": build_public_state(cfg),
    }


@app.post("/api/substitutions/{sub_id}/cancel")
async def api_substitution_cancel(
    sub_id: str, _: Session = Depends(require_current_event)
) -> dict[str, Any]:
    """取消一处替补：把换上的选手换回原来那位（只动**未结算**的对局）。

    已结算的对局保留当时实际出场的阵容与比分，不做回溯改写。
    """
    _require_league()
    info: dict[str, list[str]] = {}
    names: dict[str, str] = {}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal info
        existing = subs.load(data)
        target = next((s for s in existing if s.id == sub_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="这处替补不存在（可能已经被取消）")
        info = subs.revert(data, target)
        players = {str(item.get("id")): item for item in (data.get("players") or [])}
        names["from"] = str((players.get(target.from_id) or {}).get("name") or target.from_id)
        names["to"] = str((players.get(target.to_id) or {}).get("name") or target.to_id)
        data["substitutions"] = [s.dump() for s in existing if s.id != sub_id]
        return data

    cfg = await store.mutate(_mutate, actor="web:substitution-cancel")
    log.warning(
        "已取消替补 | 届=%s | id=%s | %s ← %s | 还原对局=%s | 保留已结算=%s",
        store.current_id,
        sub_id,
        names.get("from", ""),
        names.get("to", ""),
        "、".join(info["changed"]),
        "、".join(info["locked"]) or "无",
    )
    return {
        "ok": True,
        "revision": cfg.revision,
        "id": sub_id,
        "revertedRounds": info["changed"],
        "lockedRounds": info["locked"],
        "state": build_public_state(cfg),
    }


@app.post("/api/rounds/{ref}/lineup")
async def api_round_lineup(
    ref: str, payload: RoundLineupPayload, _: Session = Depends(require_current_event)
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

    **未指定名单**（无 ``participantsSet`` 且名单为空 = 全员参与）时无需补；
    补进去的选手从下一局起就算作本届参与者（会出现在排名榜与用户端名单里）。
    """
    current = list(data.get("participants") or [])
    if not (data.get("participantsSet") or current):
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
    ref: str, payload: RoundStatusPayload, _: Session = Depends(require_current_event)
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
                # 重置回「没有成绩」——0 是合法读数，不能用它表示「清空」
                side["score"] = metrics.MISSING
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
    ref: str, payload: RoundWalkoverPayload, _: Session = Depends(require_current_event)
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
        # 弃权 = 没有成绩（而不是「0 分」：数值型的 0 是合法读数）
        raw[index]["score"] = metrics.MISSING
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
    ref: str, payload: RoundTimesPayload, _: Session = Depends(require_current_event)
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
    ref: str, payload: RoundResultPayload, _: Session = Depends(require_current_event)
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
    # 淘汰赛必须分出胜负（双败赛制不接受平局）；小组赛与积分制常规局可按规则允许平局。
    # 娱乐模式（不排名）例外：任何场次都允许平局，只为「记下来」。
    ranked = bool(cfg_now.event.ranked)
    # 娱乐模式（不排名）下任何场次都允许平局；否则只有小组赛 / 积分制常规局按规则允许
    allow_draw = (not ranked) or (
        cfg_now.rules.allow_draw and target.stage in ("group", "league")
    )
    # 计分口径决定「谁赢」：数值高胜还是数值低胜（见 app/metrics.py）
    scoring = cfg_now.rules.scoring

    side_count = len(target.sides)
    valid_keys = [chr(ord("A") + i) for i in range(side_count)]
    explicit = (payload.winner or "").upper()
    if explicit and explicit not in (*valid_keys, "DRAW"):
        raise HTTPException(
            status_code=400, detail=f"winner 只能是 {' / '.join(valid_keys)} 或 DRAW"
        )
    # 负数只有两个来源：真填错了，或者 metrics.MISSING（「没有成绩」的哨兵）。
    # 数值型的 0 是合法读数，所以「没填」必须走哨兵这条路，不能靠 0 兼职。
    for item in payload.sides:
        if (item.score < 0 and item.score != metrics.MISSING) or item.points < 0:
            raise HTTPException(status_code=400, detail="比分与得分不能为负数")
    for legacy in (payload.score_a, payload.score_b):
        if legacy is not None and legacy < 0 and legacy != metrics.MISSING:
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
        # 整轮都没填的行丢掉（前端也会滤，但接口不能指望调用方守规矩）：
        # 留下 (-1, -1) 会让合计出现负数
        rnd["sets"] = [
            item.dump()
            for item in payload.sets
            if scoring.has_result(item.a) or scoring.has_result(item.b)
        ]

    def apply(rnd: dict[str, Any]) -> None:
        from .store import now_iso

        _write_entered(rnd)
        model = Round.model_validate(rnd)
        auto = tournament.judge_round(model, allow_draw=allow_draw, scoring=scoring)
        winner = explicit or auto
        if explicit and explicit != "DRAW" and explicit != auto:
            # 人工指定第 1 名：把指定方钉在 1，其余按得分顺序依次排 2、3、4
            keys = [chr(ord("A") + i) for i in range(len(model.sides))]
            winner_index = keys.index(explicit) if explicit in keys else -1
            if winner_index >= 0:
                counted = len(model.sides) == 2 and bool(model.sets)
                others = sorted(
                    (i for i in range(len(model.sides)) if i != winner_index),
                    key=lambda i: scoring.judge_key(
                        model.sides[i].score, model.sides[i].points, counted=counted
                    ),
                )
                model.sides[winner_index].rank = 1
                for offset, index in enumerate(others, start=2):
                    model.sides[index].rank = offset
            model.winner = winner
        if not winner:
            if ranked:
                tied_text = f"{scoring.label_text}相同"
                who = "并列第一" if side_count > 2 else tied_text
                hint = "请直接指定胜方" if not allow_draw else "请直接指定胜方或标记为平局"
                raise HTTPException(status_code=400, detail=f"{who}，无法判定晋级：{hint}")
            # 娱乐模式：分不出胜负就直接记平局，绝不因为「没点胜方」而卡住记录
            winner = "DRAW"
            model.winner = "DRAW"
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
    ref: str, payload: RoundLivePayload, _: Session = Depends(require_current_event)
) -> dict[str, Any]:
    """本场直播开关：是否为本场比赛推流，以及直播选手提示。"""

    def apply(rnd: dict[str, Any]) -> None:
        rnd["live"] = bool(payload.enabled)
        rnd["liveNote"] = (payload.note or "").strip()

    cfg = await store.mutate(_round_mutator(ref, apply), actor="web:round-live")
    log.info("对局 %s 直播开关 = %s | %s", ref, payload.enabled, payload.note)
    return {"ok": True, "revision": cfg.revision, "live": bool(payload.enabled)}


@app.post("/api/rounds/{ref}/reset")
async def api_round_reset(ref: str, _: Session = Depends(require_current_event)) -> dict[str, Any]:
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
    payload: AvatarUploadPayload, _: Session = Depends(require_event)
) -> dict[str, Any]:
    """接收 data:URL 头像并落盘，返回同源可访问地址。"""
    try:
        url = avatars.save_data_url(payload.data_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "url": url}


@app.get("/api/avatar/file/{name}")
async def api_avatar_file(name: str) -> Response:
    """读取本地上传的头像（文件名白名单 + 内容哈希，天然防穿越）。

    图片现在都放在统一仓库里（见 :mod:`app.media`），但**这个地址保持不变**：
    升级前的成员资料里存的就是它，改了等于把老头像全弄丢。
    """
    path = avatars.resolve_local(name)
    if path is None:
        raise HTTPException(status_code=404, detail="头像不存在")
    return FileResponse(
        path,
        media_type=media.mime_for(path),
        # 与 /api/media/<哈希> 用同一份缓存头：它们指向的是**同一个仓库里的同一张图**，
        # 策略不一致只会造成「换个地址访问就换了行为」这种最难查的问题。
        headers={**media.IMMUTABLE_HEADERS, "X-NTE-Avatar": "upload"},
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
# 操作日志
# --------------------------------------------------------------------------- #
@app.get("/api/activity")
async def api_activity(
    limit: int = 60,
    _: Session = Depends(require_server),  # noqa: B008  (FastAPI 依赖注入惯例)
) -> dict[str, Any]:
    """最近的操作日志（服务器管理员）。

    记录由 :class:`AuditMiddleware` 完成：只记非 GET 的 ``/api`` 请求的
    「谁 + 何时 + 方法 + 路径 + 状态码」，**请求体一律不记**。
    """
    items = await store.activity(max(1, min(int(limit or 60), 600)))
    return {"items": items}


@app.get("/api/credits")
async def api_credits() -> dict[str, Any]:
    """版权与开源组件清单（页脚「开源组件」面板用）。

    公开只读：都是「本站在用哪些开源作品、各自什么许可」这类信息，
    没有任何凭据，也不需要登录——署名本来就应该让任何人都看得到。
    """
    return {"ok": True, **credits.payload()}


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
if STATIC_DIR.exists():
    # PWA manifest 的 MIME 不在 Python 默认表里，不注册会被当成 octet-stream 而拒绝加载
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    return render_index(await page_meta("/", str(request.base_url)))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/og.png", include_in_schema=False)
async def og_image() -> Response:
    """默认分享图（1200×630）。放在这里而不是 /static 下：路径短、且不受资源版本号影响。

    管理端在「界面配置」里填了「分享图」就会改用那张，这个路由只是兜底。
    """
    art = STATIC_DIR / "og.png"
    if not art.exists():
        raise HTTPException(status_code=404, detail="没有内置分享图")
    return FileResponse(art, media_type="image/png", headers={"Cache-Control": "public, max-age=600"})


#: 「帮助图」认这几个文件名（按顺序取第一个存在的）。JPEG 优先：同画面比 PNG 小得多，
#: 而 QQ 会再压一道、不吃 PNG 的无损；SVG 放最后（QQ 对它支持不好）。
HELP_IMAGE_NAMES = ("help.jpg", "help.jpeg", "help.png", "help.webp", "help.svg")


@app.api_route("/help.jpg", methods=["GET", "HEAD"], include_in_schema=False)
async def help_image() -> Response:
    """QQ 机器人**帮助图**：把图丢进 `static/`（如 `static/help.jpg`）就能用。

    为什么单开一条路由、而不是让插件填 `/static/help.jpg`：路径短、**不受资源版本号
    影响**，换图后地址不变——插件那边的 `help_image` 一次配好就不用再动（同 `/og.png`）。

    **必须显式写上 HEAD**：插件判断「站点上有没有这张图」用的就是一次 HEAD（比 GET 省流量），
    而 FastAPI 的 `@app.get` **不挂 HEAD**（会回 405）——那样插件会一直以为没图，
    帮助图静默地永远不生效（这个坑真踩过，`test_api` 里有防回归）。

    没放图时回 404 并说清放哪儿，而不是给一张空白图：群友看到的是插件的文字说明兜底，
    站长在日志/调试里能立刻知道是「图还没放」而不是「配置填错了」。
    """
    for name in HELP_IMAGE_NAMES:
        art = STATIC_DIR / name
        if art.exists():
            media = mimetypes.guess_type(name)[0] or "image/png"
            # 不缓存：图本来就很少被请求（有人问才取一次），换图要立刻生效
            return FileResponse(art, media_type=media, headers={"Cache-Control": "no-cache"})
    raise HTTPException(
        status_code=404,
        detail="还没有帮助图：把图存成 static/help.jpg（或 .png / .webp），或改插件配置用别的地址",
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(request: Request, full_path: str) -> Response:
    """非 API 路径统一回落到首页，方便移动端直接收藏子路径。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    if not (STATIC_DIR / "index.html").exists():
        raise HTTPException(status_code=404, detail="页面不存在")
    return render_index(await page_meta(full_path, str(request.base_url)))


__all__ = ["app"]
