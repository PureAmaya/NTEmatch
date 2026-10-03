"""管理端鉴权与**凭据哈希**。

管理 KEY 保存在数据库里，前端只提交 KEY 换取一次性会话令牌，令牌存于服务端内存
并带过期时间——KEY 本身不会出现在任何响应中。

凭据（登录密钥 / Bearer 令牌 / 主管理 KEY）一律**加盐**存储，明文永不落库：

| 凭据 | 熵 | 哈希 | 理由 |
| --- | --- | --- | --- |
| 成员登录密钥 | ≈192 位随机 | `hmac_sha256$salt$digest` | 逐条随机盐，**不可能**被彩虹表 / 交叉比对 |
| Bearer 令牌 | ≈256 位随机 | 同上 | 同上；推流鉴权回调时校验一次，必须够快 |
| 服务器主管理 KEY | 人手输入（低熵） | `pbkdf2_sha256$迭代$salt$digest` | 会被字典暴力破解，必须**慢**哈希 + 随机盐 |

> 为什么高熵凭据不套 PBKDF2 迭代：它们本来就是 192~256 位随机串，攻击者没有
> 「弱口令」可猜，迭代只会让每次推流鉴权白烧几十毫秒。真正需要慢哈希的是
> **人选的**主管理 KEY（例如出厂的 `NTE-ADMIN`），那里老老实实 12 万次迭代。
> 两类都带随机盐，哈希值里已经包含盐与参数，**随数据库 / 备份一起走**，
> 因此导出、备份、还原到别的机器都不影响校验。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from .defaults import DEFAULT_ADMIN_KEY
from .logging_conf import get_logger
from .models import AdminConfig

log = get_logger("auth")

DEFAULT_TTL = 12 * 3600

# 哈希格式前缀（带前缀 = 加盐的新格式；没有前缀 = 历史的裸 sha256，见 verify_secret）
HMAC_PREFIX = "hmac_sha256"
PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 120_000
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


def hash_password(raw: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """低熵凭据（服务器主管理 KEY）：**随机盐 + PBKDF2 迭代**。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, max(1, iterations))
    return f"{PBKDF2_PREFIX}${iterations}${_b64(salt)}${_b64(digest)}"


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


def verify_admin_key(provided: str, admin: AdminConfig) -> bool:
    """校验服务器主管理 KEY：加盐哈希 → 历史 sha256 → 历史明文。"""
    raw = (provided or "").strip()
    if not raw:
        return False
    if admin.key_hash:
        return verify_secret(raw, admin.key_hash)
    if admin.key_sha256:
        return _same(sha256_hex(raw), admin.key_sha256.strip().lower())
    expected = admin.key or ""
    return bool(expected) and _same(raw, expected)


def admin_key_mode(admin: AdminConfig) -> str:
    """主管理 KEY 当前的存储形态（诊断面板展示用）。"""
    if admin.key_hash:
        return PBKDF2_PREFIX if admin.key_hash.startswith(PBKDF2_PREFIX) else HMAC_PREFIX
    if admin.key_sha256:
        return "sha256"      # 历史无盐
    return "plain" if admin.key else "unset"


def is_factory_key(admin: AdminConfig) -> bool:
    """是否仍是出厂 KEY（明文且未被改动）。

    改成哈希存储、或改成其它值后都视为「已自定义」，此时启动日志不会再输出 KEY。
    """
    if (admin.key_hash or "").strip() or (admin.key_sha256 or "").strip():
        return False
    return (admin.key or "") == DEFAULT_ADMIN_KEY


@dataclass
class Session:
    token: str
    created_at: float
    expires_at: float
    label: str = "admin"
    # 会话身份：服务器管理员用 ``uid=""``（出厂 / 自定义管理 KEY 登录）；
    # 成员用自己的 ``uid`` 登录，``permission`` 决定他能做什么。
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
    def verify_key(self, provided: str, admin: AdminConfig) -> bool:
        """校验服务器主管理 KEY（加盐 PBKDF2 / 历史 sha256 / 历史明文）。"""
        ok = verify_admin_key(provided, admin)
        log.debug("管理 KEY 校验 | 结果=%s | 形态=%s", ok, admin_key_mode(admin))
        return ok

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
        """注销全部会话（例如管理 KEY 变更后强制重新登录）。"""
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
