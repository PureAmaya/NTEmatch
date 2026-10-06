"""管理端鉴权与**凭据哈希**。

凭据只存在库里（加盐哈希），前端提交密钥换取一次性会话令牌，令牌存于服务端
内存**并写回数据库**（见下面的「会话为什么落库」），带过期时间——密钥本身不会出现
在任何响应中。

| 凭据 | 熵 | 哈希 | 理由 |
| --- | --- | --- | --- |
| 成员登录密钥 | ≈192 位随机 | `hmac_sha256$salt$digest` | 逐条随机盐，**不可能**被彩虹表 / 交叉比对 |
| Bearer 令牌 | ≈256 位随机 | 同上 | 同上；推流鉴权回调时校验一次，必须够快 |
| 会话令牌（`sessions` 表） | ≈256 位随机 | 裸 `sha256` | 需要**按值查**（不能被随机盐搅掉）；256 位随机串猜不出来，裸 sha256 足够 |

> 为什么不用 PBKDF2 迭代：这两类凭据本来就是 192~256 位随机串，攻击者没有「弱口令」
> 可猜，迭代只会让每次推流鉴权白烧几十毫秒。带随机盐的哈希值里已经包含盐与参数，
> **随数据库 / 备份一起走**，因此导出、备份、还原到别的机器都不影响校验。
>
> 历史遗留：早期版本有一把**人手输入**的「服务器主管理 KEY」，那种低熵凭据才需要慢
> 哈希（12 万次 PBKDF2）；该凭据已退休，但 ``verify_secret`` 仍然认 PBKDF2 格式，
> 这样老库里万一还留着这种哈希也不会被误判成「密码错误」。

会话为什么落库（原本只在内存里）
--------------------------------

**热更新会换掉进程**（见 :mod:`app.hot`）。会话只在内存里的话，每次更新都等于把
所有在线的人踢下线——「不中断业务」也就成了空话；容器 ``restart`` 部署更是每上线
一次就全员重登。

实现是「**内存为准 + 落库兜底**」，而不是「每次请求查库」：

* 命中内存直接返回（热路径零开销）；
* **内存里没有**（新进程 / 刚换代）才去库里 `to_thread` 点查一次，查到就放进内存
  ——所以换代之后**每个令牌只会多付一次**数据库点查；
* 写入是**攒一小会儿批量落库**（1.5 秒一批）：登录 / 登出这种低频动作用不着每次都写盘，
  而 ``main`` 的收尾里还有一次**最终落库**，优雅退出（含热更新换代）一条会话都不会丢；
* 查不到的令牌进一个**短命负缓存**（几秒、有上限）：避免有人拿一堆假令牌把库查穿。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import db
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
    def token_hash(self) -> str:
        """落库用的键：token 的 sha256（**不是**明文，见 ``sessions`` 表的说明）。

        这里必须是**确定性的**哈希（不能像凭据那样逐条加随机盐）——否则没法按值点查，
        「换代后认人」也就无从谈起。token 本身是 32 字节随机串，裸 sha256 足够。
        """
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()

    @property
    def is_server(self) -> bool:
        return self.permission == "server_admin"

    @property
    def can_manage_events(self) -> bool:
        return self.permission in ("event_admin", "server_admin")


class AuthManager:
    """会话管理：**内存为准 + 落库兜底**（进程重启不再等于全员登出）。"""

    #: 落库攒批间隔（秒）：登录 / 登出是低频动作，没必要每次都写盘
    FLUSH_INTERVAL = 1.5
    #: 负缓存上限与寿命：防「拿一堆假令牌把库查穿」，又不能长到让人刚登录就被拒
    MISS_MAX = 512
    MISS_TTL = 5.0

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._sessions: dict[str, Session] = {}
        #: 会话落库（:func:`attach` 之前是 ``None``：纯内存模式，测试与老用法照旧）
        self._db_path: Path | None = None
        self._flush_task: asyncio.Task | None = None
        #: 待落库的三个篮子：新增 / 删除 / 按 uid 整批删（后者配合 keep 一起用）
        self._dirty: dict[str, Session] = {}
        self._dead: set[str] = set()
        self._purge_uids: set[str] = set()
        self._purge_all = False
        #: 查不到的令牌（token 散列 → 记住的时刻），见类文档
        self._miss: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 生命周期（由 main 的 lifespan 驱动）
    # ------------------------------------------------------------------ #
    async def attach(self, db_path: Path) -> None:
        """挂上数据库并起落库巡检（在 lifespan 里调用一次）。

        先**清一次过期会话**再开工：库里的行会一直攒着，得有人扫。
        """
        self._db_path = Path(db_path)
        try:
            removed = await asyncio.to_thread(db.prune_sessions, self._db_path)
            if removed:
                log.info("已清理过期会话 | 数量=%d", removed)
        # 兜住一切：库还没建好 / 只读文件系统都不该拦着服务起来，会话退回纯内存模式即可
        except Exception:
            log.warning("会话表清理失败（不影响登录，仅少一层兜底）", exc_info=True)
            self._db_path = None
        if self._db_path is not None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def detach(self) -> None:
        """收尾：停掉巡检并**做最后一次落库**（优雅退出 / 热更新换代都会走到这里）。"""
        task, self._flush_task = self._flush_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            try:
                await self.flush()
            # 落库失败**不该拖垮服务**：内存里仍然有效，下一轮巡检还会再来
            except Exception:
                log.warning("会话落库失败（内存里仍然有效，稍后重试）", exc_info=True)

    async def flush(self) -> None:
        """把攒下的变更写进库（一次事务）。没有变更时什么都不做。"""
        path = self._db_path
        if path is None:
            return
        dirty = list(self._dirty.values())
        dead = list(self._dead)
        uids = list(self._purge_uids)
        purge_all = self._purge_all
        self._dirty.clear()
        self._dead.clear()
        self._purge_uids.clear()
        self._purge_all = False
        if not (dirty or dead or uids or purge_all):
            return
        try:
            if purge_all:
                await asyncio.to_thread(db.delete_all_sessions, path)
            for uid in uids:
                await asyncio.to_thread(db.delete_sessions_of, path, uid)
            if dead:
                await asyncio.to_thread(db.delete_sessions, path, dead)
            if dirty:
                rows = [
                    {
                        "token_hash": s.token_hash,
                        "uid": s.uid,
                        "name": s.name,
                        "permission": s.permission,
                        "label": s.label,
                        "created_at": s.created_at,
                        "expires_at": s.expires_at,
                    }
                    for s in dirty
                ]
                await asyncio.to_thread(db.save_sessions, path, rows)
        # 一次写失败不算数：把变更放回篮子，下次巡检继续（内存里本来就是有效的）
        except Exception:
            log.warning("会话落库失败（内存里仍然有效）", exc_info=True)
            # 失败就放回篮子，别把变更丢了
            self._dirty.update({s.token_hash: s for s in dirty})
            self._dead.update(dead)
            self._purge_uids.update(uids)
            self._purge_all = self._purge_all or purge_all

    # ------------------------------------------------------------------ #
    # 签发 / 校验
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
        self._dirty[session.token_hash] = session
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
        """取会话（**只查内存**）：无效 / 过期时返回 ``None`` 并顺手清理。

        请求路径请用 :meth:`resolve`（它会兜到库里，换代之后照样认人）。
        """
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            self._dead.add(session.token_hash)
            log.debug("会话已过期")
            return None
        return session

    async def resolve(self, token: str | None) -> Session | None:
        """鉴权入口：内存优先，没有就**去库里找一次**（换代后新进程靠它认人）。"""
        session = self.get(token)
        if session is not None or not token:
            return session
        if self._db_path is None:
            return None
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        self._gc_miss(now)
        if key in self._miss:
            return None
        try:
            row = await asyncio.to_thread(db.load_session, self._db_path, key)
        # 库暂时读不了：当作没这个会话（401 让人重登），总好过把请求打成 500
        except Exception:
            log.warning("会话查询失败（按未登录处理）", exc_info=True)
            return None
        if row is None:
            self._miss[key] = now
            return None
        session = Session(
            token=token,
            created_at=float(row.get("created_at") or now),
            expires_at=float(row.get("expires_at") or now),
            label=str(row.get("label") or "admin"),
            uid=str(row.get("uid") or ""),
            name=str(row.get("name") or ""),
            permission=str(row.get("permission") or "server_admin"),
        )
        # 找回来的会话放回内存：同一个令牌之后就不再查库了
        self._sessions[token] = session
        log.debug("会话已从库里恢复 | uid=%s", session.uid or "(server-key)")
        return session

    def check(self, token: str | None) -> bool:
        return self.get(token) is not None

    @property
    def persisted(self) -> bool:
        """会话是否落库（管理端「重启会不会掉线」的说明用它）。"""
        return self._db_path is not None

    @property
    def online(self) -> int:
        """内存里的会话数（诊断用；真实数量以库为准，见 db.count_sessions）。"""
        return len(self._sessions)

    # ------------------------------------------------------------------ #
    # 注销
    # ------------------------------------------------------------------ #
    def revoke_by_uid(self, uid: str, keep: str | None = None) -> int:
        """注销某成员的全部会话（成员被删除 / 权限变更时调用）。

        ``keep`` 是「这次刚签发的那个，别撤」——它会被重新写回库，
        所以不能简单地按 uid 全删（见 :meth:`flush` 的落库顺序）。
        """
        tokens = [tok for tok, s in self._sessions.items() if s.uid == uid and tok != keep]
        kept = self._sessions.get(keep) if keep else None
        for tok in tokens:
            session = self._sessions.pop(tok, None)
            if session is not None:
                self._dead.add(session.token_hash)
        # 内存里没有的（别的进程签的 / 还没恢复的）也要一起失效：按 uid 整批删
        self._purge_uids.add(uid)
        if kept is not None:
            self._dirty[kept.token_hash] = kept  # 落库顺序：先按 uid 删，再写回 keep
        if tokens:
            log.info("已注销成员会话 | uid=%s | 数量=%d", uid, len(tokens))
        return len(tokens)

    def revoke(self, token: str | None) -> None:
        session = self._sessions.pop(token, None) if token else None
        if session is not None:
            self._dead.add(session.token_hash)
            log.info("管理会话已注销 | 剩余=%d", len(self._sessions))

    def revoke_all(self) -> int:
        """注销全部会话（例如轮换成员密钥后强制重新登录）。"""
        count = len(self._sessions)
        self._sessions.clear()
        if count:
            log.info("已注销全部管理会话 | 数量=%d", count)
        if self._db_path is not None:
            self._purge_all = True
            self._dirty.clear()  # 全清了，没必再写回
        return count

    # ------------------------------------------------------------------ #
    def _gc(self) -> None:
        now = time.time()
        stale = [tok for tok, s in self._sessions.items() if s.expires_at <= now]
        for tok in stale:
            session = self._sessions.pop(tok, None)
            if session is not None:
                self._dead.add(session.token_hash)
        if stale:
            log.debug("清理过期会话 %d 个", len(stale))

    def _gc_miss(self, now: float) -> None:
        """清掉过期的负缓存；超标就整体清空（它只是个挡箭牌，不是账本）。"""
        if not self._miss:
            return
        if len(self._miss) > self.MISS_MAX:
            self._miss.clear()
            return
        stale = [key for key, at in self._miss.items() if now - at > self.MISS_TTL]
        for key in stale:
            self._miss.pop(key, None)


auth = AuthManager()
