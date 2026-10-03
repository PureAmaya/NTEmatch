"""管理端鉴权。

管理 KEY 保存在配置文件里，前端只提交 KEY 换取一次性会话令牌，
令牌存于服务端内存并带过期时间——KEY 本身不会出现在任何响应中。
"""

from __future__ import annotations

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


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_factory_key(admin: AdminConfig) -> bool:
    """是否仍是出厂 KEY（明文且未被改动）。

    改为 sha256 存储、或改成其它值后都视为「已自定义」，
    此时启动日志不会再输出 KEY。
    """
    return not (admin.key_sha256 or "").strip() and (admin.key or "") == DEFAULT_ADMIN_KEY


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
        """常量时间比较，支持明文 key 与 sha256 哈希两种配置。"""
        provided = (provided or "").strip()
        if not provided:
            return False
        if admin.key_sha256:
            expected = admin.key_sha256.strip().lower()
            ok = hmac.compare_digest(sha256_hex(provided), expected)
        else:
            expected = admin.key or ""
            ok = bool(expected) and hmac.compare_digest(provided, expected)
        log.debug("管理 KEY 校验 | 结果=%s | 来源=%s", ok, "sha256" if admin.key_sha256 else "plain")
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
