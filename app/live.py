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

import time
from typing import Any

import httpx
from fastapi import APIRouter

from . import logic
from .logging_conf import get_logger
from .models import Config, StreamConfig
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


# 「谁真的在推流」结果缓存：MediaMTX 的 /v3/paths/list 不必每个请求都打一次
# ``reason`` 记下上一次失败的原因（如 401 鉴权失败），前端会把它原样显示出来
_ready_cache: dict[str, Any] = {"at": 0.0, "paths": None, "reason": ""}


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


async def ready_paths(max_age: float = 8.0) -> set[str] | None:
    """媒体服务器上报的**正在推流**路径集合；``None`` = 查不到（未配置 / 不可达）。

    MediaMTX 控制 API 的 ``GET /v3/paths/list`` 里每个 path 的 ``ready`` 表示
    「当前有推流端连着」。地址在「直播配置」里填（默认 ``:9997``）；
    媒体服务器开了 API 鉴权（mediamtx.yml 的 authInternalUsers）时，
    用户名 / 密码也在那里填，这里按 Basic 认证带上。
    """
    cfg = store.snapshot().stream
    api = (cfg.api_base or "").strip().rstrip("/")
    if not api:
        return None
    now = time.monotonic()
    cached = _ready_cache["paths"]
    if cached is not None and now - _ready_cache["at"] < max_age:
        return cached
    try:
        resp = await _client_get(bool(cfg.verify_tls)).get(
            f"{api}/v3/paths/list", timeout=5.0, auth=api_auth(cfg)
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
        log.warning("MediaMTX 推流状态查询失败 | %s", reason)
        _ready_cache.update({"at": now, "paths": None, "reason": reason})
        return None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.debug("MediaMTX 推流状态查询失败 | %s", exc)
        _ready_cache.update({"at": now, "paths": None, "reason": _friendly_error(exc)})
        return None
    _ready_cache.update({"at": now, "paths": paths, "reason": ""})
    log.debug("MediaMTX 上报正在推流 | %s", sorted(paths))
    return paths


async def streaming_player_ids(cfg: Config) -> list[str]:
    """**真的在推流**的选手：他的流名出现在媒体服务器的 ready 列表里。

    查不到（API 未配置 / 不可达）时返回空列表——宁可少显示「直播中」，
    也不要给观众一个假的直播标记。
    """
    ready = await ready_paths()
    if not ready:
        return []
    return [p.id for p in cfg.players if logic.clean_key(p.stream_key) in ready]


def main_stream_key() -> str:
    """主直播间的流名：``直播配置 → 默认流名``（默认 ``stream``）。

    它不是某位选手的机位，而是「全场总机位」——谁在推这个流名，
    谁就是主直播间；一个人都没推、只有它在推时，直播页只给出它这一路。
    """
    return logic.clean_key(store.snapshot().stream.stream_key) or "stream"


async def main_stream_ready() -> bool | None:
    """主直播间当前有没有人在推流；``None`` = 查不到（API 未配置 / 不可达）。

    跟选手机位同一套判断：看媒体服务器上报的 ready 列表里有没有这个流名。
    """
    ready = await ready_paths()
    if ready is None:
        return None
    return main_stream_key() in ready


async def live_status_view() -> dict[str, Any]:
    """「谁在推流」这份数据的状态（供前端说明为什么没有直播中标记）。"""
    cfg = store.snapshot()
    api = (cfg.stream.api_base or "").strip()
    ready = await ready_paths() if api else None
    if ready is not None:
        reason = ""
    elif not api:
        reason = "未配置 MediaMTX API 地址，无法判断谁在推流"
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


@router.get("/health")
async def live_health() -> dict[str, Any]:
    """探测源站：**分别**看 WebRTC(HTTP) 端口与 HLS 端口是否可达。

    MediaMTX 的 HLS 与 WebRTC 是两个独立端口（8888 / 8889），
    任一地址配错都会播不出来，所以分开回报，便于直接看出要改哪一个。
    ``status=404`` 表示端口通、只是当前没有这个流，属于正常。
    """
    endpoints = stream_endpoints()
    if not endpoints["enabled"]:
        return {"ok": False, "reason": "disabled", "probes": {}}
    verify = bool(endpoints["verifyTls"])
    probes: dict[str, dict[str, Any]] = {}
    for name, url in (("webrtc", endpoints["originPlayPage"]), ("hls", endpoints["originHls"])):
        if not url:
            probes[name] = {"ok": False, "reason": "未配置该地址"}
            continue
        try:
            resp = await _client_get(verify).get(url, timeout=6.0)
            probes[name] = {"ok": resp.status_code < 500, "status": resp.status_code, "url": url}
        except httpx.HTTPError as exc:
            probes[name] = {"ok": False, "reason": _friendly_error(exc), "url": url}
    log.debug("直播源探测 | %s", probes)

    # 顺带刷新「谁真的在推流」：管理端点「刷新信号」时会强制重新查一次
    cfg = store.snapshot()
    api = (cfg.stream.api_base or "").strip()
    ready = await ready_paths(max_age=0.0) if api else None
    out: dict[str, Any] = {
        # 以 WebRTC/HTTP 端口为准判断「信号就绪」
        "ok": bool(probes.get("webrtc", {}).get("ok")),
        "origin": endpoints["origin"],
        "secure": endpoints["secure"],
        "verifyTls": verify,
        "probes": probes,
        # 正在推流的机位（选手 ID）：前端用它决定要不要显示「直播中」
        "streamingKnown": ready is not None,
        "streaming": await streaming_player_ids(cfg) if ready is not None else [],
        # 主直播间（默认流名）是否有人在推：只有真的在推，前端才给出这一路信号
        "mainStreaming": bool(ready) and main_stream_key() in (ready or set()),
        "api": {
            "configured": bool(api),
            "ok": ready is not None,
            "url": api,
            "reason": (
                ""
                if ready is not None
                else (
                    "未配置 MediaMTX API 地址（默认 :9997），无法判断谁在推流"
                    if not api
                    else (
                        _ready_cache.get("reason")
                        or "MediaMTX API 不可达：请确认 api: yes 且端口已开放"
                    )
                )
            ),
        },
    }
    if not out["ok"]:
        out["reason"] = probes.get("webrtc", {}).get("reason") or "直播源不可达"
    return out
