"""登录失败限制（类 fail2ban）。

只作用于 ``POST /api/auth``：同一客户端 IP 在时间窗内失败次数达到阈值后
**封禁一段时间**，期间登录请求直接回 ``429``（带 ``Retry-After``）。参数由
服务器管理员在 ``/admin → 登录限制`` 里配置。

**真实 IP**：默认只用直连地址（``request.client.host``）。站点前面有反向代理 /
负载均衡（如 EdgeOne）时，把**代理的 IP / CIDR** 填进 ``trustedProxies``，
才会信任 ``X-Forwarded-For`` / ``X-Real-IP``；**未列入可信代理时一律忽略这些
转发头**——否则任何人都能伪造 IP 来绕过封禁，或把别人「陷害」进黑名单。

计数与封禁只在内存里（进程重启即清空），对登录这种低频操作完全够用，
也避免为一点防护引入额外的落盘写。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any

from starlette.requests import Request

from .logging_conf import get_logger

log = get_logger("guard")

# 默认参数（服务器管理员可改；键名与前端表单一致，camelCase）
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "maxAttempts": 5,        # 窗口内允许的失败次数
    "windowSeconds": 300,    # 计数时间窗（秒）
    "banSeconds": 900,       # 触发后封禁时长（秒）；0 = 只计数不封禁
    "trustedProxies": "",    # 可信反向代理 IP / CIDR（逗号分隔）；空 = 不信任任何转发头
    "whitelist": "",         # 永不封禁的 IP / CIDR（逗号分隔）
    # 本机（回环地址）访问不设防：免登录、也不受登录失败限制
    "localTrust": True,
}
GUARD_KEYS = tuple(DEFAULT_SETTINGS)

# 认定为「本机」的 Host 头取值
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# 出现任意一个转发头就说明请求是经代理进来的，不能再当作本机
_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-server",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "true-client-ip",
)


# 防御性上限：跟踪的 IP 条目过多时丢弃最旧的一半
_MAX_TRACKED = 5000

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_bans: dict[str, float] = {}


# --------------------------------------------------------------------------- #
# IP 解析
# --------------------------------------------------------------------------- #
def _nets(raw: str) -> list[Any]:
    """把逗号 / 分号分隔的 IP / CIDR 解析成网络对象（非法项记一条日志后忽略）。"""
    out: list[Any] = []
    for item in str(raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("登录限制：忽略非法的 IP / CIDR | %s", item)
    return out


def _in_nets(ip: str, nets: list[Any]) -> bool:
    if not ip or not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def is_local(request: Request, settings: dict[str, Any]) -> bool:
    """是否为「本机直连」访问（本地不设防的判定）。

    三个条件**同时**满足才算本机，避免反向代理把 127.0.0.1 当成所有访客：

    1. 直连方是回环地址（127.0.0.1 / ::1）；
    2. 请求里**没有任何转发头**（有就说明是从代理进来的）；
    3. ``Host`` 头是本机名（localhost / 127.0.0.1 / [::1]）。

    只要有人把反向代理架在本机且不设转发头，第 3 条通常仍能挡住（浏览器访问
    公网域名时 Host 是域名）；这也是为什么必须三条齐备。
    """
    if not settings.get("localTrust", True):
        return False
    peer = request.client.host if request.client else ""
    if not _is_loopback(peer):
        return False
    for name in _FORWARD_HEADERS:
        if (request.headers.get(name) or "").strip():
            return False
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # IPv6 字面量：[::1]:8000
        host = host.split("]", 1)[0].strip("[]")
    else:
        host = host.split(":", 1)[0]
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def _is_loopback(ip: str) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def client_ip(request: Request, settings: dict[str, Any]) -> str:
    """解析真实客户端 IP（只看直连地址，除非直连方在可信代理名单里）。"""
    peer = request.client.host if request.client else ""
    trusted = _nets(settings.get("trustedProxies", ""))
    if not trusted or not _in_nets(peer, trusted):
        return peer or "unknown"
    # 直连方是可信代理：取 X-Forwarded-For 里最右侧「非可信」的那一跳
    chain = [
        part.strip()
        for part in (request.headers.get("x-forwarded-for") or "").split(",")
        if part.strip()
    ]
    if not chain:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            chain = [real]
    chain.append(peer)
    for hop in reversed(chain):
        if not _in_nets(hop, trusted):
            return hop
    return chain[0] if chain else peer


#: 永久封禁（``seconds=0``）：用 ``inf`` 表示「没有到期时间」
PERMANENT = float("inf")
#: 永久封禁回给客户端的 ``Retry-After``：HTTP 头只能放秒数，
#: 这里给一年（足够表达「别等了」），列表里仍显示「永久」。
PERMANENT_RETRY_AFTER = 365 * 24 * 3600
#: 封禁原因：自动（登录失败过多）/ 手动（管理员加的）
REASON_AUTO = "登录失败过多"


# --------------------------------------------------------------------------- #
# 计数与封禁
# --------------------------------------------------------------------------- #
def _prune(now: float) -> None:
    for ip, row in list(_bans.items()):
        until = float(row.get("until") or 0)
        if until != PERMANENT and until <= now:
            _bans.pop(ip, None)
    if len(_failures) > _MAX_TRACKED:
        oldest = sorted(_failures, key=lambda k: max(_failures[k] or [0]))[: len(_failures) // 2]
        for ip in oldest:
            _failures.pop(ip, None)


def is_whitelisted(ip: str, settings: dict[str, Any]) -> bool:
    """该 IP 是否在「永不封禁」名单里。"""
    return _in_nets(ip, _nets(settings.get("whitelist", "")))


def _whitelist_bypasses(ip: str, settings: dict[str, Any]) -> bool:
    """白名单能不能放过这个 IP。

    **自动**封禁会被白名单挡下（那是「别误伤自己人」）；但管理员**手动**加的封禁
    必须生效——不然他点了封禁、界面写着已封禁，实际却拦不住，这种「看着有、其实没有」
    比不加这个功能更糟。
    """
    if not is_whitelisted(ip, settings):
        return False
    with _lock:
        row = _bans.get(ip)
    return not (row and not row.get("auto"))


def blocked_seconds(ip: str, settings: dict[str, Any]) -> int:
    """该 IP 当前是否被封禁；返回剩余秒数（0 = 未被封禁，永久封禁回一年）。"""
    if not settings.get("enabled", True) or not ip:
        return 0
    if _whitelist_bypasses(ip, settings):
        return 0
    now = time.time()
    with _lock:
        row = _bans.get(ip)
        if row is None:
            return 0
        until = float(row.get("until") or 0)
        if until == PERMANENT:
            return PERMANENT_RETRY_AFTER
        if until <= now:
            _bans.pop(ip, None)
            return 0
        return int(until - now) + 1


def ban(
    ip: str,
    seconds: int,
    *,
    reason: str = "手动封禁",
    auto: bool = False,
) -> bool:
    """封禁一个 IP；``seconds=0`` = **永久**。返回是否是新增（``False`` = 覆盖了原记录）。

    只接受单个 IP（不是网段）：这条路径在每个登录请求上跑，必须是 O(1) 的字典查；
    网段是「可信代理 / 白名单」那种小众且不频繁的判断，那里才用 CIDR。
    """
    clean = _clean_ip(ip)
    if not clean:
        raise ValueError("要封禁的得是一个 IP（例如 203.0.113.7）")
    now = time.time()
    length = int(seconds or 0)
    until = PERMANENT if length <= 0 else now + length
    with _lock:
        fresh = clean not in _bans
        _bans[clean] = {"until": until, "at": now, "reason": str(reason or "")[:60], "auto": bool(auto)}
        if not auto:
            _failures.pop(clean, None)  # 手动封禁：顺带清掉失败计数，别两边都显示
    log.warning(
        "已封禁 IP | ip=%s | 时长=%s | 原因=%s | 方式=%s",
        clean,
        "永久" if until == PERMANENT else f"{length}s",
        reason,
        "自动" if auto else "手动",
    )
    return fresh


def unban(ip: str) -> bool:
    clean = _clean_ip(ip) or str(ip or "").strip()
    with _lock:
        removed = _bans.pop(clean, None) is not None
        _failures.pop(clean, None)
    if removed:
        log.warning("已解除登录封禁 | ip=%s", clean)
    return removed


def record_failure(ip: str, settings: dict[str, Any]) -> int:
    """记一次登录失败；返回本次触发的封禁秒数（0 = 未封禁）。"""
    if not settings.get("enabled", True) or not ip:
        return 0
    if is_whitelisted(ip, settings):
        return 0
    now = time.time()
    window = max(1, int(settings.get("windowSeconds", 300) or 1))
    max_attempts = max(1, int(settings.get("maxAttempts", 5) or 1))
    ban_seconds = max(0, int(settings.get("banSeconds", 900) or 0))
    with _lock:
        hits = [t for t in _failures.get(ip, []) if now - t <= window]
        hits.append(now)
        _failures[ip] = hits
        _prune(now)
        if ban_seconds and len(hits) >= max_attempts:
            _failures.pop(ip, None)
    if ban_seconds and len(hits) >= max_attempts:
        ban(ip, ban_seconds, reason=f"{REASON_AUTO}（{len(hits)} 次 / {window}s）", auto=True)
        return ban_seconds
    return 0


def record_success(ip: str) -> None:
    """登录成功：清掉该 IP 的失败计数与封禁。"""
    if not ip:
        return
    with _lock:
        _failures.pop(ip, None)
        _bans.pop(ip, None)


def _row_view(ip: str, row: dict[str, Any], now: float, window: int) -> dict[str, Any]:
    """一条封禁记录 → 管理端要的形态（时间给人看，剩余秒数给倒计时用）。"""
    until = float(row.get("until") or 0)
    permanent = until == PERMANENT
    return {
        "ip": ip,
        "permanent": permanent,
        "until": "" if permanent else _iso(until),
        "at": _iso(float(row.get("at") or 0)),
        "remaining": PERMANENT_RETRY_AFTER if permanent else max(0, int(until - now) + 1),
        "reason": str(row.get("reason") or ""),
        "auto": bool(row.get("auto")),
        "failures": len([t for t in _failures.get(ip, []) if now - t <= window]),
    }


def _iso(ts: float) -> str:
    """时间戳 → 站内统一的 ISO 文本（本地时区，与其它时间字段一致）。"""
    if ts <= 0:
        return ""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ts).replace(microsecond=0).isoformat()  # noqa: DTZ006  (本地时间)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 极端时间戳
        return ""


def _clean_ip(raw: str) -> str:
    """校验并规范化一个 IP；不合法回空串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def bans_page(
    *,
    page: int = 1,
    size: int = 20,
    query: str = "",
    kind: str = "",
    window: int = 300,
) -> dict[str, Any]:
    """封禁列表（**分页 / 搜索 / 过滤都在服务端做**）。

    列表可能很长（攻击时成百上千条），把全量丢给前端再过滤既费流量、又在最需要
    它快的时候最慢。参数：

    * ``query``：IP 片段（也匹配原因文字）；
    * ``kind``：``""`` 全部 / ``permanent`` 永久 / ``temp`` 临时 / ``auto`` 自动 /
      ``manual`` 手动；
    * 排序：**永久在前**，其余按到期时间从近到远（最该处理的先看到）。
    """
    now = time.time()
    needle = str(query or "").strip().lower()
    want = str(kind or "").strip().lower()
    rows: list[dict[str, Any]] = []
    with _lock:
        _prune(now)
        for ip, row in _bans.items():
            view = _row_view(ip, row, now, window)
            if needle and needle not in ip.lower() and needle not in view["reason"].lower():
                continue
            permanent = view["permanent"]
            auto = view["auto"]
            if want == "permanent" and not permanent:
                continue
            if want == "temp" and permanent:
                continue
            if want == "auto" and not auto:
                continue
            if want == "manual" and auto:
                continue
            rows.append(view)
        failing = {
            ip: len([t for t in hits if now - t <= window])
            for ip, hits in _failures.items()
            if any(now - t <= window for t in hits)
        }
    rows.sort(key=lambda item: (not item["permanent"], item["remaining"]))
    size = max(1, min(int(size or 20), 200))
    page = max(1, int(page or 1))
    total = len(rows)
    start = (page - 1) * size
    return {
        "items": rows[start : start + size],
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
        "failing": failing,
        "now": now,
    }


def snapshot(window: int = 300) -> dict[str, Any]:
    """当前封禁与失败计数（管理端用）。

    保留这个「一次拿全量」的形态（其它地方仍在使用）：
    ``bans`` 里的每一项与 :func:`bans_page` 的条目结构一致。
    """
    now = time.time()
    with _lock:
        bans = [_row_view(ip, row, now, window) for ip, row in _bans.items()]
        failing = {
            ip: len([t for t in hits if now - t <= window])
            for ip, hits in _failures.items()
            if any(now - t <= window for t in hits)
        }
    bans.sort(key=lambda item: (not item["permanent"], item["remaining"]))
    return {"bans": bans, "failing": failing}


def clear() -> int:
    """清空全部封禁与失败计数；返回被清掉的封禁数。"""
    with _lock:
        count = len(_bans)
        _bans.clear()
        _failures.clear()
    if count:
        log.warning("已清空全部登录封禁 | 数量=%d", count)
    return count
