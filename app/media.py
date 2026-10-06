"""上传图片：**唯一的内容寻址仓库**（公告插图、赛事信息、本地头像都放这儿）。

四件事：

1. **按魔数判断真实格式**，不信 data:URL 里声明的 MIME —— 否则可以把一段
   HTML / JS 命名成 `.png` 传上来，再配上浏览器的内容嗅探就是存储型 XSS；
2. **不收 SVG**：SVG 能带脚本，属于「看着像图片的可执行文件」，风险与收益不成正比；
3. **内容寻址，全库去重**：文件名 = 内容哈希 + 真实格式，所以
   **同一张图无论从哪个入口传（公告插图 / 头像）、客户端把 MIME 写得多离谱**，
   落到磁盘上的都是同一个文件——不会出现第二份；
4. 正因为文件名就是内容，可以放心发 `immutable` 长缓存：
   「换了图」= 文件名变了，浏览器必然拿到新的。

旧布局（只读兼容）
------------------

早期版本里「本地上传的头像」单独放在 ``data/avatars/``：同一张图从头像入口传一次、
从公告入口传一次，磁盘上就是**两份**（两个目录各查各的）。现在读的时候两个目录都认
（见 :func:`find`），写入一律进统一仓库，历史遗留的重复由 :func:`merge_legacy`
一次性收拢——**新的重复不会再产生**。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path

from .logging_conf import get_logger
from .store import DATA_DIR

log = get_logger("media")

#: 统一仓库：所有上传图片（含本地头像）都落在这里
UPLOAD_DIR = DATA_DIR / "uploads"

#: 单张图片上限。公告里的插图不需要更大；真需要时让管理员自己压一下再传。
MAX_BYTES = 8 * 1024 * 1024

#: 内容寻址图片的响应头：``/api/media/<哈希>`` 与 ``/api/avatar/file/<哈希>`` **共用这一份**。
#:
#: * ``immutable`` + 一年：文件名就是内容哈希，「换图」= 换文件名，永远不会拿到旧图，
#:   所以可以放到最长（浏览器与 CDN 都不用再回源）；
#: * ``nosniff``：我们自己保证 MIME 与内容一致，但**别让浏览器自己猜**——万一某个历史
#:   文件的扩展名不对（旧版头像听客户端声明的 MIME），嗅探可能把它当脚本执行。
#:
#: 写成一份常量是为了「两处地址不会各改一半」：这两条路径指向的是同一个仓库里的同一张图，
#: 缓存策略不一致只会造成「换个地址访问就换了行为」这种最难查的问题。
IMMUTABLE_HEADERS: dict[str, str] = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}

#: 体积为 0 / 明显不是图片时直接拒绝（魔数校验兜底）
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
#: 文件名 = 内容哈希（默认 16 位十六进制，碰巧撞名时退到 32 位）+ 真实扩展名。
#: 两种长度都认，是为了兼容早期版本已经落在磁盘上的 16 位文件名。
_NAME_RE = re.compile(r"^[0-9a-f]{16}(?:[0-9a-f]{16})?\.(?:png|jpg|webp|gif)$")
_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _detect(raw: bytes) -> str:
    """按魔数判断图片格式；不是已知图片就抛 ``ValueError``。"""
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    raise ValueError("这不是一张图片（只支持 PNG / JPEG / WebP / GIF，且不接受 SVG）")


def _legacy_roots() -> list[Path]:
    """旧布局目录（只读兼容）：本地上传的头像曾经放在 ``data/avatars/``。

    在函数里导入是为了避开循环（``avatars`` 又要用这里的 :func:`put`）。
    """
    from . import avatars

    return [avatars.AVATAR_DIR]


def _roots() -> list[Path]:
    """找文件时按顺序看的目录：统一仓库优先，旧目录兜底。"""
    return [UPLOAD_DIR, *_legacy_roots()]


def _name_for(raw: bytes, *, full: bool = False) -> str:
    """内容寻址的文件名（``full=True`` 用完整哈希，见 :func:`put` 的撞名处理）。"""
    digest = hashlib.sha256(raw).hexdigest()
    ext = _detect(raw)
    return f"{digest if full else digest[:16]}.{ext}"


def find(name: str) -> Path | None:
    """按文件名在**整个仓库**里找（统一仓库优先，旧头像目录兜底）。

    跨目录认名字是「同一张图不会存两份」的关键：升级前落在旧头像目录里的那份，
    新上传同一张图时会**复用它**，而不是在新目录里再写一份。
    """
    if not _NAME_RE.match(name or ""):
        return None
    for root in _roots():
        path = root / name
        if path.is_file():
            return path
    return None


def resolve(name: str) -> Path | None:
    """文件名 → 磁盘路径（白名单正则 + 内容哈希名，天然防穿越）。"""
    return find(name)


def _reuse(path: Path, raw: bytes) -> bool:
    """磁盘上那份的内容与 ``raw`` 是否**逐字节相同**。

    只看文件名是不够的：16 位十六进制哈希虽然撞不上，但文件被手工替换 / 拷坏是
    真会发生的。多读一次（几 KB）换「复用的一定是同一张图」，划算。
    """
    try:
        return path.read_bytes() == raw
    except OSError:
        return False


def _existing_copy(raw: bytes) -> Path | None:
    """按**内容**找仓库里已有的那份（找到就复用它）。

    不能只试「真实格式算出来的那个名字」：历史数据里同一份内容可能存成别的扩展名
    ——旧版头像按**客户端声明的 MIME** 取扩展名，一张 PNG 声明成 ``image/jpeg``
    就落成了 ``.jpg``。名字的哈希部分与扩展名无关，所以按哈希前缀把所有可能的
    扩展名都试一遍（最多几次 ``is_file`` + 命中时读一次比对），才真正「同一张图只有一份」。
    """
    digest = hashlib.sha256(raw).hexdigest()
    exts = dict.fromkeys((_detect(raw), *_MIME_BY_EXT))
    for stem in (digest[:16], digest):
        for ext in exts:
            path = find(f"{stem}.{ext}")
            if path is not None and _reuse(path, raw):
                return path
    return None


def put(raw: bytes, *, max_bytes: int = MAX_BYTES) -> dict[str, object]:
    """把一张图放进仓库，返回 ``{url, name, bytes, ext, reused}``。

    **已经有一模一样的图就复用**（不写第二份，``reused=True``）。
    """
    if not raw:
        raise ValueError("图片内容为空")
    if len(raw) > max_bytes:
        raise ValueError(f"图片体积超过 {max_bytes // 1024 // 1024} MB，请压缩后再传")

    hit = _existing_copy(raw)
    if hit is not None:
        # 复用它（返回**它现在的名字**：升级前的老资料里引用的就是这个地址）
        log.info("图片已存在，直接复用 | name=%s | bytes=%d", hit.name, len(raw))
        return {
            "url": f"/api/media/{hit.name}",
            "name": hit.name,
            "bytes": len(raw),
            "ext": hit.suffix.lstrip("."),
            "reused": True,
        }

    name = _name_for(raw)
    if (UPLOAD_DIR / name).exists():
        # 名字被占了、内容却不是它（哈希碰撞，或文件被人手工换过）：
        # 退回**完整哈希**命名，绝不覆盖别人的图。
        name = _name_for(raw, full=True)
        log.warning("内容哈希撞名（或那份文件被改过），改用完整哈希 | name=%s", name)
    path = UPLOAD_DIR / name
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        log.error("图片写入失败 | name=%s | err=%s", name, exc)
        raise ValueError("图片保存失败，请重试") from exc
    log.info("图片已保存 | name=%s | bytes=%d", name, len(raw))
    return {
        "url": f"/api/media/{name}",
        "name": name,
        "bytes": len(raw),
        "ext": name.rsplit(".", 1)[1],
        "reused": False,
    }


def save_data_url(data_url: str) -> dict[str, object]:
    """保存 data:URL 图片，返回 ``{url, name, bytes, ext}``。

    以内容哈希命名：天然去重、不可能路径穿越，而且**改了内容就是新文件名**。
    """
    head, _, payload = (data_url or "").partition(",")
    if not payload or not head.startswith("data:") or ";base64" not in head:
        raise ValueError("图片数据格式不正确（需要 data:...,base64 形式）")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片数据无法解码") from exc
    saved = put(raw)
    saved.pop("reused", None)
    return saved


def merge_legacy() -> int:
    """把旧布局里的图片并进统一仓库，顺带**去掉历史重复**（返回处理过的文件数）。

    旧版本里「本地上传的头像」单独放在 ``data/avatars/``，于是同一张图从头像入口传一次、
    从公告入口传一次就成了两份。这里做一次性收拢：

    * 统一仓库里**已有同名文件**（同名 = 同一份内容）→ 直接删掉旧目录里多出来的那份；
    * 没有 → 搬过去；
    * 同名但内容不同（撞名 / 被手工改过）→ 给旧文件换个完整哈希的名字搬过去，**不覆盖**。

    幂等：没有可搬的就什么都不做。只认内容寻址的命名，别的文件一律不碰。
    """
    handled = 0
    for root in _legacy_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file() or not _NAME_RE.match(path.name):
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            target = UPLOAD_DIR / path.name
            try:
                if target.is_file():
                    if _reuse(target, raw):
                        path.unlink()  # 同一份内容：删掉多出来的那份
                        log.info("旧目录里的重复图片已删除 | %s", path.name)
                    else:
                        target = UPLOAD_DIR / _name_for(raw, full=True)
                        os.replace(path, target)
                        log.warning("旧目录里的同名文件内容不同，已改名并入 | %s", target.name)
                else:
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
                handled += 1
            except OSError as exc:
                log.warning("旧目录图片并入失败（跳过）| %s | %s", path.name, exc)
    if handled:
        log.warning("图片仓库已收拢旧目录 | 处理=%d", handled)
    return handled


def mime_for(path: Path) -> str:
    """按**内容**给 MIME（只读文件头几个字节）。

    历史文件里扩展名可能与内容不符（旧版头像按客户端声明的 MIME 取名，一张 PNG
    会落成 ``.jpg``）。按扩展名发 MIME，浏览器拿到的是「自称 JPEG 的 PNG」——
    干脆以内容为准，读不动才退回扩展名。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        head = b""
    try:
        return _MIME_BY_EXT[_detect(head)]
    except ValueError:
        return _MIME_BY_EXT.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


def stats() -> dict[str, int]:
    """占用统计（服务器管理页展示用）。"""
    files = 0
    total = 0
    if UPLOAD_DIR.exists():
        for item in UPLOAD_DIR.iterdir():
            if item.is_file():
                files += 1
                total += item.stat().st_size
    legacy = 0
    for root in _legacy_roots():
        if root.is_dir():
            legacy += sum(1 for item in root.iterdir() if item.is_file())
    return {"files": files, "bytes": total, "maxBytes": MAX_BYTES, "legacy": legacy}
