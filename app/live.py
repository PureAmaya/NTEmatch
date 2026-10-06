"""直播信息与源站探测。

本模块**不做反代**：前端拿到的就是媒体服务器（MediaMTX）的**源地址**，
推流用 ``https://<媒体服务器>:8889/<流名>/whip``（WHIP），
观看用 ``https://<媒体服务器>:8889/<流名>``（WebRTC）或
``https://<媒体服务器>:8888/<流名>``（HLS）。

因此有两件事需要媒体服务器侧配合（都在 README 里写明）：

* **跨域**：WebRTC 观看是浏览器直接 POST 到另一个源，MediaMTX 默认会回
  ``Access-Control-Allow-Origin: *``，无需额外配置；
* **证书**：站点是 HTTPS 时浏览器不允许混用 ``http://`` 源（混合内容会被拦），
  所以源地址也要用 HTTPS 且证书要受浏览器信任——自签名证书只有服务端探测
  可以关掉校验（``verifyTls``），观众侧仍会被浏览器拦。

MediaMTX 的 HLS 与 WebRTC 是**两个独立端口**（``8888`` / ``8889``），
``/health`` 会分别探测并回报，地址写错时能直接看出要改哪一个。
"""

from __future__ import annotations

import asyncio
import hmac
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import model_validator

from . import logic
from .auth import verify_secret
from .logging_conf import get_logger
from .models import Config, LiveBan, NTEModel, StreamConfig
from .store import store
from .ws import hub

log = get_logger("live")

router = APIRouter(prefix="/api/live", tags=["live"])

# 校验证书 / 不校验证书各一个连接池（证书校验只能在创建客户端时指定）
_clients: dict[bool, httpx.AsyncClient] = {}
#: 这些连接池是在哪个事件循环上建的（见 :func:`_client_get`）
_clients_loop: asyncio.AbstractEventLoop | None = None


def _client_get(verify: bool = True) -> httpx.AsyncClient:
    """取一个连接池（证书校验 / 不校验各一个）。

    ``httpx.AsyncClient`` **绑死在创建它的那个事件循环上**：换一个循环再拿旧客户端发请求，
    会直接报 ``RuntimeError: Event loop is closed``（连接池里全是上一个循环的句柄）。
    长驻服务只有一个循环，但脚本里反复 ``asyncio.run``、测试里每个用例一个新循环都会撞上
    ——所以这里记住建池时的循环，发现换了就整体重建（旧池的子连接已经没用了，
    异步关闭也不敢在别的循环里 await，直接丢引用交给 GC）。
    """
    global _clients_loop
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _clients_loop is not loop:
        _clients.clear()
        _clients_loop = loop
    client = _clients.get(verify)
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=6.0, read=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            verify=verify,
        )
        _clients[verify] = client
    return client


async def aclose() -> None:
    global _clients_loop
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()
    _clients_loop = None


# =========================================================================== #
# 探测策略：**常驻后台探测 + 请求路径只读缓存**
#
# 媒体服务器（MediaMTX）不可达时，一次探测要一直等到超时才返回。以前
# ``/api/state`` 会同步等 4 次这样的探测（主直播间 / 选手 / 成员频道 / 状态视图），
# 媒体服务器没开时首屏就要卡十几秒 —— 而且只缓存成功结果，失败还会反复重试。
#
# 现在的分工：
#
#   * **常驻探测**（:func:`watch_loop`，由启动流程拉起）：没人访问也一直在探
#     （间隔见 ``WATCH_INTERVAL`` / ``WATCH_IDLE_INTERVAL``），所以「谁在直播」永远是
#     新数据，任何页面一打开读到的就是刚探回来的结果；状态**变了**就通过 WebSocket
#     广播出去（``{"type": "live"}``），前端因此不必频繁轮询；
#   * ``/api/state`` 与 ``/api/live/health`` **只读缓存**，因此永远毫秒级返回；
#     前者顺手 ``kick_refresh`` 兜一下（有人刚打开页面时催一次，避免看到一轮前的数据）；
#   * 只有显式 ``probe=1``（「刷新信号」）才现场同步探测。
# =========================================================================== #
#
# ``ok`` 区分「探测成功」与「探测失败」，**失败结果同样要缓存**——否则同一页加载
# 里的连续查询会各自重试一遍。``reason`` 记下上一次失败的原因（如 401 鉴权失败）。
_ready_cache: dict[str, Any] = {
    "at": float("-inf"),
    "paths": None,
    "ok": False,
    "attempted": False,
    "reason": "",
}
# 单飞锁：同一时刻只允许一次真实探测，其余调用等它结束后复用结果（避免惊群）
_ready_lock = asyncio.Lock()
# 端口探测（WebRTC / HLS 两个端口）的最近结果，同样由按需刷新更新
_health_cache: dict[str, Any] = {"at": float("-inf"), "probes": None}

_READY_TTL_OK = 8.0        # 探测成功的结果可复用多久
_READY_TTL_FAIL = 5.0      # 探测失败的结果可复用多久
_READY_TIMEOUT = 3.0       # 控制 API 单次探测超时
_PROBE_TIMEOUT = 4.0       # 端口探测单次超时
_PROBE_TTL = 60.0          # 端口探测结果可复用多久（比控制 API 贵，端口通不通也很少变）
# 两次真实探测之间的最小间隔：即使调用方要求「强制刷新」，短时间内的并发请求也复用
# 同一次结果。否则多人同时点「刷新信号」会在锁上串行排队（N 个客户端 × 超时）。
_READY_MIN_INTERVAL = 0.5

# 按需刷新的节流与新鲜度上限
_REFRESH_MIN_INTERVAL = 1.0   # 两次按需刷新之间的最小间隔
_SNAPSHOT_MAX_AGE = 30.0      # 缓存超过这么久没刷新就按「查不到」处理，不给过期的直播中标记

_refresh_task: asyncio.Task | None = None
_last_refresh = float("-inf")

# 失败日志节流：无人值守时探测会反复失败，同一原因不能每轮都刷一条 warning。
_FAIL_LOG_REPEAT = 60.0
_fail_log: dict[str, Any] = {"at": float("-inf"), "reason": ""}


def _log_ready_failure(reason: str) -> None:
    """记录一次探测失败；同一原因 60 秒内只报一次（其余降到 debug）。"""
    now = time.monotonic()
    if reason == _fail_log["reason"] and now - _fail_log["at"] < _FAIL_LOG_REPEAT:
        log.debug("MediaMTX 推流状态查询失败（同前） | %s", reason)
        return
    _fail_log.update({"at": now, "reason": reason})
    log.warning("MediaMTX 推流状态查询失败 | %s", reason)


def api_auth(stream: StreamConfig) -> tuple[str, str] | None:
    """MediaMTX 控制 API 的 Basic 认证；没填用户名就不带认证。

    ``mediamtx.yml`` 里配了 ``authInternalUsers``（比如 ``user: admin``）时必填，
    否则控制 API 回 401，表现为前端那句「媒体服务器 API 不可达」。
    用户名 / 密码就在「直播配置 → MediaMTX API 用户名 / 密码」里填。
    """
    user = (stream.api_user or "").strip()
    if not user:
        return None
    return (user, stream.api_pass or "")


async def ready_paths(max_age: float | None = None) -> set[str] | None:
    """媒体服务器上报的**正在推流**路径集合；``None`` = 查不到（未配置 / 不可达）。

    MediaMTX 控制 API 的 ``GET /v3/paths/list`` 里每个 path 的 ``ready`` 表示
    「当前有推流端连着」。地址在「直播配置」里填（默认 ``:9997``）；
    媒体服务器开了 API 鉴权（mediamtx.yml 的 authInternalUsers）时，
    用户名 / 密码也在那里填，这里按 Basic 认证带上。

    ``max_age``：缓存复用时长。``None`` = 按上一次结果自动取
    （成功 8s / 失败 5s）；``0.0`` = 强制重新探测（「刷新信号」用）。
    """
    cfg = store.snapshot().stream
    api = (cfg.api_base or "").strip().rstrip("/")
    if not api:
        return None
    ttl = max_age if max_age is not None else (_READY_TTL_OK if _ready_cache["ok"] else _READY_TTL_FAIL)
    if time.monotonic() - _ready_cache["at"] < ttl:
        return _ready_cache["paths"]

    # 单飞：同一时刻只允许一次真实探测，其余调用等它结束后复用结果
    async with _ready_lock:
        if time.monotonic() - _ready_cache["at"] < max(ttl, _READY_MIN_INTERVAL):
            return _ready_cache["paths"]
        started = time.monotonic()
        try:
            resp = await _client_get(bool(cfg.verify_tls)).get(
                f"{api}/v3/paths/list", timeout=_READY_TIMEOUT, auth=api_auth(cfg)
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("items") or []
            paths = {str(item.get("name") or "") for item in items if item.get("ready")}
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            # 401 / 403 = 媒体服务器开了 API 鉴权：把用户名 / 密码填上就好
            reason = (
                f"MediaMTX API 鉴权失败（HTTP {code}）：请在「直播配置」里填 API 用户名 / 密码"
                if code in (401, 403)
                else f"MediaMTX API 返回 HTTP {code}：请检查 API 地址"
            )
            _log_ready_failure(reason)
            _ready_cache.update(
                {"at": time.monotonic(), "paths": None, "ok": False, "attempted": True, "reason": reason}
            )
            return None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            elapsed = time.monotonic() - started
            # 超时是「媒体服务器不可达」最常见的表现，耗时记下来便于排查
            _log_ready_failure(f"{_friendly_error(exc)}（耗时 {elapsed:.1f}s / {type(exc).__name__}）")
            _ready_cache.update(
                {
                    "at": time.monotonic(),
                    "paths": None,
                    "ok": False,
                    "attempted": True,
                    "reason": _friendly_error(exc),
                }
            )
            return None
        # 恢复成功：清掉失败节流，下次再挂能立刻报到日志里
        _fail_log.update({"at": float("-inf"), "reason": ""})
        _ready_cache.update(
            {"at": time.monotonic(), "paths": paths, "ok": True, "attempted": True, "reason": ""}
        )
        log.debug(
            "MediaMTX 上报正在推流 | %s | %.0fms",
            sorted(paths),
            (time.monotonic() - started) * 1000,
        )
        return paths


def ready_paths_snapshot() -> set[str] | None:
    """**只读缓存**的推流路径集合：不发任何网络请求。

    以下三种情况都返回 ``None``（= 查不到）：

    * 还没探测过（冷启动的头一两秒）；
    * 上次探测失败（媒体服务器未配置 / 不可达）；
    * 缓存已经太旧（超过 ``_SNAPSHOT_MAX_AGE``）——常驻探测（:func:`watch_loop`）
      每几秒刷一次，所以「过期」只可能是探测任务停了或媒体服务器一直不可达；
      宁可说「查不到」，也不能把最后一次结果永久挂成「直播中」。

    新鲜度由常驻探测负责，请求处理方直接拿走即可。
    """
    if time.monotonic() - _ready_cache["at"] > _SNAPSHOT_MAX_AGE:
        return None
    return _ready_cache["paths"]


async def streaming_player_ids(cfg: Config) -> list[str]:
    """**真的在推流**的选手：他的流名出现在媒体服务器上报的列表里。

    只读后台缓存，因此**不会拖慢调用方**；查不到（API 未配置 / 不可达）时返回
    空列表——宁可少显示「直播中」，也不要给观众一个假的直播标记。
    """
    ready = ready_paths_snapshot()
    if not ready:
        return []
    return [p.id for p in cfg.players if logic.clean_key(p.stream_key) in ready]


async def streaming_channel_ids() -> list[str]:
    """**真的在推流**的成员频道：流名出现在媒体服务器上报的列表里。

    与选手机位同一套判断（宁可少显示，也不给假的直播标记）；只读后台缓存，
    查不到（API 未配置 / 不可达）时返回空列表。
    """
    ready = ready_paths_snapshot()
    if not ready:
        return []
    return [c.id for c in store.channels() if logic.clean_key(c.stream_key) in ready]


async def streaming_member_uids() -> list[str]:
    """**真的在推流**的成员：他的推流 ID 出现在媒体服务器上报的列表里。

    成员只要开播就自动出现在频道里（无需管理员批准），所以「直播中」标记
    也一律以媒体服务器上报为准；查不到就是空列表。
    """
    ready = ready_paths_snapshot()
    if not ready:
        return []
    return [m.uid for m in store.members() if logic.clean_key(m.stream_id) in ready]


# --------------------------------------------------------------------------- #
# B站直播：**只探开播状态 + 直嵌官方外链播放器**
#
# 成员的 B站 直播和 MediaMTX 那套完全独立：视频流我们**一概不碰**
# （不中继、不转码、不代理），只做两件事：
#
# 1. 用 B站**免登录**的开播状态接口判断他此刻在不在播：
#    ``GET https://api.live.bilibili.com/room/v1/Room/get_info?room_id=<房间号>``
#    里的 ``live_status``（0 未开播 / 1 直播中 / 2 轮播）；
# 2. 在播时告诉前端「把官方外链播放器嵌进来」，并把跳转地址一并给出。
#
# 播放器用的是 B站官方文档里的「嵌入活动播放器」：
# ``https://www.bilibili.com/blackboard/live/live-activity-player.html?cid=<房间号>``
# （`danmaku` / `logo` / `sendpanel` 参数见官方文档，0 = 不显示）。
#
# 两条刻意的约定：
#
# * 探测失败**不影响任何别的直播功能**：当作「不知道」，前端仍给出跳转链接，
#   观众照样能点进 B站 看（只是本站不再自己判断「在播」）；
# * 探测结果同样只读缓存（``BILI_TTL``），由按需刷新更新——请求路径永不等待 B站。
# --------------------------------------------------------------------------- #
BILI_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
BILI_ROOM_PAGE = "https://live.bilibili.com/{room}"
BILI_EMBED = "https://www.bilibili.com/blackboard/live/live-activity-player.html"
BILI_TTL = 20.0          # 开播状态可复用多久（B站 有风控，别打太勤）
BILI_TIMEOUT = 4.0       # 单次探测超时
#: B站 接口对浏览器特征比较敏感：没有 UA 容易直接 403，Referer 也一并带上
BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
}
_bili_cache: dict[str, Any] = {
    "at": float("-inf"),
    "known": False,
    "rooms": {},
    "missing": {},
    "reason": "",
}


def bili_jump_url(room: str) -> str:
    """B站直播间跳转地址（观众点「在 B站打开」用）。"""
    return BILI_ROOM_PAGE.format(room=room) if room else ""


def bili_embed_url(room: str) -> str:
    """B站官方外链播放器地址（**直连 B站，不经本站**）。

    关掉弹幕、水印与右侧互动区：这一路是嵌在赛场页面里的一个画面，
    要弹幕 / 送礼这些完整功能请点「在 B站打开」。
    """
    if not room:
        return ""
    return f"{BILI_EMBED}?cid={room}&danmaku=0&logo=0&sendpanel=0"


def _bili_area(data: dict[str, Any]) -> str:
    """B站直播间分区（字段名在不同版本里换过几次，这里按候选依次取）。"""
    for key in ("area_name", "parent_area_name", "areaName", "parentName"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _bili_live_time(data: dict[str, Any]) -> str:
    """开播时间：B站 给的是**秒级时间戳字符串**，这里转成本站的 ISO 文本。

    拿不到就返回空串（前端不显示这一行），不要为了「看起来完整」编一个时间。
    """
    raw = str(data.get("live_time") or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return ""
    try:
        # 本地无时区（与站内其它时间戳一致），因此不传 tz
        return datetime.fromtimestamp(int(raw)).replace(microsecond=0).isoformat()  # noqa: DTZ006
    except (OverflowError, OSError, ValueError):
        return ""


def bili_rooms_wanted() -> dict[str, str]:
    """需要探测的房间号 → 成员 uid（只取启用且有房间号的成员）。"""
    return {
        m.bili_room: m.uid
        for m in store.members()
        if m.active and m.bili_room
    }


async def bili_probe(*, force: bool = False) -> dict[str, Any]:
    """探测成员们的 B站 直播间是否在播（结果进缓存）。

    ``known=False`` 表示**一个都没探到**（网络不通 / 被风控）：这时前端不该说
    「没人播」，只能说「不知道」——和 MediaMTX 那套一样，宁可不说，不给假信息。
    """
    wanted = bili_rooms_wanted()
    if not wanted:
        _bili_cache.update(
            {"at": time.monotonic(), "known": True, "rooms": {}, "missing": {}, "reason": ""}
        )
        return bili_snapshot()
    if not force and time.monotonic() - _bili_cache["at"] < BILI_TTL:
        return bili_snapshot()

    client = _client_get(True)

    async def one(room: str) -> tuple[str, dict[str, Any] | None, str]:
        """返回 ``(房间号, 详情 | None, 错误说明)``。

        「**明确没有这个直播间**」与「**探测失败**」分开记：前者是确定的答案
        （填错了，能直接告诉人），后者只能叫「不知道」。混成一种就会把
        「房间号写错了」说成「B站 暂时不可用」，用户永远查不出问题在哪。
        """
        try:
            resp = await client.get(
                BILI_API, params={"room_id": room}, headers=BILI_HEADERS, timeout=BILI_TIMEOUT
            )
            if resp.status_code != 200:
                return room, None, f"B站接口返回 HTTP {resp.status_code}"
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return room, None, f"B站接口不可达：{exc.__class__.__name__}"
        code = int(payload.get("code") or 0)
        if code != 0:
            message = str(payload.get("message") or f"B站返回 code={code}")
            # -400 = 房间不存在；其余（-352 / -412 之类）是风控或参数问题
            return room, None, ("不存在" if code == -400 else "") + message
        data = payload.get("data") or {}
        status = int(data.get("live_status") or 0)
        return (
            room,
            {
                "room": str(data.get("room_id") or room),
                "shortId": str(data.get("short_id") or ""),
                "live": status == 1,
                "replay": status == 2,          # 2 = 轮播（不在直播，但也不是「没开播」）
                # 以下这些就是「开播后自动同步到本站」的东西：标题、主播名、
                # 在线人数、分区、开播时间——全部按需从 B站 现取，不需要成员手填。
                "title": str(data.get("title") or ""),
                "uname": str(data.get("uname") or ""),
                "online": int(data.get("online") or 0),
                "uid": str(data.get("uid") or ""),
                "area": _bili_area(data),
                "liveTime": _bili_live_time(data),
            },
            "",
        )

    results = await asyncio.gather(*(one(room) for room in sorted(wanted)))
    rooms: dict[str, dict[str, Any]] = {}
    missing: dict[str, str] = {}
    errors: list[str] = []
    for room, info, error in results:
        if info is not None:
            rooms[room] = info
        elif error:
            if "不存在" in error:
                missing[room] = error
            else:
                errors.append(error)
    # 「知道」= 至少有一个房间得到了确定答案（存在，或明确不存在）
    known = bool(rooms or missing)
    reason = ""
    if not known:
        # 同一个原因反复出现只留一条，日志 / 界面都不至于刷屏
        reason = errors[0] if errors else "B站接口没有返回可用的房间信息"
        log.warning("B站 直播状态探测失败（不影响其它直播功能） | %s", reason)
    else:
        for room in missing:
            log.info("B站 直播间不存在（房间号可能填错）| room=%s", room)
        if errors:
            log.debug("部分 B站 直播间探测失败 | %s", "；".join(errors[:3]))
    _bili_cache.update(
        {
            "at": time.monotonic(),
            "known": known,
            "rooms": rooms,
            "missing": missing,
            "reason": reason,
        }
    )
    return bili_snapshot()


def bili_snapshot() -> dict[str, Any]:
    """**只读缓存**的 B站 开播状态（绝不发网络请求）。"""
    fresh = time.monotonic() - _bili_cache["at"] <= max(BILI_TTL * 3, 60.0)
    rooms = _bili_cache["rooms"] if fresh else {}
    missing = _bili_cache["missing"] if fresh else {}
    known = bool(_bili_cache["known"]) if fresh else False
    return {
        "known": known,
        "reason": "" if known else (_bili_cache["reason"] or "尚未检测 B站 开播状态"),
        "rooms": rooms,
        # B站 **明确说没有**的直播间（房间号多半填错了）：与「探不到」不是一回事
        "missing": missing,
        # 正在直播的那些（room 号 → 详情），前端据此多出一路 B站 机位
        "live": {room: info for room, info in rooms.items() if info.get("live")},
    }


def bili_view() -> dict[str, Any]:
    """给前端 / 推送用的 B站 直播视图。

    ``items`` 是**正在直播**的成员，除了 uid / 名字，还带上从 B站 现取的
    **标题 / 主播名 / 在线人数 / 分区 / 开播时间**——这些就是「开播后自动同步到本站」
    的东西，成员不需要（也没法）在这里手填。

    播放器与跳转地址一律用 B站 返回的**真实房间号**（``room_id``）：成员填短号也能用，
    而外链播放器的 ``cid`` 只认真实房间号。
    """
    owners = {m.bili_room: m for m in store.members() if m.active and m.bili_room}
    items: list[dict[str, Any]] = []
    for room, info in (bili_snapshot().get("live") or {}).items():
        member = owners.get(room)
        if member is None:
            continue
        real = str(info.get("room") or room)
        items.append(
            {
                "uid": member.uid,
                "name": member.display_name,
                # room = 成员填的那个（可能只是短号），roomId = B站 认的真实房间号
                "room": room,
                "roomId": real,
                "shortId": str(info.get("shortId") or ""),
                "uname": str(info.get("uname") or ""),
                "title": str(info.get("title") or member.room_title or ""),
                "online": int(info.get("online") or 0),
                "area": str(info.get("area") or ""),
                "liveTime": str(info.get("liveTime") or ""),
                "embed": bili_embed_url(real),
                "jump": bili_jump_url(real),
            }
        )
    items.sort(key=lambda item: str(item["name"]))
    snap = bili_snapshot()
    return {"known": bool(snap["known"]), "reason": str(snap["reason"]), "items": items}


async def bili_check(room: str) -> str:
    """填完房间号**当场验一次**，返回给人看的一句话（空串 = 不用提示）。

    只做提示、**不拦保存**：B站 接口可能不可达或被风控，因为第三方抖动而不让保存
    房间号是本末倒置——填错的人至少能立刻看到「查不到这个直播间」，而不是等到开播那天
    才发现这里一直不出画面。

    顺带把缓存刷成最新的：刚保存就要在直播页看到它，没必要再等一轮后台探测。
    """
    if not room:
        return ""
    await bili_probe(force=True)
    snap = bili_snapshot()
    if room in (snap.get("missing") or {}):
        return f"B站 查不到直播间 {room}：房间号可能写错了（也可以直接粘直播间链接）"
    info = snap["rooms"].get(room)
    if not snap["known"] or info is None:
        return (
            f"暂时无法确认 B站 直播间 {room}（{snap['reason'] or '接口没给出确定答案'}）："
            "房间号填对了就会在开播后自动出现，稍后可在直播页「刷新信号」再看"
        )
    if info.get("live"):
        title = info.get("title") or "未填标题"
        return f"B站 直播间 {room} 正在直播：《{title}》——标题与在线人数会自动同步"
    title = info.get("title") or "暂无"
    return f"B站 直播间 {room} 已确认存在（当前未开播，上次标题：{title}）"


# 直播各路的称呼（群里 / 推送里展示用）
LIVE_KIND_LABEL = {
    "main": "主直播间",
    "player": "选手机位",
    "member": "成员直播间",
    "channel": "成员频道",
    "bili": "B站直播",
}
# 同一个流名常常同时对应「选手 / 成员 / 频道」（本来就常是同一个人），
# 展示时只留信息最全的那一层：选手带比赛上下文，其次成员，最后频道。
_LIVE_KIND_RANK = {"channel": 1, "member": 2, "player": 3}


async def collect_live(*, force: bool = True) -> dict[str, Any]:
    """汇总「当前谁在直播」：主直播间 + 选手机位 + 成员直播间 + 成员频道。

    与页面请求不同，**这里会真的探一次**（``force=True``）：查询 API / 推送预览都是
    「有人此刻想知道」才触发的，值得等那最多 3 秒——单飞锁保证并发也只打一次媒体
    服务器（见 :func:`ready_paths`）；页面每帧都要渲染，所以只能读缓存。

    返回的 ``items`` 已按「主直播间 → 选手机位 → 成员直播间 → 成员频道」排好，
    同一个流名只出现一次。查不到状态时 ``known=False``，并把原因放在 ``reason``。
    """
    cfg = store.snapshot()
    ready = await ready_paths(max_age=0.0) if force else ready_paths_snapshot()
    known = ready is not None
    keys = ready or set()
    status = await live_status_view()
    main_key = main_stream_key()

    entries: dict[str, dict[str, Any]] = {}

    def put(stream_key: str, kind: str, name: str, title: str = "", note: str = "") -> None:
        key = logic.clean_key(stream_key)
        if not key or key not in keys:
            return
        old = entries.get(key)
        if old is not None and _LIVE_KIND_RANK.get(str(old.get("kind")), 0) >= _LIVE_KIND_RANK[kind]:
            return
        entries[key] = {
            "key": key,
            "kind": kind,
            "name": name or key,
            "title": title or "",
            "note": note or "",
            "play": logic.play_endpoints(cfg.stream, key),
        }

    for channel in store.channels():
        # 停用的频道在访客端本来就不展示，这里也不列（否则停用后还会出现在群里）
        if channel.active:
            put(channel.stream_key, "channel", channel.display_name, channel.title)
    for member in store.members():
        if member.active:
            put(member.stream_id, "member", member.display_name, member.room_title)
    # 选手机位：顺带带上他当前所在的对局，群里就不用再查一次
    rounds = logic.player_round_map(cfg)
    for player in cfg.players:
        rnd = rounds.get(player.id)
        put(
            logic.player_stream_key(player),
            "player",
            player.display_name,
            note=(rnd.label or rnd.code) if rnd is not None else "",
        )

    # B站直播：成员填了房间号且在播时，多出一路（画面直嵌 B站 官方播放器，不经本站）
    for item in bili_view()["items"]:
        entries[f"bili:{item['uid']}"] = {
            "key": f"bili:{item['uid']}",
            "kind": "bili",
            "name": item["name"],
            "title": item["title"],
            "note": "B站直播",
            "memberUid": item["uid"],
            "bili": {
                "room": item["room"],
                "roomId": item["roomId"],
                "uname": item["uname"],
                "title": item["title"],
                "online": item["online"],
                "area": item.get("area", ""),
                "liveTime": item.get("liveTime", ""),
                "embed": item["embed"],
                "jump": item["jump"],
            },
            # 这一路没有本站的播放地址：看的是 B站 自己的播放器
            "play": {},
        }

    order = {"main": 0, "bili": 1, "player": 2, "member": 3, "channel": 4}
    items = sorted(entries.values(), key=lambda e: (order.get(str(e["kind"]), 9), str(e["name"])))
    main_live = bool(main_key) and main_key in keys
    main = {
        "key": main_key,
        "live": main_live,
        "play": logic.play_endpoints(cfg.stream, main_key) if main_key else {},
    }
    # 主直播间不是「某个人」，但同样是可观看到的独立一路：没和别人重名就插到最前
    if main_live and main_key not in entries:
        items.insert(0, {**main, "kind": "main", "name": "主直播间", "title": "", "note": ""})
    return {
        "known": known,
        "reason": "" if known else (status.get("reason") or "媒体服务器不可达，无法判断谁在推流"),
        # 直播没有总开关：只要有赛事就可能有人在推流（见 models.StreamConfig.enabled）
        "enabled": True,
        "main": main,
        "items": items,
        "total": len(items),
        "streamingPaths": status.get("count") or 0,
    }


# --------------------------------------------------------------------------- #
# 端口探测与后台刷新
# --------------------------------------------------------------------------- #
async def probe_ports(force: bool = False) -> dict[str, dict[str, Any]]:
    """探测 WebRTC / HLS 两个端口是否可达（``force=False`` 时优先用缓存）。

    MediaMTX 的 HLS 与 WebRTC 是两个独立端口，任一地址配错都会播不出来，
    所以分开回报，便于直接看出要改哪一个。``status=404`` 表示端口通、
    只是当前没有这个流，属于正常。

    两个端口**并发**探（串行会让失败路径耗时翻倍）。
    """
    endpoints = stream_endpoints()
    cached = _health_cache["probes"]
    if cached is not None and not force and time.monotonic() - _health_cache["at"] < _PROBE_TTL:
        return cached
    verify = bool(endpoints["verifyTls"])

    async def one(name: str, url: str) -> tuple[str, dict[str, Any]]:
        if not url:
            return name, {"ok": False, "reason": "未配置该地址"}
        try:
            resp = await _client_get(verify).get(url, timeout=_PROBE_TIMEOUT)
            return name, {"ok": resp.status_code < 500, "status": resp.status_code, "url": url}
        except httpx.HTTPError as exc:
            return name, {"ok": False, "reason": _friendly_error(exc), "url": url}

    targets = (
        ("webrtc", endpoints["originWebrtc"]),
        # HLS 只探**服务根地址**：去请求任何具体流路径都会让媒体服务器为那个路径
        # 建一个 HLS 会话，没人直播时就会不停刷 "no stream is available on path '…'"。
        # 根地址一样能验证端口与证书（返回 404 也算端口通）。
        ("hls", endpoints["originHlsRoot"]),
    )
    probes = dict(await asyncio.gather(*(one(name, url) for name, url in targets)))
    _health_cache.update({"at": time.monotonic(), "probes": probes})
    log.debug("直播源端口探测 | %s", probes)
    return probes


def probe_ports_snapshot() -> dict[str, dict[str, Any]]:
    """**只读缓存**的端口探测结果；还没探过就是空字典（绝不等待网络）。"""
    return _health_cache["probes"] or {}


async def _refresh_once() -> None:
    """把「谁在推流」、端口可达性与 B站 开播状态各刷新一次。

    异常一律吞掉（只记日志）：这是后台任务，挂掉不会再有人来重启它。

    这里存的是**函数**而不是协程对象：协程一旦构造出来就必须被 await，
    中间任何一步抛出 ``CancelledError``（关站时会）都会让后面的协程**从未被执行**，
    于是每个请求都留下一条 "coroutine was never awaited" 的警告。
    """
    for label, factory in (
        ("推流状态", lambda: ready_paths(max_age=0.0)),
        ("源端口", probe_ports),
        # B站 开播状态：和上面两项并列刷，失败只记日志（它挂了不该影响媒体服务器那条线）
        ("B站直播状态", bili_probe),
    ):
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("直播后台探测异常 | %s", label)


def kick_refresh() -> None:
    """安排一次**后台**探测，立刻返回；结果供下一次请求读取。

    这是「不卡前端」的关键：请求处理方只管调用，绝不等待探测完成。
    幂等且带节流，所以前端轮询多频繁都不会把媒体服务器打爆。

    常驻探测（:func:`watch_loop`）跑起来之后，它本来就一直在刷新缓存，所以这里
    加了一条「**刚探过就别再探**」的短路：否则一位访客打开页面就会与常驻循环
    各探一次，白白多打一轮媒体服务器（探测本身很轻，但没必要翻倍）。
    """
    global _refresh_task, _last_refresh
    if _refresh_task is not None and not _refresh_task.done():
        return
    now = time.monotonic()
    if now - _last_refresh < _REFRESH_MIN_INTERVAL:
        return
    if now - _ready_cache["at"] < _REFRESH_MIN_INTERVAL:
        return  # 缓存刚更新过（常驻探测刚跑完）：这次不用再探
    _last_refresh = now
    _refresh_task = asyncio.create_task(_refresh_once())


# --------------------------------------------------------------------------- #
# 常驻探测：没人看直播也一直在探
# --------------------------------------------------------------------------- #
#: 常驻探测间隔（秒）：**有人在播 / 有人在线**时用它。
#: MediaMTX 的推流列表是本地 HTTP（毫秒级、几十字节），5 秒一次完全可以忽略；
#: 换来的是「刚开播最多 5 秒后，任何页面都能看到」。
WATCH_INTERVAL = 5.0
#: 空闲间隔：没人开播、**也没有客户端在线**时放宽到它。
#: 注意是「放宽」不是「停」——一停下，第一个访问者看到的又会是旧数据。
WATCH_IDLE_INTERVAL = 20.0
#: 单轮探测的超时兜底：媒体服务器半死不活时别把循环挂在这儿（正常远小于它）
WATCH_STEP_TIMEOUT = 12.0
#: 探测失败 / 超时后的重试间隔：**不能用空闲间隔去等**——那等于媒体服务器一抖，
#: 我们就瞎 20 秒（这段时间里开播 / 下播谁都看不见）。
WATCH_RETRY = 2.0

_watch_task: asyncio.Task[None] | None = None


def live_fingerprint(view: dict[str, Any]) -> tuple[Any, ...]:
    """直播状态的指纹：**只有真的变了才广播**。

    取的字段与前端「谁算在播」用的那一组完全对应（见 ``static/js/live.js`` 的
    ``applyLiveHealth``）。刻意**不含**标题 / 在线人数这类细节：它们变了不值得让
    前端重绘一遍视图（B站 标题走的是另一条数据流）。
    """
    bili = view.get("bili") or {}
    return (
        bool(view.get("pending")),
        bool(view.get("streamingKnown")),
        bool(view.get("mainStreaming")),
        tuple(sorted(view.get("streaming") or ())),
        tuple(sorted(view.get("streamingChannels") or ())),
        tuple(sorted(view.get("streamingMembers") or ())),
        tuple(sorted(str(item.get("uid") or "") for item in (bili.get("items") or ()))),
    )


def watch_delay(view: dict[str, Any] | None = None) -> float:
    """下一轮探测该隔多久（纯函数，方便单测）。

    * **有人在播** → 勤一点（``WATCH_INTERVAL``）；
    * 没人播但**有客户端在线**（某个页面开着，可能在等开播）→ 还是勤一点；
    * 没人播、也没人在线 → 放宽到 ``WATCH_IDLE_INTERVAL``。
    """
    if view is not None:
        bili = view.get("bili") or {}
        busy = bool(
            view.get("mainStreaming")
            or view.get("streaming")
            or view.get("streamingChannels")
            or view.get("streamingMembers")
            or bili.get("items")
        )
        if busy:
            return WATCH_INTERVAL
    if hub.size == 0:
        return WATCH_IDLE_INTERVAL
    return WATCH_INTERVAL


async def watch_loop() -> None:
    """常驻探测循环：**没人访问也一直在探**，状态变化时通过 WebSocket 推给前端。

    为什么要有它：探测以前只在「前端打开直播 / 频道页」时才触发，于是没人看的时候
    缓存会过期（``_SNAPSHOT_MAX_AGE``），第一位访客看到的先是旧数据、还要等一轮才知道
    谁在播。现在缓存始终是新的，页面只做「读缓存 + 收到推送时重绘」。

    三件必须守住的事：

    * **不拖慢任何请求**：它跑在自己的任务里，请求路径只读缓存，谁也不等它；
    * **不打死媒体服务器**：间隔按需放大（见 :func:`watch_delay`），单轮还有超时兜底；
    * **崩了也继续**：任何异常只记日志，循环继续下一轮——它挂掉不会有第二个进程来救。

    广播的内容与 ``GET /api/live/health`` 完全一致，前端两条路径（轮询 / 推送）共用
    同一段解析逻辑，不会出现「推送说在播、轮询说没播」的抖动。
    """
    key: tuple[Any, ...] | None = None
    log.info(
        "直播常驻探测开始 | 间隔 %.0f 秒 / 空闲 %.0f 秒", WATCH_INTERVAL, WATCH_IDLE_INTERVAL
    )
    while True:
        delay = WATCH_IDLE_INTERVAL
        try:
            await asyncio.wait_for(_refresh_once(), timeout=WATCH_STEP_TIMEOUT)
            view = await health_view(force=False)
            delay = watch_delay(view)
            current = live_fingerprint(view)
            if current != key:
                key = current
                await hub.broadcast({"type": "live", "data": view})
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("直播探测超时（跳过本轮）| 超过 %.0f 秒", WATCH_STEP_TIMEOUT)
            delay = WATCH_RETRY
        except Exception:
            # 常驻任务：任何异常都只记日志，循环必须活下去（挂了不会有第二个进程来救）
            log.exception("直播常驻探测异常（继续下一轮）")
            delay = WATCH_RETRY
        await asyncio.sleep(max(0.2, delay))


def start_watcher() -> None:
    """启动常驻探测（幂等；由启动流程调用，不要在导入期自动拉起）。"""
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        return
    _watch_task = asyncio.create_task(watch_loop())
    log.info("直播常驻探测任务已启动")


async def stop_refresher() -> None:
    """取消常驻探测与尚未跑完的按需探测（关闭 HTTP 连接池之前调用）。"""
    global _refresh_task, _watch_task
    watch, _watch_task = _watch_task, None
    task, _refresh_task = _refresh_task, None
    for item in (watch, task):
        if item is None or item.done():
            continue
        item.cancel()
        try:
            await item
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- #
# 推流白名单（MediaMTX ``authHTTPAddress`` 回调）
#
# 背景：媒体服务器的推流与播放**路径同名**（同一个 ``/<流名>``，
# 方向由客户端行为决定），因此仅靠路径保密挡不住他人推流；
# 而本站在直播页下方又是公开播放地址。
# 打开下面的 HTTP 鉴权后，媒体服务器每次推流都会来问一次本站：
# 只有**在本站登记过流名**的人（选手 / 成员频道 / 主直播间默认流名）才放行。
#
# 媒体服务器侧配置（``mediamtx.yml``）：
#
#     authMethod: http
#     authHTTPAddress: https://<本站地址>/api/live/auth
#     authHTTPExclude:            # 播放不回调，避免每个观众都打一次本站
#       - action: read
#       - action: playback
#
# 约定（见 MediaMTX Authentication 文档）：返回 20x = 允许，其它状态码 = 拒绝。
# --------------------------------------------------------------------------- #
class LiveAuthPayload(NTEModel):
    """媒体服务器鉴权回调的请求体。"""

    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str = ""          # publish / read / playback / api / metrics / pprof
    path: str = ""
    protocol: str = ""        # rtsp / rtmp / hls / webrtc / srt
    id: str = ""
    query: str = ""
    user_agent: str = ""

    @model_validator(mode="before")
    @classmethod
    def _null_to_empty(cls, data: Any) -> Any:
        """媒体服务器可能把未提供的字段发成 ``null``；不能因此回 422 误拒推流。"""
        if isinstance(data, dict):
            return {key: ("" if value is None else value) for key, value in data.items()}
        return data


def registered_push_keys() -> set[str]:
    """本站登记过的流名（**遗留**白名单）：选手 + 成员频道 + 主直播间默认流名。

    成员的推流走「推流 ID + Bearer 令牌」强校验（见 :func:`authorize_publish`），
    不在这里；这个集合只兜住主直播间与服务器管理员手工建的频道 / 选手。
    """
    cfg = store.snapshot()
    keys: set[str] = set()
    for player in cfg.players:
        key = logic.clean_key(player.stream_key)
        if key:
            keys.add(key)
    for channel in store.channels():
        key = logic.clean_key(channel.stream_key)
        if key:
            keys.add(key)
    main = logic.clean_key(cfg.stream.stream_key)
    if main:
        keys.add(main)
    return keys


def _publish_token(payload: LiveAuthPayload) -> str:
    """从鉴权回调里取推流令牌。

    WHIP 客户端（OBS）把凭据放在地址里（``…/whip?token=xxx``）或 Basic 认证
    里，MediaMTX 会分别透传到 ``token`` / ``password`` / ``query`` 字段；
    为兼容各种写法，这里依次尝试。
    """
    for value in (payload.token, payload.password):
        raw = (value or "").strip()
        if raw:
            return raw
    query = (payload.query or "").strip()
    if query:
        try:
            params = parse_qs(query)
            for name in ("token", "bearer", "access_token", "key"):
                if params.get(name):
                    return str(params[name][0]).strip()
        except (ValueError, TypeError):
            log.debug("推流令牌 query 解析失败（忽略）| query=%s", query)
    return ""


def active_ban_for(key: str) -> LiveBan | None:
    """某条推流当前生效的封禁（按流名或成员命中；全局 + 赛事级都算）。"""
    if not key:
        return None
    member = store.member_by_stream_id(key)
    for ban in store.live_bans():
        if not logic.ban_active(ban):
            continue
        hit_stream = bool(ban.stream_id) and ban.stream_id == key
        hit_uid = bool(ban.member_uid) and member is not None and ban.member_uid == member.uid
        if not (hit_stream or hit_uid):
            continue
        log.debug("命中封禁 | ban=%s | key=%s | 至=%s", ban.id, key, ban.until or "永久")
        return ban
    return None


def authorize_publish(key: str, token: str) -> tuple[bool, str]:
    """推流授权：**推流 ID + Bearer 令牌**同时正确才放行。

    成员（含其关联的选手）必须令牌匹配；主直播间与服务器管理员手工建的
    遗留频道走登记白名单（无令牌）。封禁期间一律拒绝。
    """
    key = logic.clean_key(key)
    if not key:
        return False, "空的推流路径"
    ban = active_ban_for(key)
    if ban is not None:
        return False, f"该直播间已被封禁（{ban.reason or '未填写原因'}）"
    member = store.member_by_stream_id(key)
    if member is not None:
        if not member.active:
            return False, "该成员已被停用"
        if not member.bearer_stored:
            return False, "该成员未配置推流令牌"
        # 逐条随机盐，必须按该成员的存储值校验；比较是常量时间的
        if not token or not verify_secret(token, member.bearer_stored):
            return False, "Bearer 令牌不正确"
        return True, ""
    if key in registered_push_keys():
        # 主直播间 / 服务器管理员手工建的遗留频道：这类流名不属于任何成员，所以没得
        # 「按成员认人」。配了「推流令牌」就要令牌；**留空则保持旧的白名单放行**
        #（向后兼容：不配也能照旧推）。
        expected = (store.snapshot().stream.push_token or "").strip()
        if not expected:
            return True, ""
        # 按 UTF-8 **字节**比：compare_digest 对含非 ASCII 的 str 会直接抛 TypeError
        #（用户随手填个中文令牌就把鉴权接口打成 500）——auth._same 里踩过同一个坑。
        if not token or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return False, "该流名需要「推流令牌」（在直播配置里设置的那个）"
        return True, ""
    return False, "该推流流名未在本站登记"


@router.post("/auth")
async def live_auth(payload: LiveAuthPayload) -> dict[str, Any]:
    """媒体服务器鉴权回调：**推流必须带正确的推流 ID 与 Bearer 令牌**，播放一律放行。

    * ``publish``：成员推流要求「推流 ID = 他的 streamId」且 Bearer 令牌匹配
      （令牌在地址 ``?token=`` 或 Basic 认证里带），并且未处于封禁期；
      主直播间与遗留频道按登记白名单放行。任一不满足即回 401；
    * ``api`` ：控制 API 的 Basic 认证按「直播配置」里的用户名 / 密码校验
      （没填就放行，与以前一致）；
    * ``read`` / ``playback``：播放放行（建议在媒体服务器侧用
      ``authHTTPExclude`` 直接排除，不必回调）；
    * 其余动作（``metrics`` / ``pprof``）放行。
    """
    action = (payload.action or "").strip().lower()
    path = logic.clean_key(payload.path)
    if action == "publish":
        token = _publish_token(payload)
        ok, reason = authorize_publish(path, token)
        if ok:
            log.info(
                "推流鉴权通过 | path=%s | protocol=%s | ip=%s | 带令牌=%s",
                path,
                payload.protocol,
                payload.ip,
                bool(token),
            )
            return {"ok": True, "action": action, "path": path}
        log.warning(
            "推流鉴权拒绝 | path=%s | 原因=%s | protocol=%s | ip=%s | user=%s",
            payload.path,
            reason,
            payload.protocol,
            payload.ip,
            payload.user,
        )
        raise HTTPException(status_code=401, detail=reason)
    if action == "api":
        stream = store.snapshot().stream
        expected = (stream.api_user or "").strip()
        # 没配控制 API 账号时保持原行为（放行），配了才校验
        if not expected:
            return {"ok": True, "action": action}
        if payload.user == expected and payload.password == (stream.api_pass or ""):
            return {"ok": True, "action": action}
        log.warning("控制 API 鉴权拒绝 | user=%s | ip=%s", payload.user, payload.ip)
        raise HTTPException(status_code=401, detail="控制 API 用户名 / 密码不正确")
    return {"ok": True, "action": action, "path": path}


async def kick_stream(stream_key: str) -> dict[str, Any]:
    """强制掐断一条正在推流的 WHIP 会话（服务器管理员 / 赛事管理员的「掐断直播」）。

    实现：查 MediaMTX 的 WebRTC 会话列表，找到该路径**推流端**（``state=publish``）
    的会话并调 ``kick``。查不到（未配置 API / 不可达 / 已停播）时返回 ``ok=False``，
    但封禁记录仍会生效——下次该令牌再来推流一样会被鉴权拒绝。
    """
    key = logic.clean_key(stream_key)
    if not key:
        return {"ok": False, "kicked": 0, "reason": "未指定推流 ID"}
    cfg = store.snapshot().stream
    api = (cfg.api_base or "").strip().rstrip("/")
    if not api:
        return {"ok": False, "kicked": 0, "reason": "未配置 MediaMTX API 地址，无法远程掐断"}
    client = _client_get(bool(cfg.verify_tls))
    try:
        resp = await client.get(
            f"{api}/v3/webrtcsessions/list", timeout=_READY_TIMEOUT, auth=api_auth(cfg)
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("items") or []
    except httpx.HTTPStatusError as exc:
        reason = f"MediaMTX API 返回 HTTP {exc.response.status_code}"
        log.warning("掐断直播失败 | key=%s | %s", key, reason)
        return {"ok": False, "kicked": 0, "reason": reason}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        reason = _friendly_error(exc)
        log.warning("掐断直播失败 | key=%s | %s", key, reason)
        return {"ok": False, "kicked": 0, "reason": reason}

    sessions = [item for item in items if logic.clean_key(str(item.get("path") or "")) == key]
    # 只踢**推流端**（state=publish）：踢掉观看会话会把观众断掉，反而帮了违规者
    publishers = [item for item in sessions if str(item.get("state") or "") == "publish"]
    kicked = 0
    for item in publishers:
        sid = str(item.get("id") or "").strip()
        if not sid:
            continue
        try:
            await client.post(
                f"{api}/v3/webrtcsessions/{sid}/kick", timeout=_READY_TIMEOUT, auth=api_auth(cfg)
            )
            kicked += 1
        except httpx.HTTPError as exc:
            log.warning("掐断会话失败 | key=%s | sid=%s | %s", key, sid, _friendly_error(exc))
    if kicked:
        # 掐断后让「谁在推流」的缓存立即失效，前端下次刷新就看不到直播中
        _ready_cache["at"] = float("-inf")
        kick_refresh()
        log.warning("已强制掐断直播 | key=%s | 会话=%d", key, kicked)
    if kicked:
        reason = ""
    elif sessions:
        reason = "未能定位推流会话（媒体服务器版本不支持或状态未知）"
    else:
        reason = "当前没有该路径的推流会话（可能已经停播）"
    return {"ok": kicked > 0, "kicked": kicked, "reason": reason}


def main_stream_key() -> str:
    """主直播间的流名：``直播配置 → 默认流名``（默认 ``stream``）。

    它不是某位选手的机位，而是「全场总机位」——谁在推这个流名，
    谁就是主直播间；一个人都没推、只有它在推时，直播页只给出它这一路。
    """
    return logic.clean_key(store.snapshot().stream.stream_key) or "stream"


async def main_stream_ready() -> bool | None:
    """主直播间当前有没有人在推流；``None`` = 查不到（API 未配置 / 不可达 / 还没探到）。

    跟选手机位同一套判断：看媒体服务器上报的 ready 列表里有没有这个流名。
    只读后台缓存，**不发网络请求**。
    """
    ready = ready_paths_snapshot()
    if ready is None:
        return None
    return main_stream_key() in ready


async def live_status_view() -> dict[str, Any]:
    """「谁在推流」这份数据的状态（供前端说明为什么没有直播中标记）。

    只读后台缓存：接口本身不等待媒体服务器。
    """
    cfg = store.snapshot()
    api = (cfg.stream.api_base or "").strip()
    ready = ready_paths_snapshot() if api else None
    if ready is not None:
        reason = ""
    elif not api:
        reason = "未配置 MediaMTX API 地址，无法判断谁在推流"
    elif not _ready_cache["attempted"]:
        reason = "尚未检测：打开「直播」页后会自动检测推流状态"
    elif _ready_cache["paths"] is not None:
        # 上次探测是成功的，只是缓存过期了（离开直播页后不再刷新）
        reason = "推流状态已过期：离开直播页后不再检测"
    else:
        # 把上次失败的真实原因带出去（鉴权失败 / 超时 / 端口不通…）
        reason = _ready_cache.get("reason") or "MediaMTX API 不可达，无法判断谁在推流"
    return {
        "known": ready is not None,
        "count": len(ready) if ready else 0,
        "apiConfigured": bool(api),
        "reason": reason,
        # B站 直播（另一条完全独立的链路）：只读缓存，前端据此多出 B站 机位
        "bili": bili_view(),
    }


def stream_endpoints() -> dict[str, Any]:
    """公开的源地址集合（**只有观看地址**，推流地址属于凭据，仅在管理端出现）。

    ``key`` 是**主直播间的流名**（``直播配置 → 默认流名``）。它本来就写在
    下面这些观看地址里（``…/<流名>``），所以不算额外泄露；
    前端用它把「主直播间」当成一路独立机位来播放与切换。
    """
    cfg = store.snapshot().stream
    base = (cfg.base_url or "").rstrip("/")
    hls = (cfg.hls_base or "").rstrip("/")
    key = (cfg.stream_key or "").strip("/") or "stream"
    return {
        # 直播没有总开关（见 models.StreamConfig.enabled），留着这个键是为了兼容前端读法
        "enabled": True,
        "mode": cfg.mode,
        "key": key,
        # 源站是否 HTTPS：HTTPS 站点上只能连 HTTPS 源，否则会被按混合内容拦掉
        "secure": base.startswith("https://"),
        "verifyTls": bool(cfg.verify_tls),
        "origin": base,
        # 观看地址就两条：8889（WebRTC）与 8888（HLS）
        "originWebrtc": f"{base}/{key}" if base else "",
        "originHls": f"{hls}/{key}" if hls else "",
        # HLS 服务的**根地址**：端口探测专用。探测不去碰任何具体流路径——那会让媒体
        # 服务器为该路径创建一个 HLS 会话，没人直播时日志会被刷屏（见 probe_ports）。
        "originHlsRoot": f"{hls}/" if hls else "",
        # 观众可切换的两种播放线路（都只是播放，不含推流凭据）
        "protocols": [
            {"id": "webrtc", "label": "WebRTC", "note": "延迟最低（UDP）"},
            {"id": "hls", "label": "HLS", "note": "抗抖动（TCP，延迟略高）"},
        ],
    }


@router.get("/info")
async def live_info() -> dict[str, Any]:
    """公开的直播信息：**不含任何推流地址**（管理端请用 /api/private）。"""
    return {
        "config": logic.public_stream_config(store.snapshot().stream),
        "endpoints": stream_endpoints(),
    }


def _friendly_error(exc: Exception) -> str:
    """把底层网络异常翻译成管理员能直接照做的说明。

    HTTPS 化之后最容易踩的三件事：证书不被信任、scheme 与端口不匹配、
    端口没开——只回一句底层英文报错，排查起来全靠猜。

    匹配时把**异常类名**也算进去：有些连接类异常（如
    ``RemoteProtocolError``）的消息是空串，只看消息会得到一句空话。
    """
    name = type(exc).__name__
    text = str(exc).strip()
    found = f"{name} {text}".lower()
    if "certificate" in found or "sslcertverification" in found or "self-signed" in found:
        return (
            "源站证书校验失败：自签名证书可在「直播配置」里关闭「校验上游证书」"
            "（仅影响这里的探测；观众侧仍会被浏览器拦，请换成受信任证书）"
        )
    if (
        "wrong version number" in found
        or "record layer" in found
        or "unexpected eof" in found
        or "remote protocol" in found
        or "disconnect" in found
    ):
        return (
            "协议不匹配或连接被中断：多半是用 https:// 访问到了明文端口（或反之），"
            "请确认该端口已开启 TLS（webrtcEncryption / hlsEncryption）且 scheme 与端口一致"
        )
    if "name or service not known" in found or "getaddrinfo" in found or "nodename" in found:
        return "域名解析失败：请检查「直播配置」里的地址是否写对"
    if "timed out" in found or "timeout" in found:
        return "连接超时：媒体服务器没有响应（端口未开 / 被防火墙拦截）"
    if "refused" in found:
        return "连接被拒绝：媒体服务器未启动，或端口写错"
    if text:
        return text
    return f"源站不可达（{name}）"


async def health_view(force: bool = False) -> dict[str, Any]:
    """组装「直播链路健康」视图（接口、常驻探测与 WebSocket 推送共用同一份）。

    它就是前端的**唯一数据源**：轮询拉的是它，常驻探测推的也是它——所以「谁算在播」
    两边口径完全一致（服务端拿 :func:`live_fingerprint` 判断要不要推，前端拿
    ``applyLiveHealth`` 判断要不要重绘，两组字段刻意对齐）。

    ``force=False``（默认）**只读缓存**（外加一次可能被跳过的后台催更），本身不等待网络——
    这个接口会被前端轮询，绝不能因为媒体服务器不可达而挂住。
    """
    endpoints = stream_endpoints()
    if force:
        # 显式刷新：现场探一次（可能等到超时，但这是用户主动要求的）
        await ready_paths(max_age=0.0)
        probes = await probe_ports(force=True)
    else:
        # 只读缓存 + 安排一次后台刷新：本次先返回手里的值，新结果下次轮询生效。
        # 冷启动时 probes 是空的，用 pending 告诉前端「后台正在探」，别当成探测失败。
        kick_refresh()
        probes = probe_ports_snapshot()
    pending = not probes
    verify = bool(endpoints["verifyTls"])
    cfg = store.snapshot()
    api = (cfg.stream.api_base or "").strip()
    ready = ready_paths_snapshot() if api else None
    known = ready is not None
    api_reason = ""
    if not known:
        if not api:
            api_reason = "未配置 MediaMTX API 地址（默认 :9997），无法判断谁在推流"
        elif not _ready_cache["attempted"]:
            api_reason = "尚未检测：正在向媒体服务器查询推流状态"
        elif _ready_cache["paths"] is not None:
            api_reason = "推流状态已过期：后台探测没有更新成功（检查日志里的探测原因）"
        else:
            api_reason = _ready_cache.get("reason") or "MediaMTX API 不可达：请确认 api: yes 且端口已开放"
    out: dict[str, Any] = {
        # 以 WebRTC/HTTP 端口为准判断「信号就绪」
        "ok": bool(probes.get("webrtc", {}).get("ok")),
        "origin": endpoints["origin"],
        "secure": endpoints["secure"],
        "verifyTls": verify,
        "probes": probes,
        # 端口还在后台探测中（冷启动）：前端据此缩短下一次轮询，别显示成「探测失败」
        "pending": pending,
        # 正在推流的机位（选手 ID）：前端用它决定要不要显示「直播中」
        "streamingKnown": known,
        "streaming": await streaming_player_ids(cfg) if known else [],
        # 成员频道（日常直播）里正在推流的频道 ID：与选手机位同一套判断
        "streamingChannels": await streaming_channel_ids() if known else [],
        # 成员直播间（推流 ID 对应成员）里正在推流的成员 uid
        "streamingMembers": await streaming_member_uids() if known else [],
        # 主直播间（默认流名）是否有人在推：只有真的在推，前端才给出这一路信号
        "mainStreaming": bool(ready) and main_stream_key() in (ready or set()),
        "api": {"configured": bool(api), "ok": known, "url": api, "reason": api_reason},
        # B站 直播：与媒体服务器那套**完全独立**（成员填了房间号才有），
        # 只读缓存；在播的那些前端会多出「B站直播」机位
        "bili": bili_view(),
    }
    if pending:
        out["reason"] = "正在检测源站端口…"
    elif not out["ok"]:
        out["reason"] = probes.get("webrtc", {}).get("reason") or "直播源不可达"
    return out


@router.get("/health")
async def live_health(probe: bool = Query(default=False)) -> dict[str, Any]:
    """直播链路健康视图：**分别**看 WebRTC(HTTP) 端口与 HLS 端口是否可达。

    MediaMTX 的 HLS 与 WebRTC 是两个独立端口（8888 / 8889），
    任一地址配错都会播不出来，所以分开回报，便于直接看出要改哪一个。
    ``status=404`` 表示端口通、只是当前没有这个流，属于正常。

    默认**只读后台缓存**（毫秒级返回）；``?probe=1`` 才现场重新探测一次，
    供管理端「刷新信号」按钮显式调用。
    """
    return await health_view(force=probe)
