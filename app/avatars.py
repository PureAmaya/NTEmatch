"""QQ 头像抓取与本地缓存。

直接在前端引用 QQ 头像域名会受防盗链 / 混合内容 / 跨域限制影响，
因此统一由服务端代理：命中磁盘缓存直接返回，未命中再回源，
回源失败时生成一张 NTE 风格的 SVG 占位图，保证页面永不破图。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import time
from pathlib import Path

import httpx

from . import media
from .logging_conf import get_logger
from .store import DATA_DIR

log = get_logger("avatar")

CACHE_DIR = DATA_DIR / "avatar_cache"
#: **旧布局**：本地上传的头像曾经单独放这里。现在统一进 ``media.UPLOAD_DIR``
#: （同一个内容仓库，同一张图不会存两份），这个目录只为「读旧数据 / 还原旧备份」保留，
#: 历史文件由 :func:`app.media.merge_legacy` 一次性收拢。
AVATAR_DIR = DATA_DIR / "avatars"

#: 本地上传头像的体积上限（比公告插图小：头像用不到那么大）
_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
_ALLOWED_SIZES = (40, 100, 140, 640)
_MEM_TTL = 600.0
# 磁盘缓存有效期：QQ 换头像后最多 12 小时自动跟上。
# （之前磁盘缓存**永不过期**，换过头像也会一直显示旧图。）
_CACHE_TTL = 12 * 3600.0
# 只做「太小肯定不是图」的兜底；真正的有效性靠下面的魔数校验，
# 避免把体积很小但完全合法的头像误判成错误图
_MIN_BYTES = 100

# 图片魔数：拿不到正确的 content-type 时也能判断是不是真的图片
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",        # JPEG
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"GIF87a",
    b"GIF89a",              # GIF
    b"RIFF",                # WebP（RIFF....WEBP）
    b"BM",                  # BMP
)

# 磁盘缓存扩展名 ↔ MIME（GIF / WebP 也必须按真实格式落盘，
# 否则读回来时 MIME 与内容不符会让浏览器直接不显示）
_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_EXT_BY_MIME = {mime: ext for ext, mime in _MIME_BY_EXT.items()}

# 多个回源地址依次尝试，提升可用性
_SOURCES = (
    "https://q1.qlogo.cn/g?b=qq&nk={qq}&s={size}",
    "https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec={size}&img_type=jpg",
)

_locks: dict[str, asyncio.Lock] = {}
_mem: dict[str, tuple[float, bytes, str]] = {}
_client: httpx.AsyncClient | None = None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=4.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NTE-Match/0.1)",
                "Referer": "https://qzone.qq.com/",
            },
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def normalize_size(size: int | None) -> int:
    if not size:
        return 100
    return min(_ALLOWED_SIZES, key=lambda s: abs(s - size))


def is_valid_qq(qq: str) -> bool:
    return qq.isdigit() and 4 <= len(qq) <= 12


def _placeholder_svg(qq: str, name: str = "") -> bytes:
    """按 QQ 号派生稳定配色，生成「环」占位头像（外环 + 中心圆 + 首字）。"""
    digest = hashlib.md5(qq.encode("utf-8")).hexdigest()
    hue = int(digest[:2], 16) % 360
    label = (name or qq)[:2]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="hsl({hue},85%,58%)"/>
      <stop offset="100%" stop-color="hsl({(hue + 48) % 360},75%,34%)"/>
    </linearGradient>
  </defs>
  <rect width="200" height="200" fill="#0b0e14"/>
  <circle cx="100" cy="100" r="82" fill="none" stroke="url(#g)" stroke-width="9" opacity="0.9"/>
  <circle cx="100" cy="100" r="63" fill="url(#g)" opacity="0.92"/>
  <text x="100" y="124" text-anchor="middle" font-size="64" font-weight="700"
        font-family="Segoe UI, PingFang SC, Microsoft YaHei, sans-serif" fill="#07121a">{label}</text>
</svg>"""
    return svg.encode("utf-8")


async def get_avatar(
    qq: str, size: int = 100, name: str = "", *, refresh: bool = False
) -> tuple[bytes, str, str]:
    """返回 ``(图片字节, MIME, 来源)``，永远不会抛异常。

    来源用于 ``X-NTE-Avatar`` 响应头，方便直接看出这次到底是命中缓存还是回源：

    * ``memory`` 内存缓存（10 分钟）
    * ``disk`` 磁盘缓存且未过期（12 小时）
    * ``fetched`` 本次回源成功
    * ``stale`` 回源失败，暂用磁盘上的旧图兜底
    * ``placeholder`` 回源失败且无缓存，用生成的占位图

    ``refresh=True`` 会跳过内存与磁盘缓存强制回源——换过头像后点「刷新头像」走的就是它。
    """
    size = normalize_size(size)
    key = f"{qq}_{size}"

    if not refresh:
        cached = _mem.get(key)
        if cached and time.time() - cached[0] < _MEM_TTL:
            return cached[1], cached[2], "memory"

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not refresh:
            cached = _mem.get(key)
            if cached and time.time() - cached[0] < _MEM_TTL:
                return cached[1], cached[2], "memory"
            fresh = await asyncio.to_thread(_read_cache, key, _CACHE_TTL)
            if fresh is not None:
                _mem[key] = (time.time(), fresh[0], fresh[1])
                return fresh[0], fresh[1], "disk"

        fetched = await _fetch_remote(qq, size)
        if fetched is not None:
            body, mime = fetched
            await asyncio.to_thread(_write_cache, key, body, mime)
            _mem[key] = (time.time(), body, mime)
            return body, mime, "fetched"

        # 回源失败：磁盘上的旧图通常仍比占位图好（可能只是过期，内容还是本人）
        stale = await asyncio.to_thread(_read_cache, key, None)
        if stale is not None:
            log.warning("头像回源失败，暂用磁盘旧图 | qq=%s | size=%d", qq, size)
            if not refresh:
                _mem[key] = (time.time(), stale[0], stale[1])
            return stale[0], stale[1], "stale"

        log.debug("头像回源失败，使用占位图 | qq=%s | size=%d", qq, size)
        body = _placeholder_svg(qq, name)
        _mem[key] = (time.time(), body, "image/svg+xml")
        return body, "image/svg+xml", "placeholder"


def _looks_like_image(body: bytes) -> bool:
    return any(body.startswith(sig) for sig in _IMAGE_MAGIC)


def _mime_from_magic(body: bytes) -> str:
    """按魔数推断 MIME（源站偶尔回 ``application/octet-stream``）。"""
    for sig, mime in (
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF8", "image/gif"),
        (b"RIFF", "image/webp"),
        (b"BM", "image/bmp"),
    ):
        if body.startswith(sig):
            return mime
    return ""


async def _fetch_remote(qq: str, size: int) -> tuple[bytes, str] | None:
    client = _client_get()
    for template in _SOURCES:
        url = template.format(qq=qq, size=size)
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            log.debug("头像请求异常 | url=%s | err=%s", url, exc)
            continue
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        body = resp.content
        # 以「魔数」为准判断是不是图片：只看体积会把尺寸很小但合法的头像误杀
        if resp.status_code == 200 and len(body) >= _MIN_BYTES and _looks_like_image(body):
            mime = ctype if ctype in _EXT_BY_MIME else (_mime_from_magic(body) or "image/jpeg")
            log.debug("头像回源成功 | qq=%s | size=%d | %s | bytes=%d", qq, size, mime, len(body))
            return body, mime
        log.debug(
            "头像回源未命中 | qq=%s | status=%s | type=%s | bytes=%d | 魔数=%s",
            qq,
            resp.status_code,
            ctype,
            len(body),
            _looks_like_image(body),
        )
    return None


def _cache_path(key: str, ext: str) -> Path:
    return CACHE_DIR / f"{key}.{ext}"


def _read_cache(key: str, max_age: float | None) -> tuple[bytes, str] | None:
    """读磁盘缓存；``max_age=None`` 表示不看时间（回源失败时的兜底）。

    只认真正的图片扩展名——历史上 GIF 被错误地存成 ``.svg``，
    读回来 MIME 与内容不符会让浏览器直接不显示，因此这里忽略 ``.svg``。
    """
    for ext, mime in _MIME_BY_EXT.items():
        path = _cache_path(key, ext)
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_size <= 0:
                continue
            if max_age is not None and time.time() - stat.st_mtime > max_age:
                log.debug("头像磁盘缓存已过期 | key=%s | 龄=%.0fs", key, time.time() - stat.st_mtime)
                continue
            return path.read_bytes(), mime
        except OSError:
            continue
    return None


def _write_cache(key: str, body: bytes, mime: str) -> None:
    ext = _EXT_BY_MIME.get(mime)
    if ext is None:
        log.debug("头像格式无法缓存到磁盘 | key=%s | mime=%s", key, mime)
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # 先清掉同 key 的其它扩展名，避免旧格式被优先读到
        for other in _MIME_BY_EXT:
            if other != ext:
                _cache_path(key, other).unlink(missing_ok=True)
        _cache_path(key, ext).write_bytes(body)
    except OSError as exc:
        log.debug("头像缓存写入失败 | key=%s | err=%s", key, exc)


def save_data_url(data_url: str) -> str:
    """保存 data:URL 形式的头像，返回同源可访问地址（校验失败抛 ``ValueError``）。

    落盘交给 :func:`app.media.put`（**统一的内容仓库**），所以：

    * 格式按**魔数**判断，不信 data:URL 里声明的 MIME —— 否则可以塞一段 HTML
      冒充 ``image/png``（公告图片那条路早就防了，头像这条路以前是漏的）；
    * 同一张图**无论从头像入口还是从公告入口传，都只会有一份**，复用已有文件。
    """
    raw = _decode(data_url)
    if len(raw) > _UPLOAD_MAX_BYTES:
        raise ValueError(f"头像体积超过 {_UPLOAD_MAX_BYTES // 1024} KB")
    return str(media.put(raw, max_bytes=_UPLOAD_MAX_BYTES)["url"])


def _decode(data_url: str) -> bytes:
    """把 data:URL 解成字节（只做解码，格式交给 :func:`app.media.put` 按魔数判断）。"""
    head, _, payload = (data_url or "").partition(",")
    if not payload or not head.startswith("data:") or ";base64" not in head:
        raise ValueError("头像数据格式不正确")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("头像数据无法解码") from exc


def resolve_local(name: str) -> Path | None:
    """把文件名解析为磁盘路径；非法名称或文件不存在返回 None。

    走 :func:`app.media.find`：**统一仓库与旧头像目录都认**，所以升级前上传的
    头像（还在 ``data/avatars/`` 里）照旧能读出来。
    """
    return media.find(name)


def cache_stats() -> dict[str, int]:
    files = 0
    total = 0
    if CACHE_DIR.exists():
        for item in CACHE_DIR.iterdir():
            if item.is_file():
                files += 1
                total += item.stat().st_size
    return {"files": files, "bytes": total, "memory": len(_mem)}
