"""管理端鉴权与**凭据哈希**。

凭据只存在库里（加盐哈希），前端提交密钥换取一次性会话令牌，令牌存于服务端内存
并带过期时间——密钥本身不会出现在任何响应中。

| 凭据 | 熵 | 哈希 | 理由 |
| --- | --- | --- | --- |
| 成员登录密钥 | ≈192 位随机 | `hmac_sha256$salt$digest` | 逐条随机盐，**不可能**被彩虹表 / 交叉比对 |
| Bearer 令牌 | ≈256 位随机 | 同上 | 同上；推流鉴权回调时校验一次，必须够快 |

> 为什么不用 PBKDF2 迭代：这两类凭据本来就是 192~256 位随机串，攻击者没有「弱口令」
> 可猜，迭代只会让每次推流鉴权白烧几十毫秒。带随机盐的哈希值里已经包含盐与参数，
> **随数据库 / 备份一起走**，因此导出、备份、还原到别的机器都不影响校验。
>
> 历史遗留：早期版本有一把**人手输入**的「服务器主管理 KEY」，那种低熵凭据才需要慢
> 哈希（12 万次 PBKDF2）；该凭据已退休，但 ``verify_secret`` 仍然认 PBKDF2 格式，
> 这样老库里万一还留着这种哈希也不会被误判成「密码错误」。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from .logging_conf import get_logger

log = get_logger("auth")

DEFAULT_TTL = 12 * 3600

# 哈希格式前缀（带前缀 = 加盐的新格式；没有前缀 = 历史的裸 sha256，见 verify_secret）
HMAC_PREFIX = "hmac_sha256"
# PBKDF2 前缀只为「读得懂老库里的值」而保留，新凭据一律用 hash_secret（见模块文档）
PBKDF2_PREFIX = "pbkdf2_sha256"
SALT_BYTES = 16


def sha256_hex(raw: str) -> str:
    """**仅供历史格式兼容**使用；新代码请不要再用它存凭据（无盐）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _same(a: str, b: str) -> bool:
    """常量时间比较两个字符串。

    按 UTF-8 **字节**比：``hmac.compare_digest`` 对含非 ASCII 的 ``str`` 会直接抛
    ``TypeError``（用户输入里出现中文 / emoji 就会把登录打成 500）。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_secret(raw: str) -> str:
    """高熵凭据（成员登录密钥 / Bearer 令牌）：**逐条随机盐 + HMAC-SHA256**。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hmac.new(salt, raw.encode("utf-8"), hashlib.sha256).digest()
    return f"{HMAC_PREFIX}${_b64(salt)}${_b64(digest)}"


def is_legacy_hash(stored: str) -> bool:
    """是不是「历史无盐」的裸 sha256（需要用户轮换一次才升到加盐格式）。"""
    value = (stored or "").strip()
    return bool(value) and "$" not in value


def verify_secret(raw: str, stored: str) -> bool:
    """校验凭据（常量时间比较）。

    依次支持：加盐 HMAC → 加盐 PBKDF2 → 历史裸 sha256。历史格式只为让老库
    平滑升级而保留：轮换一次凭据就会写成加盐格式（接口层会提示还有几条待轮换）。
    """
    raw = (raw or "").strip()
    stored = (stored or "").strip()
    if not raw or not stored:
        return False
    try:
        if stored.startswith(f"{HMAC_PREFIX}$"):
            _, salt_b64, digest_b64 = stored.split("$", 2)
            got = hmac.new(_unb64(salt_b64), raw.encode("utf-8"), hashlib.sha256).digest()
            return hmac.compare_digest(got, _unb64(digest_b64))
        if stored.startswith(f"{PBKDF2_PREFIX}$"):
            _, iter_s, salt_b64, digest_b64 = stored.split("$", 3)
            got = hashlib.pbkdf2_hmac(
                "sha256", raw.encode("utf-8"), _unb64(salt_b64), max(1, int(iter_s))
            )
            return hmac.compare_digest(got, _unb64(digest_b64))
    except (ValueError, binascii.Error):
        log.warning("凭据哈希无法解析（已按不匹配处理）")
        return False
    # 历史格式：裸 sha256，无盐
    return _same(sha256_hex(raw), stored.lower())


@dataclass
class Session:
    token: str
    created_at: float
    expires_at: float
    label: str = "admin"
    # 会话身份：一律绑定到登录的那位成员（``permission`` 决定他能做什么）。
    # ``uid=""`` 只在极端情况出现——本机免登录时那位管理员成员记录缺失。
    uid: str = ""
    name: str = ""
    permission: str = "server_admin"

    @property
    def is_server(self) -> bool:
        return self.permission == "server_admin"

    @property
    def can_manage_events(self) -> bool:
        return self.permission in ("event_admin", "server_admin")


class AuthManager:
    """内存会话管理，进程重启即失效（可接受）。"""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._sessions: dict[str, Session] = {}

    # ------------------------------------------------------------------ #
    def issue(
        self,
        label: str = "admin",
        *,
        uid: str = "",
        name: str = "",
        permission: str = "server_admin",
    ) -> Session:
        now = time.time()
        session = Session(
            token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + self._ttl,
            label=label,
            uid=uid,
            name=name,
            permission=permission,
        )
        self._sessions[session.token] = session
        self._gc()
        log.info(
            "签发会话 | label=%s | uid=%s | 权限=%s | 有效期=%ds | 在线会话=%d",
            label,
            uid or "(server-key)",
            permission,
            self._ttl,
            len(self._sessions),
        )
        return session

    def get(self, token: str | None) -> Session | None:
        """取会话（含身份）；无效 / 过期时返回 ``None`` 并顺手清理。"""
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            log.debug("会话已过期")
            return None
        return session

    def check(self, token: str | None) -> bool:
        return self.get(token) is not None

    def revoke_by_uid(self, uid: str, keep: str | None = None) -> int:
        """注销某成员的全部会话（成员被删除 / 权限变更时调用）。"""
        tokens = [
            tok
            for tok, s in self._sessions.items()
            if s.uid == uid and tok != keep
        ]
        for tok in tokens:
            self._sessions.pop(tok, None)
        if tokens:
            log.info("已注销成员会话 | uid=%s | 数量=%d", uid, len(tokens))
        return len(tokens)

    def revoke(self, token: str | None) -> None:
        if token and self._sessions.pop(token, None):
            log.info("管理会话已注销 | 剩余=%d", len(self._sessions))

    def revoke_all(self) -> int:
        """注销全部会话（例如轮换成员密钥后强制重新登录）。"""
        count = len(self._sessions)
        self._sessions.clear()
        if count:
            log.info("已注销全部管理会话 | 数量=%d", count)
        return count

    def _gc(self) -> None:
        now = time.time()
        stale = [tok for tok, s in self._sessions.items() if s.expires_at <= now]
        for tok in stale:
            self._sessions.pop(tok, None)
        if stale:
            log.debug("清理过期会话 %d 个", len(stale))


auth = AuthManager()
