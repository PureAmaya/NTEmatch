"""直播信息与源站探测。

本模块**不做反代**：前端拿到的就是媒体服务器（MediaMTX）的**源地址**，
直接去 ``https://<媒体服务器>:8889/<流名>/whep`` 拉 WebRTC、
``https://<媒体服务器>:8888/<流名>/index.m3u8`` 拉 HLS。

因此有两件事需要媒体服务器侧配合（都在 README 里写明）：

* **跨域**：WHEP 是浏览器直接 fetch 到另一个源，MediaMTX 默认会回
  ``Access-Control-Allow-Origin: *``，无需额外配置；
* **证书**：站点是 HTTPS 时浏览器不允许混用 ``http://`` 源（混合内容会被拦），
  所以源地址也要用 HTTPS 且证书要受浏览器信任——自签名证书只有服务端探测
  可以关掉校验（``verifyTls``），观众侧仍会被浏览器拦。

MediaMTX 的 HLS 与 WebRTC 是**两个独立端口**（``8888`` / ``8889``），
``/health`` 会分别探测并回报，地址写错时能直接看出要改哪一个。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import model_validator

from . import logic
from .logging_conf import get_logger
from .models import Config, NTEModel, StreamConfig
from .store import store

log = get_logger("live")

router = APIRouter(prefix="/api/live", tags=["live"])

# 校验证书 / 不校验证书各一个连接池（证书校验只能在创建客户端时指定）
_clients: dict[bool, httpx.AsyncClient] = {}


def _client_get(verify: bool = True) -> httpx.AsyncClient:
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
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()


# =========================================================================== #
# 后台探测：**请求处理路径永不等待媒体服务器**
#
# 媒体服务器（MediaMTX）不可达时，一次探测要一直等到超时才返回。以前
# ``/api/state`` 会同步等 4 次这样的探测（主直播间 / 选手 / 成员频道 / 状态视图），
# 媒体服务器没开时首屏就要卡十几秒 —— 而且只缓存成功结果，失败还会反复重试。
#
# 现在的分工：
#   * 一个后台任务按固定间隔把「谁在推流」与「端口是否可达」问一遍，写进缓存；
#   * ``/api/state`` 与 ``/api/live/health`` **只读缓存**，因此永远毫秒级返回；
#   * 只有显式 ``probe=1``（管理端「刷新信号」）才现场探测。
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
# 端口探测（WebRTC / HLS 两个端口）的最近结果，同样由后台任务刷新
_health_cache: dict[str, Any] = {"at": float("-inf"), "probes": None}

_READY_TTL_OK = 8.0        # 探测成功的结果可复用多久
_READY_TTL_FAIL = 5.0      # 探测失败的结果可复用多久
_READY_TIMEOUT = 3.0       # 控制 API 单次探测超时
_PROBE_TIMEOUT = 4.0       # 端口探测单次超时
_PROBE_TTL = 15.0          # 端口探测结果可复用多久（比控制 API 贵，刷新得慢一些）
# 两次真实探测之间的最小间隔：即使调用方要求「强制刷新」，短时间内的并发请求也复用
# 同一次结果。否则多人同时点「刷新信号」会在锁上串行排队（N 个客户端 × 超时）。
_READY_MIN_INTERVAL = 0.5

# 后台探测间隔。控制 API 每次都会探；端口探测走 _PROBE_TTL 节流。
_PROBE_INTERVAL = 5.0
_probe_task: asyncio.Task | None = None

# 失败日志节流：后台每 5 秒探一次，媒体服务器长期不可达时不能每轮都刷同一条 warning。
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

    没探测过、或上次探测失败（媒体服务器不可达）时都是 ``None`` = 「查不到」。
    新鲜度由后台任务负责（见 :func:`start_prober`），请求处理方直接拿走即可。
    """
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
    if not endpoints["enabled"]:
        return {}
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

    targets = (("webrtc", endpoints["originPlayPage"]), ("hls", endpoints["originHls"]))
    probes = dict(await asyncio.gather(*(one(name, url) for name, url in targets)))
    _health_cache.update({"at": time.monotonic(), "probes": probes})
    log.debug("直播源端口探测 | %s", probes)
    return probes


async def _probe_loop() -> None:
    """后台探测循环：把最新结果写进缓存，供请求处理方零等待读取。"""
    while True:
        try:
            await ready_paths(max_age=0.0)
            await probe_ports()
        except asyncio.CancelledError:
            raise
        except Exception:  # 兜底：任何异常都不能让后台任务夭折
            log.exception("直播后台探测异常（将在下一轮重试）")
        await asyncio.sleep(_PROBE_INTERVAL)


def start_prober() -> None:
    """启动直播后台探测任务（幂等）。

    API 地址没配时会空转（``ready_paths`` 直接返回），几乎不产生开销。
    """
    global _probe_task
    if _probe_task is not None and not _probe_task.done():
        return
    _probe_task = asyncio.create_task(_probe_loop())
    log.info(
        "直播后台探测已启动 | 间隔=%.0fs | 控制 API=%s",
        _PROBE_INTERVAL,
        (store.snapshot().stream.api_base or "").strip() or "(未配置，跳过)",
    )


async def stop_prober() -> None:
    """停止直播后台探测任务（幂等）；在关闭 HTTP 连接池之前调用。"""
    global _probe_task
    task, _probe_task = _probe_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.debug("直播后台探测已停止")


# --------------------------------------------------------------------------- #
# 推流白名单（MediaMTX ``authHTTPAddress`` 回调）
#
# 背景：媒体服务器的推流与播放**路径同名**（RTMP / RTSP 甚至同址），
# 因此仅靠路径保密挡不住他人推流；而本站在直播页下方又是公开播放地址。
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
    """本站登记过的流名（允许推流的白名单）：选手 + 成员频道 + 主直播间默认流名。"""
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


@router.post("/auth")
async def live_auth(payload: LiveAuthPayload) -> dict[str, Any]:
    """媒体服务器鉴权回调：**只放行本站登记过的推流，播放一律放行**。

    * ``publish``：推流路径必须在本站登记过（选手流名 / 成员频道流名 /
      主直播间默认流名），否则回 401 拒绝——这就是「不接受匿名推流」；
    * ``api`` ：控制 API 的 Basic 认证按「直播配置」里的用户名 / 密码校验
      （没填就放行，与以前一致）；
    * ``read`` / ``playback``：播放放行（建议在媒体服务器侧用
      ``authHTTPExclude`` 直接排除，不必回调）；
    * 其余动作（``metrics`` / ``pprof``）放行。
    """
    action = (payload.action or "").strip().lower()
    path = logic.clean_key(payload.path)
    if action == "publish":
        if path and path in registered_push_keys():
            log.info(
                "推流鉴权通过 | path=%s | protocol=%s | ip=%s", path, payload.protocol, payload.ip
            )
            return {"ok": True, "action": action, "path": path}
        log.warning(
            "推流鉴权拒绝（流名未在本站登记） | path=%s | protocol=%s | ip=%s | user=%s",
            payload.path,
            payload.protocol,
            payload.ip,
            payload.user,
        )
        raise HTTPException(status_code=401, detail="该推流流名未在本站登记，已拒绝推流")
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
        reason = "尚未探测到媒体服务器（后台正在重试）"
    else:
        # 把上次失败的真实原因带出去（鉴权失败 / 超时 / 端口不通…）
        reason = _ready_cache.get("reason") or "MediaMTX API 不可达，无法判断谁在推流"
    return {
        "known": ready is not None,
        "count": len(ready) if ready else 0,
        "apiConfigured": bool(api),
        "reason": reason,
    }


def stream_endpoints() -> dict[str, Any]:
    """公开的源地址集合（**只有播放地址**，推流地址属于凭据，仅在管理端出现）。

    ``key`` 是**主直播间的流名**（``直播配置 → 默认流名``）。它本来就写在
    下面这些播放地址里（``…/<流名>/whep``），所以不算额外泄露；
    前端用它把「主直播间」当成一路独立机位来播放与切换。
    """
    cfg = store.snapshot().stream
    base = (cfg.base_url or "").rstrip("/")
    hls = (cfg.hls_base or "").rstrip("/")
    key = (cfg.stream_key or "").strip("/") or "stream"
    return {
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "key": key,
        # 源站是否 HTTPS：HTTPS 站点上只能连 HTTPS 源，否则会被按混合内容拦掉
        "secure": base.startswith("https://"),
        "verifyTls": bool(cfg.verify_tls),
        "origin": base,
        "originPlayPage": f"{base}/{key}/" if base else "",
        "originHlsPage": f"{hls}/{key}/" if hls else "",
        "originWhep": f"{base}/{key}/whep" if base else "",
        "originHls": cfg.hls_url or (f"{hls}/{key}/index.m3u8" if hls else ""),
        # 观众可切换的两种播放线路（都只是播放，不含推流凭据）
        "protocols": [
            {"id": "webrtc", "label": "WebRTC", "note": "延迟最低（UDP）"},
            {"id": "tcp", "label": "HLS", "note": "抗抖动（TCP，延迟略高）"},
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
    """组装「直播链路健康」视图（接口与后台任务共用）。

    ``force=False``（默认）只读后台缓存，**不发任何网络请求**——
    这个接口会被前端每 15 秒轮询一次，绝不能因为媒体服务器不可达而挂住。
    """
    endpoints = stream_endpoints()
    if not endpoints["enabled"]:
        return {"ok": False, "reason": "disabled", "probes": {}}
    if force:
        # 显式刷新：现场重新问一次控制 API（可能等到超时，但这是用户主动要求的）
        await ready_paths(max_age=0.0)
    verify = bool(endpoints["verifyTls"])
    probes = await probe_ports(force=force)
    cfg = store.snapshot()
    api = (cfg.stream.api_base or "").strip()
    ready = ready_paths_snapshot() if api else None
    known = ready is not None
    api_reason = ""
    if not known:
        if not api:
            api_reason = "未配置 MediaMTX API 地址（默认 :9997），无法判断谁在推流"
        elif not _ready_cache["attempted"]:
            api_reason = "尚未探测到媒体服务器（后台正在重试）"
        else:
            api_reason = _ready_cache.get("reason") or "MediaMTX API 不可达：请确认 api: yes 且端口已开放"
    out: dict[str, Any] = {
        # 以 WebRTC/HTTP 端口为准判断「信号就绪」
        "ok": bool(probes.get("webrtc", {}).get("ok")),
        "origin": endpoints["origin"],
        "secure": endpoints["secure"],
        "verifyTls": verify,
        "probes": probes,
        # 正在推流的机位（选手 ID）：前端用它决定要不要显示「直播中」
        "streamingKnown": known,
        "streaming": await streaming_player_ids(cfg) if known else [],
        # 成员频道（日常直播）里正在推流的频道 ID：与选手机位同一套判断
        "streamingChannels": await streaming_channel_ids() if known else [],
        # 主直播间（默认流名）是否有人在推：只有真的在推，前端才给出这一路信号
        "mainStreaming": bool(ready) and main_stream_key() in (ready or set()),
        "api": {"configured": bool(api), "ok": known, "url": api, "reason": api_reason},
    }
    if not out["ok"]:
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
