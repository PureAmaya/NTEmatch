"""公告图片：上传、落盘与读取（图文并茂用的那部分）。

与头像上传同一套路（data:URL → 内容哈希命名 → 同源地址），但有三点加强：

1. **按魔数判断真实格式**，不信 data:URL 里声明的 MIME —— 否则可以把一段
   HTML / JS 命名成 `.png` 传上来，再配上浏览器的内容嗅探就是存储型 XSS；
2. **不收 SVG**：SVG 能带脚本，属于「看着像图片的可执行文件」，风险与收益不成正比；
   真要放矢量图就用 PNG；
3. **内容寻址**：文件名就是内容哈希，所以可以放心发 ``immutable`` 长缓存 ——
   「源文件更新」= 文件名变了，浏览器必然拿到新的，不存在缓存不刷新的问题。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

from .logging_conf import get_logger
from .store import DATA_DIR

log = get_logger("media")

UPLOAD_DIR = DATA_DIR / "uploads"

#: 单张图片上限。公告里的插图不需要更大；真需要时让管理员自己压一下再传。
MAX_BYTES = 8 * 1024 * 1024

#: 体积为 0 / 明显不是图片时直接拒绝（魔数校验兜底）
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|webp|gif)$")
_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _detect(raw: bytes) -> str:
    """按魔数判断图片格式；不是已知图片就抛 ``ValueError``。"""
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    raise ValueError("这不是一张图片（只支持 PNG / JPEG / WebP / GIF，且不接受 SVG）")


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
    if not raw:
        raise ValueError("图片内容为空")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"图片体积超过 {MAX_BYTES // 1024 // 1024} MB，请压缩后再传")
    ext = _detect(raw)  # 真实格式说了算，data:URL 里的 MIME 只作参考
    name = f"{hashlib.sha256(raw).hexdigest()[:16]}.{ext}"
    path = UPLOAD_DIR / name
    if not path.exists():
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError as exc:
            log.error("图片写入失败 | name=%s | err=%s", name, exc)
            raise ValueError("图片保存失败，请重试") from exc
    log.info("公告图片已保存 | name=%s | bytes=%d", name, len(raw))
    return {"url": f"/api/media/{name}", "name": name, "bytes": len(raw), "ext": ext}


def resolve(name: str) -> Path | None:
    """文件名 → 磁盘路径（白名单正则 + 内容哈希名，天然防穿越）。"""
    if not _NAME_RE.match(name or ""):
        return None
    path = UPLOAD_DIR / name
    return path if path.is_file() else None


def mime_for(path: Path) -> str:
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
    return {"files": files, "bytes": total, "maxBytes": MAX_BYTES}
