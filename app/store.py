"""赛事配置存储（多届赛事 · SQLite）。

目录布局::

    config/nte.sqlite3        全部届次数据（SQLite，WAL 模式）
    config/events/*.json      旧版每届 JSON，首次启动自动导入
    config/match.json         旧版单文件赛事，首次导入为 e001
    config/migrated-json/     迁移后的旧文件备份（可安全删除）

职责：
* 载入 / 校验当前届配置（数据库为空时按模板创建并登记）
* 所有写操作串行化（asyncio.Lock），单事务覆盖式写入，不会写半截
* 提供届次的增删改查与切换；历史届次只读读取

设计上对外只暴露「读快照 / 事务式修改 / 届次管理」三类能力，
业务逻辑不直接接触数据库，保证高内聚。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, league, tournament
from .auth import hash_secret, verify_secret
from .defaults import default_config
from .logging_conf import get_logger
from .login_guard import DEFAULT_SETTINGS as GUARD_DEFAULTS
from .login_guard import GUARD_KEYS
from .models import Channel, Config, LiveBan, Member, Player
from .qqbot import DEFAULT_SETTINGS as QQBOT_DEFAULTS

log = get_logger("store")

# 出厂示例名单：早期版本会把这份假数据写进新库，现在已从默认配置里移除。
# 载入时做一次性清理，**只在「原样未动」时触发**（见 _strip_demo_roster）。
_LEGACY_DEMO_ROSTER: tuple[tuple[str, str, str], ...] = (
    ("p01", "星野遥", "10001"),
    ("p02", "洛鸢", "10002"),
    ("p03", "司岚", "10003"),
    ("p04", "夜见铃", "10004"),
    ("p05", "白鹭昕", "10005"),
    ("p06", "沉舟", "10006"),
    ("p07", "苏黎", "10007"),
    ("p08", "阿澈", "10008"),
    ("p09", "江晚", "10009"),
    ("p10", "陆铭", "10010"),
)


def _strip_demo_roster(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """清掉早期版本写进新库的出厂示例选手，返回 ``(数据, 是否清理过)``。

    条件刻意卡得很死，避免误删真实数据——必须**同时**满足：

    * 人数正好是这 10 位，且 id / 姓名 / QQ 与出厂值逐一相同（改过名就不动）；
    * 没有任何对局、队伍与参与名单（也就是这些假人还没被用过）。

    名单里还有人用着的话保持原样，交给管理员自己删。
    """
    players = data.get("players") or []
    if len(players) != len(_LEGACY_DEMO_ROSTER):
        return data, False
    for player, (pid, name, qq) in zip(players, _LEGACY_DEMO_ROSTER):
        if (player.get("id"), player.get("name"), player.get("qq")) != (pid, name, qq):
            return data, False
    if (data.get("rounds") or []) or (data.get("teams") or []) or (data.get("participants") or []):
        return data, False
    return {**data, "players": []}, True

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 数据根目录：默认就在仓库里（config / data / backups 三处）。
# ``NTE_DATA_DIR`` 可以把它整个挪走——容器里挂一个卷最省事；代码与静态资源
# 始终跟着仓库走，不受影响。
_DATA_ENV = os.getenv("NTE_DATA_DIR", "").strip()
DATA_ROOT = Path(_DATA_ENV).expanduser().resolve() if _DATA_ENV else PROJECT_ROOT
CONFIG_DIR = DATA_ROOT / "config"
DB_PATH = CONFIG_DIR / "nte.sqlite3"
LEGACY_EVENTS_DIR = CONFIG_DIR / "events"
LEGACY_INDEX_PATH = CONFIG_DIR / "index.json"
LEGACY_CONFIG_PATH = CONFIG_DIR / "match.json"
MIGRATED_DIR = CONFIG_DIR / "migrated-json"
DATA_DIR = DATA_ROOT / "data"
# 备份也属于「数据」：跟着 DATA_ROOT 走，别把它们留在镜像层里
BACKUP_ROOT = DATA_ROOT / "backups"

_EVENT_ID_RE = re.compile(r"^e\d{3,}$")

# 全局 meta 键：「频道」板块的公告 / 异环相关内容（不属于任何一届）
CHANNEL_NOTICE_KEY = "channel_notice"
# 全局 meta 键：站点名称（服务器管理员设定；顶栏、浏览器标签、主页都用它）
SITE_NAME_KEY = "site_name"
DEFAULT_SITE_NAME = "NTE 比赛"
SITE_NAME_MAX = 24
# 全局 meta 键：服务器管理员注入的自定义 HTML（用于接入统计 / 数据采集脚本）
CUSTOM_HTML_KEY = "custom_html"
#: 服务器信息（Markdown 原文）：站点级的「关于本站 / 说明」
SERVER_INFO_KEY = "server_info"
# 全局 meta 键：「历届选手 → 成员」的一次性迁移是否已执行（幂等，避免每次启动都扫全库）
MEMBERS_MIGRATED_KEY = "members_migrated_v1"
# 全局 meta 键：登录失败限制（类 fail2ban）的配置（JSON 文本）
LOGIN_GUARD_KEY = "login_guard"

# 成员密钥与 WHIP Bearer 令牌的生成：都由服务端随机产出，明文只回给前端一次。
MEMBER_KEY_BYTES = 24      # 密钥：token_urlsafe(24) ≈ 32 字符
BEARER_TOKEN_BYTES = 32    # Bearer 令牌：token_urlsafe(32) ≈ 43 字符


def new_member_secret() -> str:
    """生成成员登录密钥（随机、URL 安全）。"""
    return secrets.token_urlsafe(MEMBER_KEY_BYTES)


def new_bearer_token() -> str:
    """生成 WHIP 推流 Bearer 令牌（随机、URL 安全、全局唯一）。"""
    return secrets.token_urlsafe(BEARER_TOKEN_BYTES)

Mutator = Callable[[dict[str, Any]], dict[str, Any]]
ChangeHook = Callable[[Config, str], Awaitable[None]]
# 收尾器：在所有变更与阵容重算都完成之后再过一遍（见 Mutate.final）
Finalizer = Callable[[dict[str, Any]], dict[str, Any]]


def _follow_roster_change(
    cfg: Config,
    merged: dict[str, Any],
    before: set[str],
    warnings: list[str],
) -> dict[str, Any]:
    """参与名单变化之后的赛制跟进（积分制重排未结算对局 / 锦标赛制提示重新组队）。

    ``set_participants``（手工勾选手）与 ``adopt_members``（从成员列表勾人）**共用这一份**
    ——分两处写迟早会漂移：一个会重排、另一个只提示，用户看到的就是「同样改了名单，
    结果不一样」。
    """
    chosen = list(merged.get("participants") or [])
    if not chosen:
        if cfg.rounds:
            warnings.append("本届参与名单为空：现有赛程仍按旧名单保留，可重新生成或清空赛程。")
        return merged
    if cfg.rounds and before == set(chosen):
        return merged
    if cfg.rules.format == "league" and cfg.rounds:
        rounds, warns = league.reconcile_rounds(Config.model_validate(merged))
        merged["rounds"] = [r.dump() for r in rounds]
        warnings.extend(warns)
    elif cfg.rounds:
        warnings.append("参与名单已变化：现有队伍与赛程仍是按旧名单生成的，请重新组队并生成赛程。")
    return merged


def now_iso() -> str:
    """本地时区的 ISO 秒级时间戳（刻意使用本地无时区表示，便于前端直接展示）。"""
    return datetime.now().replace(microsecond=0).isoformat()  # noqa: DTZ005


#: 报名 / 取消报名的闸门：只有**筹备中**的届能自助改名单。
#: 文案在 :func:`signup_blocked` 里现算（要说清卡在哪一条，而不是统一回一句「不允许」）。
SIGNUP_EVENT_STATUS = "draft"
_EVENT_STATUS_CN = {"draft": "筹备中", "active": "进行中", "closed": "已结束"}


def signup_blocked(cfg: Config) -> str:
    """报名 / 取消报名现在能不能做；返回**不能做的原因**（空串 = 可以做）。

    两条闸门，理由各不一样，所以文案要分开：

    * **只有筹备中的届**：开赛之后名单就该冻住（谁上场、谁替补已经定了），
      赛后更不用说了——报名 / 取消报名都是「赛前那件事」；
    * **已经组队或生成过赛程的届也不行**：名单一改，队伍与对阵里引用的选手就对不上，
      这种改动得让管理员在网站上看着办（先清空组队 / 赛程，再改名单）。
    """
    if cfg.event.status != SIGNUP_EVENT_STATUS:
        state = _EVENT_STATUS_CN.get(cfg.event.status, cfg.event.status)
        # 文案会原样发到群里（插件直接回显），所以**不写 Markdown 记号**
        return (
            f"这一届现在是「{state}」：报名只对「筹备中」的比赛开放"
            f"（管理员在网站上把状态改回「筹备中」才能继续报名）。"
        )
    if cfg.teams or cfg.rounds:
        return "这一届已经组队 / 生成赛程了：名单不能再自助改，请联系管理员在网站上处理。"
    return ""


def _ensure_player(data: dict[str, Any], member: Member) -> tuple[str, bool]:
    """让这一届里有这个成员的选手档案；返回 ``(选手 id, 是否新建)``。

    报名（:meth:`ConfigStore.sign_up`）与「从成员列表勾人」（:meth:`ConfigStore.adopt_members`）
    **共用这一份**：id 怎么分配（``pNN``）、同步哪几个字段（姓名 / QQ / 头像 / 游戏 UUID），
    两处各写一遍迟早会漂移——用户看到的就成了「同样一个人，报名进来的和勾进来的不一样」。
    """
    players = data.setdefault("players", [])
    for row in players:
        if str(row.get("memberUid") or "") != member.uid:
            continue
        patch: dict[str, Any] = {}
        if member.name and row.get("name") != member.name:
            patch["name"] = member.name
        if row.get("qq") != member.qq:
            patch["qq"] = member.qq
        if member.avatar and row.get("avatar") != member.avatar:
            patch["avatar"] = member.avatar
        if member.game_uuid and row.get("uuid") != member.game_uuid:
            patch["uuid"] = member.game_uuid
        if patch:  # 只补有变化的，免得白改一遍 revision
            row.update(patch)
        return str(row.get("id") or ""), False
    used = {str(p.get("id") or "") for p in players}
    seq = 1
    while f"p{seq:02d}" in used:
        seq += 1
    pid = f"p{seq:02d}"
    players.append(
        {
            "id": pid,
            "name": member.name,
            "uuid": member.game_uuid or "",
            "qq": member.qq,
            "avatar": member.avatar or "",
            "memberUid": member.uid,
        }
    )
    return pid, True


def _roster_ids(cfg: Config) -> list[str]:
    """这一届**实际会在场上的人**（显式名单为空时 = 全员参与）。"""
    from .logic import joined_players  # 局部导入，避免模块级循环依赖

    return [p.id for p in joined_players(cfg)]


def _write_roster(data: dict[str, Any], chosen: list[str]) -> dict[str, Any]:
    """把 ``chosen`` 写成显式参赛名单（``participantsSet=True``）。

    「未显式定过名单 = 全员参与」这个语义在自助报名里很危险：第一个人报名时若从空名单
    起算，原来「全员参与」的人会被集体挤出名单。所以报名 / 取消报名这两条路径都先落到
    **当前实际参与的人**上，再增删一个人。
    """
    from .logic import normalize_participants  # 局部导入，避免模块级循环依赖

    merged = {**data, "participants": [], "participantsSet": True}
    merged["participants"] = normalize_participants(Config.model_validate(merged), chosen)
    return merged


#: 复制一届时，对局里**要抹掉**的字段：这些是「这一场真的打过」的痕迹。
#: 注意是**删字段**（回模型默认）而不是写 0：侧方成绩的默认值是 ``metrics.MISSING``
#: （= 没有成绩），而 0 是合法读数（0 分）——手写 0 会把「没打」写成「拿了 0 分」。
_ROUND_RESULT_KEYS = (
    "status",          # 回到 pending
    "winner",
    "sets",            # 各轮成绩
    "durationMinutes",
    "startedAt",
    "finishedAt",
    "scheduledAt",     # 上一届的日程；新届的日期还没定
    "locked",
)
_SIDE_RESULT_KEYS = ("score", "points", "rank", "forfeit")


def _round_without_result(round_data: dict[str, Any]) -> dict[str, Any]:
    """把一场对局**抹回「一场都还没打」**：留对阵、清成绩。

    留下的是**赛程骨架**：``stage`` / ``code`` / ``label`` / 席位来源（``srcA`` /
    ``srcB``）与两边的**人和队伍**；抹掉的是成绩、胜者、用时、时间戳与锁定。
    于是复制出来的届既有完整对阵表，又不会带着上一届的比分——带着比分只会有两种下场：
    要么一出生就被「打完自动结束本届」判成已结束，要么顶着「筹备中」却已经有冠军。
    """
    out = {k: v for k, v in round_data.items() if k not in _ROUND_RESULT_KEYS}
    out["sides"] = [
        {k: v for k, v in side.items() if k not in _SIDE_RESULT_KEYS}
        for side in round_data.get("sides") or []
    ]
    return out


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """递归合并：字典逐层合并，列表与标量整体替换。"""
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


class ConfigStore:
    """多届赛事的唯一事实来源（single source of truth）。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._lock = asyncio.Lock()
        self._current: str = ""
        self._config: Config = Config.model_validate(default_config())
        self._hooks: list[ChangeHook] = []
        # 成员频道（日常直播）：全局，跨届共享，与 _config 平级
        self._channels: list[Channel] = []
        # 成员（全局账号）：跨届共享；密钥 / 令牌以 sha256 存储
        self._members: list[Member] = []
        # 直播间封禁（全局）：scope=global / event
        self._live_bans: list[LiveBan] = []
        # 频道板块的公告（全局，纯展示文案）
        self._channel_notice: str = ""
        # 站点名称（全局；服务器管理员设定，空 = 用默认值）
        self._site_name: str = ""
        # 服务器管理员注入的自定义 HTML（全局，用于数据采集）
        self._custom_html: str = ""
        # 服务器信息（全局，Markdown）：服务器管理员在管理页维护的「关于本站」
        self._server_info: str = ""
        # 各作用域**最新一条**通知的轻量信息（内存缓存）：状态广播要用到它，
        # 不能每次都查库。key = "scope:event_id"（服务器级是 "server:"）。
        self._notice_heads: dict[str, dict[str, Any]] = {}
        # 登录失败限制（类 fail2ban）配置（全局；运行时计数在 login_guard 模块）
        self._guard: dict[str, Any] = dict(GUARD_DEFAULTS)
        # QQ 机器人（AstrBot）推送配置（全局；含 API Key，只进不出）
        self._qqbot: dict[str, Any] = dict(QQBOT_DEFAULTS)
        self._running = False
        # 出厂示例名单是否在本次载入中被清理（需要在启动时写回数据库）
        self._demo_purged = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(db.init_db, self._db_path)
        await asyncio.to_thread(self._migrate_sync)
        # 计分口径升级：把老数据的 metric 落成「类型 / 标签 / 判断标准」。
        # **写入之前先留一份旧库快照**（只读、可下载、不提供还原，见 app/legacy.py）。
        await asyncio.to_thread(self.migrate_scoring)

        current = await asyncio.to_thread(self._read_current_sync)
        if not current:
            current = await asyncio.to_thread(self._create_blank_sync)
            log.info("数据库为空，已初始化首届赛事 | id=%s", current)
        self._current = current
        self._config = await asyncio.to_thread(self._load_sync, current)
        self._channels = await asyncio.to_thread(self._load_channels_sync)
        self._members = await asyncio.to_thread(self._load_members_sync)
        self._live_bans = await asyncio.to_thread(self._load_live_bans_sync)
        self._channel_notice = await asyncio.to_thread(self._load_channel_notice_sync)
        self._site_name = await asyncio.to_thread(self._load_site_name_sync)
        self._custom_html = await asyncio.to_thread(self._load_custom_html_sync)
        self._server_info = await asyncio.to_thread(self._load_server_info_sync)
        self._notice_heads = await asyncio.to_thread(self._load_notice_heads_sync)
        self._guard = await asyncio.to_thread(self._load_login_guard_sync)
        self._qqbot = await asyncio.to_thread(self._load_qqbot_sync)
        await self.ensure_server_admin()
        # 一次性迁移：历届所有选手都转成成员并建立关联（权限默认「成员」）
        await self.migrate_players_to_members()
        self.report_legacy_credentials()
        if self._demo_purged:
            # 把出厂示例名单的清理结果落盘：数据库里也不该留这些假数据，
            # 顺便刷新 events 表缓存的选手数（往届列表会读它）
            self._demo_purged = False
            await self.mutate(lambda data: data, actor="purge-demo-roster", resolve=False)
        # 补记「其实已经打完」的届（升级上来的老数据 / 在自动结束生效之前打完的届）：
        # 不然届状态会一直停在进行中，而页面上又写着「已结束 · 赛程 10/10 场」
        await self.close_finished_events()
        self._running = True

        cfg = self._config
        events = await self.list_events()
        log.info(
            "赛事已就绪 | 届=%s(%s) | 共 %d 届 | 数据库=%s | 选手=%d | 局数=%d",
            cfg.event.name or cfg.event.title,
            self._current,
            len(events),
            self._db_path,
            len(cfg.players),
            len(cfg.rounds),
        )
        self._warn_non_ascii_stream_keys()

    async def stop(self) -> None:
        self._running = False
        log.debug("配置存储已停止")

    def on_change(self, hook: ChangeHook) -> None:
        self._hooks.append(hook)

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Config:
        """返回当前配置快照（pydantic 模型，调用方不应修改）。"""
        return self._config

    @property
    def revision(self) -> int:
        return self._config.revision

    @property
    def path(self) -> Path:
        """数据库文件路径（导出 / 诊断用）。"""
        return self._db_path

    @property
    def current_id(self) -> str:
        return self._current

    def raw_json(self) -> dict[str, Any]:
        return self._config.dump()

    async def list_events(self) -> list[dict[str, Any]]:
        """届次列表（含当前标记与**举办者名字**），按创建时间倒序。

        存储层只存 ``ownerUid``（一串随机字符串），对用户毫无意义——所以在这里
        配上成员显示名。查不到（成员被删了 / 历史无主数据）就留空，前端据此
        显示「服务器管理员」。
        """
        events = await asyncio.to_thread(self._list_sync)
        by_uid = {m.uid: m for m in self._members}
        for item in events:
            item["current"] = item["id"] == self._current
            uid = str(item.get("ownerUid") or "")
            owner = by_uid.get(uid) if uid else None
            item["ownerName"] = owner.display_name if owner else ""
            # 头像也一起给：列表上只有名字时，一屏十几届根本认不出是谁办的
            item["ownerAvatar"] = (owner.avatar or "") if owner else ""
        return events

    async def read_event(self, event_id: str) -> Config:
        """只读载入任意一届（用于查看历史战绩，不影响当前届）。"""
        self._check_id(event_id)
        return await asyncio.to_thread(self._load_sync, event_id)

    async def event_count(self) -> int:
        return len(await self.list_events())

    # ------------------------------------------------------------------ #
    # 成员频道（日常直播）——全局，跨届共享
    # ------------------------------------------------------------------ #
    def channels(self) -> list[Channel]:
        """成员频道快照（全局，与当前届无关）。"""
        return self._channels

    @staticmethod
    def _channel_sort(channel: Channel) -> tuple[Any, ...]:
        return (not channel.featured, channel.sort, channel.id)

    async def save_channel(self, channel: Channel, actor: str = "api") -> Channel:
        """新增 / 更新一个成员频道（未给 id 时自动分配 ``c01`` 这类编号）。"""
        async with self._lock:
            if not channel.id:
                channel = channel.model_copy(update={"id": self._next_channel_id()})
            await asyncio.to_thread(self._save_channel_sync, channel.dump())
            rest = [c for c in self._channels if c.id != channel.id]
            self._channels = sorted([*rest, channel], key=self._channel_sort)
        log.warning(
            "成员频道已保存 | id=%s | 名称=%s | 流名=%s | 启用=%s",
            channel.id,
            channel.display_name,
            channel.stream_key or "(未设置)",
            channel.active,
        )
        await self._notify(self._config, f"channel:save:{actor}")
        return channel

    async def delete_channel(self, channel_id: str, actor: str = "api") -> bool:
        """删除一个成员频道；返回是否真的删掉了。"""
        async with self._lock:
            removed = await asyncio.to_thread(self._delete_channel_sync, channel_id)
            if removed:
                self._channels = [c for c in self._channels if c.id != channel_id]
        if removed:
            log.warning("成员频道已删除 | id=%s", channel_id)
            await self._notify(self._config, f"channel:delete:{actor}")
        return removed

    def channel_notice(self) -> str:
        """「频道」板块的公告 / 异环相关内容（全局，纯展示文案）。"""
        return self._channel_notice

    async def set_channel_notice(self, text: str, actor: str = "api") -> str:
        """设置频道公告（写入全局 meta，并广播一次）。"""
        clean = (text or "").strip()
        async with self._lock:
            await asyncio.to_thread(self._set_channel_notice_sync, clean)
            self._channel_notice = clean
        log.info("频道公告已更新 | 长度=%d", len(clean))
        await self._notify(self._config, f"channel:notice:{actor}")
        return clean

    def _load_channel_notice_sync(self) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, CHANNEL_NOTICE_KEY)

    def site_name(self) -> str:
        """站点名称（服务器管理员设定）；没设过就用默认值。"""
        return (self._site_name or "").strip() or DEFAULT_SITE_NAME

    async def set_site_name(self, name: str, actor: str = "api") -> str:
        """设置站点名称（写入全局 meta，并广播一次，让所有在线页面立刻换名字）。"""
        clean = " ".join((name or "").split())[:SITE_NAME_MAX]
        async with self._lock:
            await asyncio.to_thread(self._set_site_name_sync, clean)
            self._site_name = clean
        log.info("站点名称已更新 | %s", clean or f"(清空，回落默认 {DEFAULT_SITE_NAME})")
        await self._notify(self._config, f"site:name:{actor}")
        return self.site_name()

    def _load_site_name_sync(self) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, SITE_NAME_KEY)

    def _set_site_name_sync(self, name: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, SITE_NAME_KEY, name)

    def _set_channel_notice_sync(self, text: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, CHANNEL_NOTICE_KEY, text)

    def _next_channel_id(self) -> str:
        used = {c.id for c in self._channels}
        seq = 1
        while f"c{seq:02d}" in used:
            seq += 1
        return f"c{seq:02d}"

    def _load_channels_sync(self) -> list[Channel]:
        with db.connect(self._db_path) as conn:
            rows = db.list_channels(conn)
        return sorted((Channel.model_validate(row) for row in rows), key=self._channel_sort)

    def _save_channel_sync(self, row: dict[str, Any]) -> None:
        with db.connect(self._db_path) as conn:
            db.upsert_channel(conn, row)

    def _delete_channel_sync(self, channel_id: str) -> bool:
        with db.connect(self._db_path) as conn:
            return db.delete_channel(conn, channel_id)

    # ------------------------------------------------------------------ #
    # 成员（全局账号）
    #
    # 密钥与 Bearer 令牌只由服务端随机生成，明文只在生成 / 轮换的那一次回给
    # 调用方；库里只存 sha256，之后任何接口都取不回明文（只能轮换）。
    # ------------------------------------------------------------------ #
    def members(self) -> list[Member]:
        """成员列表快照（服务器管理员在前）。"""
        return self._members

    def member(self, uid: str) -> Member | None:
        return next((m for m in self._members if m.uid == uid), None)

    def member_by_stream_id(self, stream_id: str) -> Member | None:
        key = (stream_id or "").strip()
        if not key:
            return None
        return next((m for m in self._members if m.stream_id == key), None)

    def member_by_qq(self, qq: str) -> Member | None:
        """按 QQ 找成员（群里认人用：插件上报的 QQ → 站内身份）。

        **只在唯一命中时返回**：两位成员填了同一个 QQ 时返回 ``None``——那种情况下
        「这是谁」本身就有歧义，宁可当作查不到让对方先去改资料，也不能随便挑一个
        （挑错就等于把权限给了错的人）。歧义会记一条 warning 方便排查。
        """
        want = (qq or "").strip()
        if not want:
            return None
        hits = [m for m in self._members if (m.qq or "").strip() == want]
        if len(hits) > 1:
            log.warning(
                "QQ 对应多个成员，按「查不到」处理 | qq=%s | uid=%s",
                want,
                ", ".join(m.uid for m in hits),
            )
            return None
        return hits[0] if hits else None

    def member_by_key(self, key: str) -> Member | None:
        """按登录密钥定位成员。

        每条成员都有自己的随机盐，必须逐条校验（新格式是 HMAC，一次也就微秒级）。
        这里**不提前返回**——把整张表都过一遍，避免用耗时差异泄露「命中了第几条」。
        """
        raw = (key or "").strip()
        if not raw:
            return None
        found: Member | None = None
        for m in self._members:
            stored = m.key_stored
            if stored and verify_secret(raw, stored) and found is None:
                found = m
        return found

    def server_admin(self) -> Member | None:
        return next((m for m in self._members if m.permission == "server_admin"), None)

    def legacy_credential_members(self) -> list[Member]:
        """凭据还是**历史无盐格式**的成员（只能靠轮换升级，因为服务端拿不到明文）。"""
        return [m for m in self._members if m.legacy_credentials]

    def report_legacy_credentials(self) -> int:
        """把「还有几个成员的凭据是无盐旧格式」记进日志（管理端也会提示轮换）。"""
        legacy = self.legacy_credential_members()
        if legacy:
            log.warning(
                "有 %d 位成员的凭据仍是历史无盐格式（建议在成员管理里轮换一次，"
                "轮换后即为加盐哈希）：%s",
                len(legacy),
                "、".join(m.display_name for m in legacy[:5]) + ("…" if len(legacy) > 5 else ""),
            )
        return len(legacy)

    async def save_member(
        self, member: Member, *, new_key: bool = False, new_bearer: bool = False
    ) -> tuple[Member, str, str]:
        """新增 / 更新成员。

        返回 ``(成员, 新密钥明文, 新令牌明文)``；明文只在本次调用里生成，
        未轮换时为空字符串（调用方据此决定要不要「仅显示一次」地回给前端）。
        新建成员时密钥与令牌自动生成。
        """
        async with self._lock:
            current = self.member(member.uid) if member.uid else None
            data = member.model_copy()
            if not data.uid:
                data.uid = self._new_member_uid()
            gen_key = new_key or current is None
            gen_bearer = new_bearer or current is None
            key_plain = new_member_secret() if gen_key else ""
            bearer_plain = new_bearer_token() if gen_bearer else ""
            if current is not None:
                data.created_at = current.created_at or now_iso()
                data.key_hash = current.key_hash
                data.bearer_hash = current.bearer_hash
                data.key_sha256 = current.key_sha256
                data.bearer_sha256 = current.bearer_sha256
            else:
                data.created_at = data.created_at or now_iso()
            if key_plain:
                # 新凭据一律写成**加盐**格式，并清掉历史无盐值（避免两套并存）
                data.key_hash = hash_secret(key_plain)
                data.key_sha256 = ""
            if bearer_plain:
                data.bearer_hash = hash_secret(bearer_plain)
                data.bearer_sha256 = ""
            data.updated_at = now_iso()
            await asyncio.to_thread(self._save_member_sync, data)
            rest = [m for m in self._members if m.uid != data.uid]
            self._members = [*rest, data]
        log.warning(
            "成员已保存 | uid=%s | 名称=%s | 权限=%s | 流名=%s | 轮换密钥=%s | 轮换令牌=%s",
            data.uid,
            data.display_name,
            data.permission,
            data.stream_id or "(未设置)",
            bool(key_plain),
            bool(bearer_plain),
        )
        await self._notify(self._config, "member:save")
        return data, key_plain, bearer_plain

    async def delete_member(self, uid: str, actor: str = "api") -> bool:
        """删除成员；返回是否真的删掉了。"""
        async with self._lock:
            removed = await asyncio.to_thread(self._delete_member_sync, uid)
            if removed:
                self._members = [m for m in self._members if m.uid != uid]
        if removed:
            log.warning("成员已删除 | uid=%s", uid)
            await self._notify(self._config, f"member:delete:{actor}")
        return removed

    async def ensure_server_admin(self) -> Member:
        """启动自检：全站**有且只有一个**服务器管理员。

        没有就自动创建一个（随机密钥并打进启动日志）；多于一个时只保留最早
        创建的那个，其余降级为赛事管理员。这样「谁是管理员」不会成为空缺，
        也不会同时存在两个最高权限账号。
        """
        admins = [m for m in self._members if m.permission == "server_admin"]
        if len(admins) == 1:
            return admins[0]
        if len(admins) > 1:
            keep = min(admins, key=lambda m: (m.created_at, m.uid))
            for m in admins:
                if m.uid == keep.uid:
                    continue
                await self.save_member(m.model_copy(update={"permission": "event_admin"}))
                log.warning("检测到多个服务器管理员，已将「%s」降级为赛事管理员", m.display_name)
            return keep
        member = Member(uid=self._new_member_uid(), name="服务器管理员", permission="server_admin")
        saved, key_plain, _bearer_plain = await self.save_member(member)
        log.warning("-" * 68)
        log.warning("未检测到服务器管理员，已自动创建：%s", saved.display_name)
        log.warning("登录密钥（仅此一次显示，请立即保存）：%s", key_plain)
        log.warning("在 /admin（服务器管理）或 /user（个人）用该密钥登录；忘记可在成员管理里轮换。")
        log.warning("-" * 68)
        return saved

    # ------------------------------------------------------------------ #
    # 选手 ↔ 成员（「选手就是成员」）
    #
    # 成员是全局账号；历届的「选手」都对应一位成员：
    #   * 启动时一次性把历届已有选手迁移成成员（继承姓名 / QQ / 头像 / 游戏 UUID / 推流 ID），
    #     权限默认「成员」；
    #   * 新增 / 编辑选手时自动建号并关联；
    #   * 成员资料变更时反向同步到各届里关联的选手，保证两边一致。
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 计分口径升级（老数据：只有一个 metric 字段）
    # ------------------------------------------------------------------ #
    def migrate_scoring(self) -> int:
        """把只有旧口径 ``metric`` 的届落成「类型 / 标签 / 判断标准」，返回处理了几届。

        为什么要有这一步：新字段（``value_type`` / ``value_label`` / ``better``）
        出现之前的库只有 ``metric``。模型层本来就会按 ``metric`` 推导（读取无损），
        但那只是「读的时候假装有」——这里把它**真的写下来**，之后的一切都只看新字段。

        两条硬规矩：

        * **动手之前先留一份旧库快照**（见 :mod:`app.legacy`）。留不下来就不转换——
          数据本来也是无损可读的，宁可不转换，也不能在没有退路的情况下改库；
        * 无损：数值一个字节都不动，只是把「含义」拆成三个字段
          （``score`` → 自然数 + 得分 + 数值高胜，``time`` → 时间 + 用时 + 数值低胜）。
        """
        from . import legacy

        pending: list[tuple[str, dict[str, Any]]] = []
        for entry in self._list_sync():
            event_id = str(entry.get("id") or "")
            if not event_id:
                continue
            data = self._raw_sync(event_id)
            if data is None:
                continue
            rules = data.get("rules") or {}
            if str(rules.get("valueType") or "").strip():
                continue  # 已经是新口径，不必动
            pending.append((event_id, data))
        if not pending:
            return 0

        try:
            legacy.snapshot(
                reason="scoring-upgrade",
                note=f"计分口径升级前的旧数据（{len(pending)} 届待转换）",
            )
        except Exception:
            log.exception("旧数据快照失败，本次不转换（数据仍按旧口径无损读取）")
            return 0

        done = 0
        for event_id, data in pending:
            try:
                cfg = Config.model_validate(data)
                self._save_sync(event_id, cfg, touch_current=False)
                done += 1
            except Exception:
                log.exception("计分口径升级失败（该届保持原样）| 届=%s", event_id)
        if done:
            log.warning(
                "计分口径已升级 | 届=%d/%d | 旧数据快照见「服务器 → 旧数据备份」",
                done,
                len(pending),
            )
        return done

    async def migrate_players_to_members(self) -> int:
        """一次性迁移：历届所有选手 → 成员并建立关联（幂等，跑过即打标记）。"""
        async with self._lock:
            if await asyncio.to_thread(self._get_meta_sync, MEMBERS_MIGRATED_KEY) == "1":
                return 0
            entries = await asyncio.to_thread(self._list_sync)
            linked = 0
            for entry in entries:
                cfg = await asyncio.to_thread(self._load_sync, entry["id"])
                players = [p.dump() for p in cfg.players]
                changed = False
                for p in players:
                    member = await asyncio.to_thread(self._ensure_member_sync, p, create=True)
                    if member is None:
                        continue
                    linked += 1
                    if str(p.get("memberUid") or "") != member.uid:
                        p["memberUid"] = member.uid
                        changed = True
                if not changed:
                    continue
                data = cfg.dump()
                data["players"] = players
                data["revision"] = int(data.get("revision", 0)) + 1
                data["updatedAt"] = now_iso()
                updated = Config.model_validate(data)
                await asyncio.to_thread(self._save_sync, entry["id"], updated, False)
                if entry["id"] == self._current:
                    self._config = updated
            await asyncio.to_thread(self._set_meta_sync, MEMBERS_MIGRATED_KEY, "1")
        if linked:
            log.warning(
                "已将历届选手迁移为成员 | 关联选手=%d | 成员总数=%d", linked, len(self._members)
            )
            await self._notify(self._config, "members:migrate")
        return linked

    async def ensure_member_for_player(self, player: Player) -> Member | None:
        """确保选手有对应成员（无则按选手信息建号，权限默认「成员」），返回该成员。"""
        async with self._lock:
            return await asyncio.to_thread(self._ensure_member_sync, player.dump(), create=True)

    async def propagate_member(self, member: Member, actor: str = "api") -> int:
        """把成员资料同步到各届里关联的选手（改名 / 换头像 / 换推流 ID 后调用）。"""
        async with self._lock:
            changed = await asyncio.to_thread(self._propagate_member_sync, member)
        if changed:
            log.warning("成员资料已同步到关联选手 | uid=%s | 覆盖届数=%d", member.uid, changed)
            await self._notify(self._config, f"member:propagate:{actor}")
        return changed

    def _ensure_member_sync(self, player: dict[str, Any], *, create: bool = True) -> Member | None:
        """按「关联 uid → 游戏 UUID → 姓名+QQ」定位成员；找不到就在 create 时新建。

        已有成员只**补齐空缺字段**（不覆盖已填内容），避免反复覆盖全局资料。
        """
        from .logic import clean_key  # 局部导入，避免模块级循环依赖

        uid = str(player.get("memberUid") or "").strip()
        if uid:
            existing = self.member(uid)
            if existing is not None:
                return existing
        game_uuid = str(player.get("uuid") or "").strip()
        name = str(player.get("name") or "").strip()
        qq = str(player.get("qq") or "").strip()
        stream_key = clean_key(str(player.get("streamKey") or ""))

        found: Member | None = None
        for m in self._members:
            if game_uuid and m.game_uuid and m.game_uuid == game_uuid:
                found = m
                break
            if name and qq and m.name == name and m.qq == qq:
                found = m
                break
        if found is None and not create:
            return None
        if found is not None:
            patch: dict[str, Any] = {}
            if not found.qq and qq:
                patch["qq"] = qq
            if not found.avatar and player.get("avatar"):
                patch["avatar"] = str(player["avatar"])
            if not found.game_uuid and game_uuid:
                patch["game_uuid"] = game_uuid
            if (
                not found.stream_id
                and stream_key
                and not any(m.stream_id == stream_key for m in self._members if m.uid != found.uid)
            ):
                patch["stream_id"] = stream_key
            if patch:
                found = found.model_copy(update={**patch, "updated_at": now_iso()})
                self._save_member_sync(found)
                self._members = [found if m.uid == found.uid else m for m in self._members]
            return found

        stream_id = ""
        if stream_key and not any(m.stream_id == stream_key for m in self._members):
            stream_id = stream_key
        member = Member(
            uid=self._new_member_uid(),
            name=name or "成员",
            qq=qq,
            avatar=str(player.get("avatar") or ""),
            game_uuid=game_uuid,
            stream_id=stream_id,
            permission="member",
            active=bool(player.get("active", True)),
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self._save_member_sync(member)
        self._members = [*self._members, member]
        log.info(
            "选手已转为成员 | 名称=%s | uid=%s | 推流ID=%s",
            member.display_name,
            member.uid,
            member.stream_id or "(无)",
        )
        return member

    async def propagate_player(self, player: Player, actor: str = "api") -> int:
        """把**选手档案**的改动同步到关联成员（名字 / QQ / 头像 / 游戏 UUID）。

        与 :meth:`propagate_member` 是一对：那边是「改成员 → 各届关联选手」，
        这边是「改本届选手 → 成员」。两边都在**保存动作**里各写一次，不做互相回写，
        所以不会来回打架。以前只有成员 → 选手这一个方向，在「比赛选手」里改完名字
        成员那边还是旧的（两边看着像两个人）。

        两个例外：

        * **空值不倒灌**：选手档案里空着的字段不会把成员那边清掉（改个名字顺手抹掉
          QQ / UUID 是最难查的那种坏），要清空请到成员那一侧改；
        * **推流 ID**：必须全局唯一（两位成员撞同一个流名会串流），所以只在
          「成员还没有推流 ID、且这个名字没人占用」时才带过去。
        """
        from .logic import clean_key  # 局部导入，避免模块级循环依赖

        if not player.member_uid:
            return 0
        async with self._lock:
            member = self.member(player.member_uid)
            if member is None:
                return 0
            patch: dict[str, Any] = {}
            if player.name and member.name != player.name:
                patch["name"] = player.name
            # **空值不倒灌**：选手档案里没填的东西（头像 / QQ / UUID）不该把成员那边
            # 清掉——改个名字顺手把成员的 QQ 抹了是最难查的那种坏。要清空请到成员里改。
            if player.qq and member.qq != player.qq:
                patch["qq"] = player.qq
            if player.avatar and member.avatar != player.avatar:
                patch["avatar"] = player.avatar
            if player.uuid and member.game_uuid != player.uuid:
                patch["game_uuid"] = player.uuid
            key = clean_key(player.stream_key)
            if (
                key
                and not member.stream_id
                and not any(m.stream_id == key for m in self._members if m.uid != member.uid)
            ):
                patch["stream_id"] = key
            if not patch:
                return 0
            saved = member.model_copy(update={**patch, "updated_at": now_iso()})
            await asyncio.to_thread(self._save_member_sync, saved)
            self._members = [saved if m.uid == saved.uid else m for m in self._members]
        log.warning(
            "选手档案已同步到成员 | 选手=%s | uid=%s | 字段=%s",
            player.display_name,
            saved.uid,
            "、".join(sorted(patch)),
        )
        await self._notify(self._config, f"player:propagate:{actor}")
        return 1

    def _propagate_member_sync(self, member: Member) -> int:
        """把成员资料写回各届里与之关联的选手，返回被改动的届数。"""
        changed_events = 0
        for entry in self._list_sync():
            cfg = self._load_sync(entry["id"])
            players = [p.dump() for p in cfg.players]
            changed = False
            for p in players:
                if str(p.get("memberUid") or "") != member.uid:
                    continue
                patch: dict[str, Any] = {}
                if member.name and p.get("name") != member.name:
                    patch["name"] = member.name
                if p.get("qq") != member.qq:
                    patch["qq"] = member.qq
                if p.get("avatar") != member.avatar:
                    patch["avatar"] = member.avatar
                if p.get("uuid") != member.game_uuid:
                    patch["uuid"] = member.game_uuid
                # 推流流名跟随成员的推流 ID（选手的机位地址 = 成员的推流 ID）
                if member.stream_id and p.get("streamKey") != member.stream_id:
                    patch["streamKey"] = member.stream_id
                if patch:
                    p.update(patch)
                    changed = True
            if not changed:
                continue
            data = cfg.dump()
            data["players"] = players
            data["revision"] = int(data.get("revision", 0)) + 1
            data["updatedAt"] = now_iso()
            updated = Config.model_validate(data)
            self._save_sync(entry["id"], updated, False)
            if entry["id"] == self._current:
                self._config = updated
            changed_events += 1
        return changed_events

    def _get_meta_sync(self, key: str) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, key)

    def _set_meta_sync(self, key: str, value: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, key, value)

    # ------------------------------------------------------------------ #
    # 直播间封禁（全局）
    # ------------------------------------------------------------------ #
    def live_bans(self) -> list[LiveBan]:
        return self._live_bans

    async def add_live_ban(self, ban: LiveBan, actor: str = "api") -> LiveBan:
        """新增一条直播封禁记录（未给 id 时自动分配 ``b001`` 这类编号）。"""
        async with self._lock:
            if not ban.id:
                ban = ban.model_copy(update={"id": self._new_ban_id()})
            await asyncio.to_thread(self._save_live_ban_sync, ban)
            rest = [b for b in self._live_bans if b.id != ban.id]
            self._live_bans = [*rest, ban]
        log.warning(
            "直播已封禁 | id=%s | 范围=%s | 成员=%s | 流名=%s | 至=%s | 原因=%s",
            ban.id,
            ban.scope,
            ban.member_uid or "(按流名)",
            ban.stream_id or "(未指定)",
            ban.until or "永久",
            ban.reason or "(未填写)",
        )
        await self._notify(self._config, f"live:ban:{actor}")
        return ban

    async def remove_live_ban(self, ban_id: str, actor: str = "api") -> bool:
        """解除一条直播封禁（仅服务器管理员可调用，见接口层）。"""
        async with self._lock:
            removed = await asyncio.to_thread(self._delete_live_ban_sync, ban_id)
            if removed:
                self._live_bans = [b for b in self._live_bans if b.id != ban_id]
        if removed:
            log.warning("已解除直播封禁 | id=%s", ban_id)
            await self._notify(self._config, f"live:unban:{actor}")
        return removed

    # ------------------------------------------------------------------ #
    # 服务器级配置：QQ 机器人（AstrBot）推送
    #
    # 设置存在数据库 meta 里（跟着备份 / 还原走），其中 AstrBot 的 API Key 属于
    # 凭据：接口层只回「配没配」，绝不回明文。
    # ------------------------------------------------------------------ #
    def qqbot_settings(self) -> dict[str, Any]:
        """QQ 机器人推送配置快照（含 API Key，**只给服务端用**）。"""
        return dict(self._qqbot)

    async def set_qqbot(
        self, patch: dict[str, Any], actor: str = "api", *, internal: bool = False
    ) -> dict[str, Any]:
        """更新 QQ 机器人配置（只接受已知键；apiKey 传空串 = 不改）。

        ``internal=True`` 时才允许写**只由服务端生成**的字段（如查询 API 令牌哈希）。
        """
        from . import qqbot  # 局部导入，避免模块级循环依赖

        payload = {**(patch or {}), "__internal__": True} if internal else (patch or {})
        async with self._lock:
            merged = qqbot.normalize_settings(payload, self._qqbot)
            self._qqbot = merged
            await asyncio.to_thread(
                self._set_meta_sync, qqbot.QQBOT_KEY, json.dumps(merged, ensure_ascii=False)
            )
        log.warning(
            "QQ 机器人设置已更新 | 启用=%s | 目标=%s | @方式=%s",
            merged.get("enabled"),
            qqbot.resolved_umo(merged) or "(未设置)",
            merged.get("atMode"),
        )
        await self._notify(self._config, f"server:qqbot:{actor}")
        return merged

    def _load_qqbot_sync(self) -> dict[str, Any]:
        """读 QQ 机器人配置（JSON），坏数据一律回落到默认值。"""
        from . import qqbot

        with db.connect(self._db_path) as conn:
            raw = db.get_meta(conn, qqbot.QQBOT_KEY)
        if not raw:
            return dict(QQBOT_DEFAULTS)
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("QQ 机器人配置无法解析，已回落到默认值")
            return dict(QQBOT_DEFAULTS)
        return qqbot.merge_settings(data if isinstance(data, dict) else {})

    # ------------------------------------------------------------------ #
    # 服务器级配置：自定义 HTML（全局，用于接入统计 / 数据采集）
    # ------------------------------------------------------------------ #
    def custom_html(self) -> str:
        return self._custom_html

    async def set_custom_html(self, text: str, actor: str = "api") -> str:
        clean = (text or "").strip()
        async with self._lock:
            await asyncio.to_thread(self._set_custom_html_sync, clean)
            self._custom_html = clean
        log.info("自定义 HTML 已更新 | 长度=%d", len(clean))
        await self._notify(self._config, f"server:custom-html:{actor}")
        return clean

    # ------------------------------------------------------------------ #
    # 全局键值（meta 表）：给「提醒去重」这类小状态用
    # ------------------------------------------------------------------ #
    async def meta(self, key: str, default: str = "") -> str:
        """读一条全局键值。"""
        return await asyncio.to_thread(self._meta_sync, key, default)

    def _meta_sync(self, key: str, default: str) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, key, default)

    async def set_meta(self, key: str, value: str) -> None:
        """写一条全局键值（立即落库：进程重启后仍要记得「这条提醒已经发过」）。"""
        await asyncio.to_thread(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, key, value)
            conn.commit()

    # ------------------------------------------------------------------ #
    # 群消息投递队列：**真 @** 只能由 AstrBot 里的插件发（见 app/outbox.py）
    # ------------------------------------------------------------------ #
    async def push_enqueue(
        self, *, kind: str, body: str, mentions: list[str], umo: str = "", event_id: str = ""
    ) -> dict[str, Any]:
        """排队一条待投递的消息（``mentions`` = 要真 @ 的 QQ）。"""
        stamp = now_iso()
        item_id = f"n{secrets.token_hex(6)}"
        item = {
            "id": item_id,
            "createdAt": stamp,
            "updatedAt": stamp,
            "kind": kind,
            "eventId": event_id,
            "umo": umo,
            "mentions": [str(q) for q in (mentions or [])],
            "body": body,
            "status": "pending",
            "via": "",
            "attempts": 0,
            "detail": "",
        }
        async with self._lock:
            await asyncio.to_thread(self._outbox_insert_sync, item)
        return item

    def _outbox_insert_sync(self, item: dict[str, Any]) -> None:
        with db.connect(self._db_path) as conn:
            db.insert_outbox(
                conn,
                id=item["id"],
                kind=item["kind"],
                umo=item["umo"],
                mentions=item["mentions"],
                body=item["body"],
                event_id=item["eventId"],
                created_at=item["createdAt"],
            )
            conn.commit()

    async def push_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        """待投递的消息（老消息在前）——插件每隔几秒来取一次。"""
        return await asyncio.to_thread(self._outbox_pending_sync, limit)

    def _outbox_pending_sync(self, limit: int) -> list[dict[str, Any]]:
        with db.connect(self._db_path) as conn:
            return db.list_outbox(conn, limit=limit)

    async def push_item(self, item_id: str) -> dict[str, Any] | None:
        """按 id 取一条（插件回执说发不出去时，要拿它的正文与 @ 名单去退回重发）。"""
        return await asyncio.to_thread(self._outbox_item_sync, item_id)

    def _outbox_item_sync(self, item_id: str) -> dict[str, Any] | None:
        with db.connect(self._db_path) as conn:
            return db.get_outbox(conn, item_id)

    async def push_finish(self, item_id: str, *, status: str, via: str, detail: str = "") -> bool:
        """给一条消息收尾（``sent`` / ``fallback`` / ``failed``）。

        返回 ``False`` = 这条已经被别人收掉了（插件回执与站点超时可能同时发生）。
        """
        return await asyncio.to_thread(self._outbox_finish_sync, item_id, status, via, detail)

    def _outbox_finish_sync(self, item_id: str, status: str, via: str, detail: str) -> bool:
        with db.connect(self._db_path) as conn:
            done = db.finish_outbox(
                conn, item_id, status=status, via=via, detail=detail, updated_at=now_iso()
            )
            conn.commit()
            return done

    async def push_prune(self, keep: int = 50) -> int:
        """清掉旧的已收尾记录（未投递的永不清）。"""
        return await asyncio.to_thread(self._outbox_prune_sync, keep)

    def _outbox_prune_sync(self, keep: int) -> int:
        with db.connect(self._db_path) as conn:
            count = db.prune_outbox(conn, keep=keep)
            conn.commit()
            return count

    # ------------------------------------------------------------------ #
    # 服务器信息（站点级 Markdown）+ 公告（通知）
    # ------------------------------------------------------------------ #
    def server_info(self) -> str:
        """站点级说明（Markdown 原文，渲染在服务端做）。"""
        return self._server_info

    async def set_server_info(self, text: str, actor: str = "api") -> str:
        clean = str(text or "").strip()
        async with self._lock:
            await asyncio.to_thread(self._set_server_info_sync, clean)
            self._server_info = clean
        log.info("服务器信息已更新 | 长度=%d", len(clean))
        await self._notify(self._config, f"server:info:{actor}")
        return clean

    def notice_heads(self) -> dict[str, dict[str, Any] | None]:
        """「当前届」与「服务器级」各自最新一条公告的轻量信息（给状态广播用）。

        只带 id / 标题 / 时间，不带正文：状态每次变更都会广播给所有在线客户端，
        把正文塞进去等于让每次改比分都重发一遍公告。
        """
        return {
            "event": self._notice_head("event", self._current),
            "server": self._notice_head("server", ""),
        }

    def _notice_head(self, scope: str, event_id: str) -> dict[str, Any] | None:
        head = self._notice_heads.get(f"{scope}:{event_id}")
        return dict(head) if head else None

    async def list_notices(
        self, scope: str, event_id: str = "", page: int = 1, size: int = 4
    ) -> dict[str, Any]:
        """分页取公告（最新在前）。"""
        size = max(1, min(int(size or 4), 50))
        page = max(1, int(page or 1))
        items, total = await asyncio.to_thread(
            self._list_notices_sync, scope, event_id, size, (page - 1) * size
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "pages": max(1, (total + size - 1) // size),
        }

    async def get_notice(self, notice_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_notice_sync, notice_id)

    async def save_notice(
        self,
        *,
        scope: str,
        event_id: str = "",
        notice_id: str = "",
        title: str,
        body: str,
        author: str = "",
    ) -> dict[str, Any]:
        """新建或更新一条公告，并刷新内存里的「最新一条」。"""
        stamp = now_iso()
        nid = (notice_id or "").strip() or uuid.uuid4().hex[:12]
        existing = await asyncio.to_thread(self._get_notice_sync, nid) if notice_id else None
        row = {
            "id": nid,
            "scope": scope,
            "eventId": event_id,
            "title": (title or "").strip()[:120],
            "body": body or "",
            "author": author or (existing or {}).get("author", ""),
            "createdAt": (existing or {}).get("createdAt") or stamp,
            "updatedAt": stamp,
        }
        async with self._lock:
            await asyncio.to_thread(self._save_notice_sync, row)
            self._notice_heads = await asyncio.to_thread(self._load_notice_heads_sync)
        log.info(
            "公告已保存 | scope=%s | event=%s | id=%s | 标题=%s",
            scope,
            event_id or "-",
            nid,
            row["title"],
        )
        await self._notify(self._config, f"notice:save:{nid}")
        return row

    async def delete_notice(self, notice_id: str) -> bool:
        async with self._lock:
            removed = await asyncio.to_thread(self._delete_notice_sync, notice_id)
            self._notice_heads = await asyncio.to_thread(self._load_notice_heads_sync)
        if removed:
            log.warning("公告已删除 | id=%s", notice_id)
            await self._notify(self._config, f"notice:delete:{notice_id}")
        return removed

    # ------------------------------------------------------------------ #
    # 服务器级配置：登录失败限制（类 fail2ban）
    # ------------------------------------------------------------------ #
    def guard_settings(self) -> dict[str, Any]:
        """登录限制配置快照（含默认值）。"""
        return dict(self._guard)

    async def set_login_guard(self, patch: dict[str, Any], actor: str = "api") -> dict[str, Any]:
        """更新登录限制配置（只接受已知键）。"""
        clean = {k: v for k, v in (patch or {}).items() if k in GUARD_KEYS and v is not None}
        if not clean:
            raise ValueError("没有需要修改的配置项")
        async with self._lock:
            merged = {**self._guard, **clean}
            self._guard = merged
            await asyncio.to_thread(
                self._set_meta_sync, LOGIN_GUARD_KEY, json.dumps(merged, ensure_ascii=False)
            )
        log.warning("登录限制配置已更新 | %s", merged)
        await self._notify(self._config, f"server:login-guard:{actor}")
        return dict(merged)

    def _new_member_uid(self) -> str:
        used = {m.uid for m in self._members}
        while True:
            uid = uuid.uuid4().hex
            if uid not in used:
                return uid

    def _new_ban_id(self) -> str:
        used = {b.id for b in self._live_bans}
        seq = 1
        while f"b{seq:03d}" in used:
            seq += 1
        return f"b{seq:03d}"

    def _load_members_sync(self) -> list[Member]:
        with db.connect(self._db_path) as conn:
            rows = db.list_members(conn)
        return [Member.model_validate(row) for row in rows]

    def _save_member_sync(self, member: Member) -> None:
        with db.connect(self._db_path) as conn:
            db.upsert_member(conn, member.dump())

    def _delete_member_sync(self, uid: str) -> bool:
        with db.connect(self._db_path) as conn:
            return db.delete_member(conn, uid)

    def _load_live_bans_sync(self) -> list[LiveBan]:
        with db.connect(self._db_path) as conn:
            rows = db.list_live_bans(conn)
        return [LiveBan.model_validate(row) for row in rows]

    def _save_live_ban_sync(self, ban: LiveBan) -> None:
        with db.connect(self._db_path) as conn:
            db.upsert_live_ban(conn, ban.dump())

    def _delete_live_ban_sync(self, ban_id: str) -> bool:
        with db.connect(self._db_path) as conn:
            return db.delete_live_ban(conn, ban_id)

    def _load_custom_html_sync(self) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, CUSTOM_HTML_KEY)

    def _set_custom_html_sync(self, text: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, CUSTOM_HTML_KEY, text)

    def _load_server_info_sync(self) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, SERVER_INFO_KEY)

    def _set_server_info_sync(self, text: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, SERVER_INFO_KEY, text)

    def _load_notice_heads_sync(self) -> dict[str, dict[str, Any]]:
        with db.connect(self._db_path) as conn:
            return db.notice_heads(conn)

    def _list_notices_sync(
        self, scope: str, event_id: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        with db.connect(self._db_path) as conn:
            return db.list_notices(
                conn, scope=scope, event_id=event_id, limit=limit, offset=offset
            )

    def _get_notice_sync(self, notice_id: str) -> dict[str, Any] | None:
        with db.connect(self._db_path) as conn:
            return db.get_notice(conn, notice_id)

    def _save_notice_sync(self, row: dict[str, Any]) -> None:
        with db.connect(self._db_path) as conn:
            db.save_notice(conn, row)

    def _delete_notice_sync(self, notice_id: str) -> bool:
        with db.connect(self._db_path) as conn:
            return db.delete_notice(conn, notice_id)

    def _load_login_guard_sync(self) -> dict[str, Any]:
        """读登录限制配置（JSON），坏数据一律回落到默认值。"""
        with db.connect(self._db_path) as conn:
            raw = db.get_meta(conn, LOGIN_GUARD_KEY)
        settings = dict(GUARD_DEFAULTS)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    settings.update({k: v for k, v in data.items() if k in GUARD_KEYS})
            except ValueError:
                log.warning("登录限制配置无法解析，已回落到默认值")
        return settings

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def reload(self, reason: str = "manual") -> Config:
        """从数据库重新载入当前届配置。"""
        async with self._lock:
            cfg = await asyncio.to_thread(self._load_sync, self._current)
            self._config = cfg
            log.info("配置载入完成 | reason=%s | 届=%s | revision=%d", reason, self._current, cfg.revision)
        await self._notify(cfg, f"reload:{reason}")
        return cfg

    async def reload_all(self, reason: str = "manual") -> Config:
        """把**全部**持久化状态重新读进内存（还原备份之后调用）。

        比 :meth:`reload` 多覆盖：当前届 ID、届次结构升级、成员 / 封禁 / 频道 /
        公告 / 自定义 HTML / 登录限制——还原后库里的「当前届」很可能跟内存里的
        不是同一届，只重载当前届是不够的。
        """
        async with self._lock:
            await asyncio.to_thread(db.init_db, self._db_path)
            await asyncio.to_thread(self._migrate_sync)
            current = await asyncio.to_thread(self._read_current_sync)
            if not current:
                current = await asyncio.to_thread(self._create_blank_sync)
            self._current = current
            self._config = await asyncio.to_thread(self._load_sync, current)
            self._channels = await asyncio.to_thread(self._load_channels_sync)
            self._members = await asyncio.to_thread(self._load_members_sync)
            self._live_bans = await asyncio.to_thread(self._load_live_bans_sync)
            self._channel_notice = await asyncio.to_thread(self._load_channel_notice_sync)
            self._site_name = await asyncio.to_thread(self._load_site_name_sync)
            self._custom_html = await asyncio.to_thread(self._load_custom_html_sync)
            self._server_info = await asyncio.to_thread(self._load_server_info_sync)
            self._notice_heads = await asyncio.to_thread(self._load_notice_heads_sync)
            self._guard = await asyncio.to_thread(self._load_login_guard_sync)
            self._qqbot = await asyncio.to_thread(self._load_qqbot_sync)
            await self.ensure_server_admin()
            self.report_legacy_credentials()
        log.warning(
            "已从数据库全量重载 | reason=%s | 届=%s | 成员=%d",
            reason,
            self._current,
            len(self._members),
        )
        await self._notify(self._config, f"reload-all:{reason}")
        return self._config

    async def update(self, patch: dict[str, Any], actor: str = "api") -> Config:
        """按 patch 合并更新（字典深合并、列表替换），并落盘。"""

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            merged = deep_merge(data, patch)
            merged["revision"] = int(data.get("revision", 0)) + 1
            merged["updatedAt"] = now_iso()
            return merged

        return await self.mutate(_mutate, actor=actor)

    @staticmethod
    def _resolve(data: dict[str, Any]) -> dict[str, Any]:
        """按比赛进程重算淘汰赛阵容（幂等，仅锦标赛制）。

        上游结果被改写时，下游对局会在同一次前向遍历中自动作废，
        因此任何写入路径都不可能留下「对阵与结果不一致」的状态。
        积分制（league）的阵容由换人接口维护，不走这里。
        """
        if data.get("rules", {}).get("format", "tournament") != "tournament":
            return data
        if not data.get("rounds") or not data.get("teams"):
            return data
        cfg = Config.model_validate(data)
        dumped = [
            r.dump()
            for r in tournament.resolve_tournament(cfg.teams, cfg.rounds, cfg.rules.scoring)
        ]
        if dumped == data.get("rounds"):
            return data
        return {**data, "rounds": dumped}

    async def mutate(
        self,
        mutator: Mutator,
        actor: str = "api",
        *,
        resolve: bool = True,
        final: Finalizer | None = None,
    ) -> Config:
        """在锁内执行一次事务式修改。mutator 接收 dict，返回新的 dict。

        ``final`` 在 ``resolve``（上下游阵容自动重算）之后执行，用于「必须看到
        最终结果才能定」的收尾，例如总冠军决出后自动把本届标记为已结束。
        """
        async with self._lock:
            data = self._config.dump()
            try:
                updated = mutator(data)
                if resolve:
                    updated = self._resolve(updated)
                if final is not None:
                    updated = final(updated)
            except Exception:
                log.exception("配置修改失败 | actor=%s", actor)
                raise
            updated["revision"] = int(data.get("revision", 0)) + 1
            updated["updatedAt"] = now_iso()
            cfg = Config.model_validate(updated)
            await asyncio.to_thread(self._save_sync, self._current, cfg)
            self._config = cfg
            log.info(
                "配置已更新 | 届=%s | actor=%s | revision=%d | 选手=%d | 局数=%d",
                self._current,
                actor,
                cfg.revision,
                len(cfg.players),
                len(cfg.rounds),
            )
        await self._notify(cfg, f"update:{actor}")
        return cfg

    async def mutate_event(
        self, event_id: str, mutator: Mutator, actor: str = "api", *, resolve: bool = False
    ) -> Config:
        """在**指定届**上做一次事务式修改（不必是当前届，也不会挪走「当前届」指针）。

        群里的报名 / 取消报名按命令里写的届次写入（`比赛报名 e003`），与「谁最近打开过
        就是当前届」那个指针无关：走 :meth:`switch_event` 会把所有人的视图一起切走，
        副作用太大，不能用在「给某一届报个名」上。当前届则直接复用 :meth:`mutate`。
        """
        self._check_id(event_id)
        if event_id == self._current:
            return await self.mutate(mutator, actor=actor, resolve=resolve)
        async with self._lock:
            cfg = await asyncio.to_thread(self._load_sync, event_id)
            data = cfg.dump()
            updated = mutator(data)
            if resolve:
                updated = self._resolve(updated)
            updated["revision"] = int(data.get("revision", 0)) + 1
            updated["updatedAt"] = now_iso()
            fresh = Config.model_validate(updated)
            await asyncio.to_thread(self._save_sync, event_id, fresh, False)
        log.warning(
            "届次已更新（非当前届）| 届=%s | actor=%s | revision=%d", event_id, actor, fresh.revision
        )
        # **不广播**：广播用的状态取自当前届（``main.build_public_state``），把别的届推给
        # 正在看当前届的人只会错位——``update_event_meta`` 对非当前届也是这个取舍。
        return fresh

    async def close_finished_events(self, actor: str = "startup:close-finished") -> list[str]:
        """把「其实已经打完、状态却还停在进行中」的届补记成「已结束」。

        为什么需要这一步：自动结束挂在**写入**上（见 :func:`app.logic.close_on_champion`），
        只在「最后一笔结果落库的那一刻」生效。于是两种情况会漏：

        * 在补上这条规则**之前**就打完了的届（用户报的就是这个：页面上「已结束 ·
          赛程 10/10 场」，而届状态下拉里还写着「进行中」，自相矛盾）；
        * 升级上来的老数据（最后一笔结果早写完了）。

        启动时跑一次即可，**幂等**：只碰「非已结束且赛程确实打完」的届。

        个别情况下管理员手动「恢复进行」想把这一届留着，再重启时又会被补回「已结束」
        ——这与「录一笔新结果也会自动关上」是同一条口径（赛程确实打完了）。
        真要留着进行状态，动一下赛程或名单即可（那样它就不算「打完」了）。
        """
        from . import logic  # 局部导入，避免模块级循环依赖

        closed: list[str] = []
        for entry in await self.list_events():
            event_id = str(entry.get("id") or "")
            if not event_id or str(entry.get("status") or "") == "closed":
                continue
            try:
                cfg = await self.read_event(event_id)
            except (FileNotFoundError, ValueError):
                continue
            if not logic.season_finished(cfg):
                continue
            await self.mutate_event(event_id, logic.close_on_champion, actor=actor)
            closed.append(event_id)
        if closed:
            log.warning("已补记「已结束」的届 | %s", ", ".join(closed))
        return closed

    # ------------------------------------------------------------------ #
    # 本届参与名单 / 组队 / 赛程
    # ------------------------------------------------------------------ #
    async def set_participants(self, ids: list[str], actor: str = "api") -> tuple[Config, list[str]]:
        """设定本届参与名单，并按赛制自动跟进。

        * 积分制：名单变化后可在同一事务内重排未开赛对局（人数不足则整体拒绝）；
        * 锦标赛制：只改名单，提示需要重新组队并生成赛程；
        * **空名单是合法状态**（本届没有参与者）：不重排、不报错，现有赛程原样留着，
          由诊断面板提示「未开赛对局里还有非参与选手」。
        """
        from .logic import joined_players, normalize_participants  # 局部导入，避免模块级循环依赖

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            before = {p.id for p in joined_players(cfg)}
            chosen = normalize_participants(cfg, ids)
            # 显式保存过就置位：此后「空名单」也是一份真正的名单（不再回落成全员参与）
            merged = {**data, "participants": chosen, "participantsSet": True}
            return _follow_roster_change(cfg, merged, before, warnings)

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning(
            "已更新本届参与名单 | 届=%s | 参与=%d/%d 人",
            self._current,
            len(cfg.participants),
            len(cfg.players),
        )
        return cfg, warnings

    async def adopt_members(
        self,
        member_uids: list[str],
        player_ids: list[str],
        actor: str = "api",
    ) -> tuple[Config, list[str], list[str]]:
        """把**勾选的成员**纳入本届参赛：缺档案的按成员资料建好，然后写参与名单。

        这就是「本届名单从成员列表来」的那一步——勾一个成员，他就参加这一届：

        * 该成员本届**已有档案** → 直接用，并顺手把姓名 / QQ / 头像 / 游戏 UUID
          同步成成员资料里的最新值（选手档案只是成员在本届的一份投影）；
        * **还没有档案** → 按成员资料建一个（id 沿用 ``p01`` 这套编号）；
        * ``player_ids``：前端另外勾选的**非成员选手**（手工登记的客串），
          一并并入名单——否则一次保存就会把他们从参与名单里挤出去。

        返回 ``(配置, 新建的选手 ID, 提示)``。
        """
        from .logic import joined_players  # 局部导入，避免模块级循环

        by_uid = {m.uid: m for m in self._members}
        warnings: list[str] = []
        created: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            before = {p.id for p in joined_players(cfg)}
            chosen = [pid for pid in player_ids if pid]
            for uid in dict.fromkeys(member_uids):
                member = by_uid.get(uid)
                if member is None:
                    continue
                pid, is_new = _ensure_player(data, member)
                if is_new:
                    created.append(pid)
                chosen.append(pid)
            # 名单要按**新建之后的**报名池校验与排序（拿旧 cfg 会把刚建的人当不存在丢掉）
            return _follow_roster_change(cfg, _write_roster(data, chosen), before, warnings)

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning(
            "已按成员列表更新参赛名单 | 届=%s | 勾选成员=%d | 新建档案=%d | 参与=%d/%d 人",
            self._current,
            len(member_uids),
            len(created),
            len(cfg.participants),
            len(cfg.players),
        )
        return cfg, created, warnings

    async def sign_up(self, event_id: str, member: Member, *, actor: str = "bot:signup") -> dict[str, Any]:
        """把成员加进**指定届**的参赛名单（机器人自助报名 / 网站报名共用这一份）。

        **只定「谁来打」**：不动成员档案、不组队、不生成赛程——组队与赛制留给管理员
        在网站上做（报名期本来就只该定名单，见 :func:`signup_blocked` 的两条闸门）。

        返回 ``{"cfg", "playerId", "already", "created", "warnings"}``：
        ``already=True`` 表示他本来就在名单里（这一次一个字都没写，revision 也不动）。
        """
        from .logic import joined_players  # 局部导入，避免模块级循环

        self._check_id(event_id)
        before_cfg = await self.read_event(event_id)
        blocked = signup_blocked(before_cfg)
        if blocked:
            raise ValueError(blocked)
        hit = next((p for p in before_cfg.players if p.member_uid == member.uid), None)
        if hit is not None and hit.id in _roster_ids(before_cfg):
            return {
                "cfg": before_cfg,
                "playerId": hit.id,
                "already": True,
                "created": False,
                "warnings": [],
            }

        warnings: list[str] = []
        made: dict[str, Any] = {"playerId": "", "created": False}

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            before = {p.id for p in joined_players(cfg)}
            pid, is_new = _ensure_player(data, member)
            made["playerId"], made["created"] = pid, is_new
            # 从未显式定过名单 = 「全员参与」：先把现有的人落成显式名单，再加报名的人
            # （否则第一个人报名会把原来的全员挤出名单——见 _write_roster）
            roster = [p.id for p in joined_players(cfg)]
            return _follow_roster_change(cfg, _write_roster(data, [*roster, pid]), before, warnings)

        cfg = await self.mutate_event(event_id, _mutate, actor=actor)
        log.warning(
            "已报名 | 届=%s | 成员=%s | 选手=%s | 新建档案=%s | 参与=%d/%d 人",
            event_id,
            member.uid,
            made["playerId"],
            made["created"],
            len(cfg.participants),
            len(cfg.players),
        )
        return {
            "cfg": cfg,
            "playerId": made["playerId"],
            "already": False,
            "created": made["created"],
            "warnings": warnings,
        }

    async def cancel_signup(
        self, event_id: str, member: Member, *, actor: str = "bot:cancel-signup"
    ) -> dict[str, Any]:
        """把成员从**指定届**的参赛名单里去掉（机器人取消报名）。

        **只取消这一届的参赛资格**：成员档案留着，本届的选手档案也留着（名字 / 头像 /
        游戏 UUID 这些资料不是报名的一部分），队伍与赛程一个字都不动。

        返回与 :meth:`sign_up` 同构；``already=True`` 表示他本来就不在名单里。
        """
        from .logic import joined_players  # 局部导入，避免模块级循环

        self._check_id(event_id)
        before_cfg = await self.read_event(event_id)
        blocked = signup_blocked(before_cfg)
        if blocked:
            raise ValueError(blocked)
        hit = next((p for p in before_cfg.players if p.member_uid == member.uid), None)
        roster = _roster_ids(before_cfg)
        if hit is None or hit.id not in roster:
            return {
                "cfg": before_cfg,
                "playerId": hit.id if hit else "",
                "already": True,
                "created": False,
                "warnings": [],
            }

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            before = {p.id for p in joined_players(cfg)}
            keep = [pid for pid in _roster_ids(cfg) if pid != hit.id]
            return _follow_roster_change(cfg, _write_roster(data, keep), before, warnings)

        cfg = await self.mutate_event(event_id, _mutate, actor=actor)
        log.warning(
            "已取消报名 | 届=%s | 成员=%s | 选手=%s | 参与=%d/%d 人",
            event_id,
            member.uid,
            hit.id,
            len(cfg.participants),
            len(cfg.players),
        )
        return {
            "cfg": cfg,
            "playerId": hit.id,
            "already": False,
            "created": False,
            "warnings": warnings,
        }

    async def form_teams(
        self,
        seed: int | None = None,
        *,
        team_size: int | None = None,
        merge_remainder: bool = False,
        actor: str = "api",
    ) -> tuple[Config, list[str]]:
        """按本届参与名单随机分配队友，生成固定队伍，并清空原赛程。

        ``team_size`` 可临时覆盖每队人数（默认取赛制配置），
        ``merge_remainder`` 把凑不满整队的零头平均并入前排队伍。
        """
        from .logic import joined_players  # 局部导入，避免模块级循环依赖

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            size = int(team_size) if team_size else cfg.rules.team_size
            teams, warns = tournament.auto_form_teams(
                joined_players(cfg),
                size,
                seed,
                merge_remainder=merge_remainder,
            )
            warnings.extend(warns)
            rules = dict(data.get("rules") or {})
            if team_size:
                rules["teamSize"] = size
            return {**data, "rules": rules, "teams": [t.dump() for t in teams], "rounds": []}

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning(
            "已随机组队 | 届=%s | 队伍=%d | 每队=%d 人 | 零头并入=%s",
            self._current,
            len(cfg.teams),
            cfg.rules.team_size,
            merge_remainder,
        )
        return cfg, warnings

    async def generate_tournament(
        self,
        seed: int | None = None,
        size: int | None = None,
        *,
        teams_per_match: int | None = None,
        loser_bracket: bool | None = None,
        reform: bool = False,
        team_size: int | None = None,
        group_count: int | None = None,
        actor: str = "api",
    ) -> tuple[Config, list[str]]:
        """生成完整赛程：小组赛轮转 + 淘汰赛（覆盖现有对局与比分）。

        * ``size`` 淘汰赛规模（2 的幂，不超过队伍数）；
        * ``teams_per_match``（2/3/4）小组赛每场同场队伍数；
        * ``loser_bracket`` 双败 / 单败；
        * ``reform`` 先**重新随机组队**再排赛程（「快速创建分组」用）；
        * ``team_size`` / ``group_count`` 覆盖每队人数与小组数（0 = 自动）。

        以上都会写回赛制配置，保证下次打开表单看到的是实际生效的参数。
        """
        from .logic import joined_players  # 局部导入，避免模块级循环依赖

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            overrides: dict[str, Any] = {}
            if size:
                overrides["knockoutSize"] = int(size)
            if teams_per_match:
                overrides["teamsPerMatch"] = int(teams_per_match)
            if loser_bracket is not None:
                overrides["loserBracket"] = bool(loser_bracket)
            if team_size:
                overrides["teamSize"] = int(team_size)
            if group_count is not None:
                overrides["groupCount"] = int(group_count)
            if overrides:
                data = {**data, "rules": {**data.get("rules", {}), **overrides}}
            cfg = Config.model_validate(data)
            teams = list(cfg.teams)
            if reform or not teams:
                teams, warns = tournament.auto_form_teams(
                    joined_players(cfg),
                    cfg.rules.team_size,
                    seed,
                )
                warnings.extend(warns)
            if size:
                # 用户显式指定的规模要严格校验，非法直接报错（配置里的旧偏好则自动回退）
                tournament.resolve_size(len(teams), int(size))
            rounds, warns, _summary = tournament.build_tournament(teams, cfg.rules)
            warnings.extend(warns)
            return {
                **data,
                "teams": [t.dump() for t in teams],
                "rounds": [r.dump() for r in rounds],
            }

        cfg = await self.mutate(_mutate, actor=actor)
        log.warning(
            "已生成赛程 | 届=%s | 队伍=%d | 对局=%d",
            self._current,
            len(cfg.teams),
            len(cfg.rounds),
        )
        return cfg, warnings

    async def generate_league_schedule(
        self,
        mode: str = "rotate",
        total_rounds: int | None = None,
        seed: int | None = None,
        actor: str = "api",
    ) -> tuple[Config, list[str]]:
        """积分制：生成动态轮换（或固定队伍）赛程，覆盖现有对局与比分。"""
        from .logic import joined_players, normalize_participants  # 局部导入，避免模块级循环依赖

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            from .logic import has_custom_roster  # 局部导入，避免模块级循环依赖

            cfg = Config.model_validate(data)
            # 未指定参与名单时按「全部启用选手」落成显式名单，避免后续歧义
            if not has_custom_roster(cfg):
                data = {
                    **data,
                    "participants": normalize_participants(cfg, [p.id for p in joined_players(cfg)]),
                    "participantsSet": True,
                }
                cfg = Config.model_validate(data)
            rounds, warns = league.generate_schedule(
                cfg, mode=mode, total_rounds=total_rounds, seed=seed
            )
            warnings.extend(warns)
            merged = {**data, "rounds": [r.dump() for r in rounds]}
            if total_rounds:
                merged["rules"] = {**merged.get("rules", {}), "totalRounds": len(rounds)}
            return merged

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning(
            "已生成积分制赛程 | 届=%s | 模式=%s | 局数=%d",
            self._current,
            mode,
            len(cfg.rounds),
        )
        return cfg, warnings

    async def append_league_rounds(
        self, count: int = 1, seed: int | None = None, actor: str = "api"
    ) -> tuple[Config, list[str]]:
        """积分制：为出场次数最少的选手追加补赛（不影响已有比分）。"""
        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            extra, warns = league.append_rounds(cfg, count=count, seed=seed)
            warnings.extend(warns)
            rounds = data.setdefault("rounds", [])
            rounds.extend([r.dump() for r in extra])
            for pos, rnd in enumerate(rounds, start=1):
                rnd["index"] = pos
                rnd["code"] = f"L-{pos}"
                rnd["slot"] = pos
                rnd["label"] = ""      # 交回 round_view 按新序号生成
            return data

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning("已追加补赛 | 届=%s | 新增=%d | 现有=%d", self._current, count, len(cfg.rounds))
        return cfg, warnings

    async def clear_rounds(self, actor: str = "api") -> Config:
        """清空**全部比赛**（保留队伍与名单），两套赛制通用；比分一并丢弃。"""
        before = len(self._config.rounds)

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "rounds": []}

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning("已清空全部比赛 | 届=%s | 原场次=%d", self._current, before)
        return cfg

    async def clear_tournament(self, actor: str = "api") -> Config:
        """清空赛程（保留队伍），用于重新编排。与 :meth:`clear_rounds` 同义。"""
        return await self.clear_rounds(actor=actor)

    # ------------------------------------------------------------------ #
    # 届次管理
    # ------------------------------------------------------------------ #
    async def create_event(
        self,
        name: str,
        copy_roster: bool = False,
        fmt: str = "",
        owner_uid: str = "",
    ) -> Config:
        """新建一届并切换过去；可选沿用当前届的名单、队伍、规则与界面配置，并指定赛制。

        ``fmt`` 留空时按类型自动选：排名模式用锦标赛制，娱乐（不排名）用积分制
        —— 娱乐赛事没有「晋级」可言，锦标赛制跑不起来。
        """
        async with self._lock:
            template = default_config()
            current = self._config.dump()
            if copy_roster:
                template["players"] = current.get("players", [])
                template["participants"] = current.get("participants", [])
                # 连「名单是显式指定的」这一点一起沿用：否则空名单会在新一届里变回全员
                template["participantsSet"] = bool(
                    current.get("participantsSet") or current.get("participants")
                )
                # 沿用固定队伍（积分制的「固定队伍」模式也依赖它）；
                # 新一届没有赛程，可随时在组队台重新随机
                template["teams"] = current.get("teams", [])
                for key in ("rules", "stream", "ui"):
                    if key in current:
                        template[key] = current[key]
                # 比赛类型与排名开关也跟着沿用（同一类赛事通常连着办好几届）
                for key in ("sport", "ranked"):
                    if key in current.get("event", {}):
                        template["event"][key] = current["event"][key]
            # 赛制以新建时选择的为准（覆盖沿用的规则）；未指定时按排名模式挑一个合理的
            ranked = bool(template["event"].get("ranked", True))
            resolved_fmt = fmt if fmt in ("league", "tournament") else ("tournament" if ranked else "league")
            template["rules"] = {**template.get("rules", {}), "format": resolved_fmt}
            events = await asyncio.to_thread(self._list_sync)
            clean_name = (name or "").strip() or f"第 {len(events) + 1} 届"
            template["event"]["name"] = clean_name
            template["event"]["status"] = "active"
            # 记录归属：赛事管理员只能管理 / 删除自己创建的届
            template["event"]["ownerUid"] = owner_uid
            if clean_name:
                template["event"]["title"] = clean_name
            if template.get("participants"):
                from .logic import normalize_participants  # 局部导入，避免模块级循环依赖

                template["participants"] = normalize_participants(
                    Config.model_validate(template), template["participants"]
                )
            cfg = Config.model_validate(template)
            event_id = await asyncio.to_thread(self._insert_sync, cfg)
            self._current = event_id
            self._config = cfg
            log.warning("新建赛事届 | id=%s | 名称=%s | 沿用名单=%s", event_id, clean_name, copy_roster)
        await self._notify(cfg, "event:create")
        return cfg

    async def duplicate_event(self, source_id: str, name: str = "", owner_uid: str = "") -> Config:
        """复制一届：**配置与名单照抄，成绩与时间清空**，新届是「筹备中」。

        为什么成绩必须清：新届要能重新打（复制过来的届就是拿来再办一届的）。带着上一届的
        比分复制只会有两种下场——要么一出生就被「打完自动结束本届」判成已结束，要么顶着
        「筹备中」却已经有冠军，两种都自相矛盾。所以：

        * **照抄**：赛制与规则、选手与参与名单（连「名单是显式指定的」这一点一起）、队伍、
          赛程骨架（对阵、席位来源、每场的人与队伍）、比赛类型与排名开关、直播与界面配置、
          届名之外的展示信息（简介 / 场馆 / 主办 / 副标题 / 规则文案 / logo 文字）；
        * **清空**：每一场回到「未开始」（比分 / 胜者 / 各轮成绩 / 名次 / 弃权 / 用时 /
          起止时间 / 锁定）、开赛与结束时间、场次计划时间、**替补登记**（它按对局编号生效，
          而新届一场都没打）、开赛锁定（``locked``，否则新届一进来就是只读的）；
        * **不带过来**：届上的**公告**（挂在外面的通知带时间与作者，照抄是误导——要留在
          新届重发一条）；
        * 新届**归属操作者**（与新建一致），建完**立刻切换过去**，省得用户自己去列表里找。

        源届**一个字节都不改**（只读它）。
        """
        self._check_id(source_id)
        async with self._lock:
            if source_id == self._current:
                src = self._config
            else:
                src = await asyncio.to_thread(self._load_sync, source_id)
            data = src.dump()
            event = dict(data.get("event") or {})
            source_name = str(event.get("name") or "").strip()
            clean_name = (name or "").strip() or f"{source_name or source_id} 副本"
            # 标题若是「跟着届名自动填的」（新建时就是这么填的），就跟着新届名走；
            # 手工起过标题的（与届名不同）原样保留——那是用户特意写的文案。
            if not str(event.get("title") or "").strip() or event.get("title") == source_name:
                event["title"] = clean_name
            event.update(
                {
                    "name": clean_name,
                    # 筹备中：与报名闸门认的是同一个状态（见 SIGNUP_EVENT_STATUS）
                    "status": "draft",
                    "ownerUid": owner_uid,
                    "hidden": False,
                    "startTime": "",
                    "endTime": "",
                    "locked": False,
                    "lockedAt": "",
                }
            )
            data.update(
                {
                    "event": event,
                    "rounds": [_round_without_result(row) for row in data.get("rounds") or []],
                    "substitutions": [],
                    "revision": 0,
                    "updatedAt": now_iso(),
                }
            )
            cfg = Config.model_validate(data)
            event_id = await asyncio.to_thread(self._insert_sync, cfg)
            self._current = event_id
            self._config = cfg
            log.warning(
                "已复制届次 | 源=%s | 新=%s | 名称=%s | 场次=%d | 选手=%d",
                source_id,
                event_id,
                clean_name,
                len(cfg.rounds),
                len(cfg.players),
            )
        await self._notify(cfg, "event:copy")
        return cfg

    async def switch_event(self, event_id: str) -> Config:
        """切换当前届。"""
        self._check_id(event_id)
        async with self._lock:
            if event_id == self._current:
                return self._config
            cfg = await asyncio.to_thread(self._load_sync, event_id)
            self._current = event_id
            self._config = cfg
            await asyncio.to_thread(self._set_current_sync, event_id)
            log.warning("已切换赛事届 | id=%s | 名称=%s", event_id, cfg.event.name or cfg.event.title)
        await self._notify(cfg, "event:switch")
        return cfg

    async def update_event_meta(self, event_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """修改某一届的名称 / 状态；当前届走常规更新以触发广播。"""
        self._check_id(event_id)
        clean = {
            k: v
            for k, v in patch.items()
            if k in {"name", "status", "hidden", "sport", "ranked"} and v not in (None, "")
        }
        if not clean:
            raise ValueError("没有需要修改的字段")
        if event_id == self._current:
            await self.update({"event": clean}, actor="web:event-meta")
        else:
            async with self._lock:
                cfg = await asyncio.to_thread(self._load_sync, event_id)
                data = cfg.dump()
                data["event"] = {**data.get("event", {}), **clean}
                data["revision"] = int(data.get("revision", 0)) + 1
                data["updatedAt"] = now_iso()
                updated = Config.model_validate(data)
                await asyncio.to_thread(self._save_sync, event_id, updated, False)
                log.warning("已更新届次信息 | id=%s | %s", event_id, clean)
        entry = next((e for e in await self.list_events() if e["id"] == event_id), None)
        return entry or {}

    async def delete_event(self, event_id: str) -> Config:
        """删除一届；若删的是当前届则自动切到最近的一届（至少保留一届）。"""
        self._check_id(event_id)
        async with self._lock:
            events = await asyncio.to_thread(self._list_sync)
            if not any(e["id"] == event_id for e in events):
                raise FileNotFoundError(f"第 {event_id} 届不存在")
            if len(events) <= 1:
                raise ValueError("至少保留一届赛事，无法删除")
            was_current = event_id == self._current
            await asyncio.to_thread(self._delete_sync, event_id)
            log.warning("已删除赛事届 | id=%s", event_id)
            if not was_current:
                return self._config
            remaining = [e for e in events if e["id"] != event_id]
            next_id = remaining[0]["id"]
            cfg = await asyncio.to_thread(self._load_sync, next_id)
            self._current = next_id
            self._config = cfg
            await asyncio.to_thread(self._set_current_sync, next_id)
        await self._notify(cfg, "event:delete")
        return cfg

    # ------------------------------------------------------------------ #
    # 数据库操作（同步实现，由 asyncio.to_thread 调用）
    # ------------------------------------------------------------------ #
    def _check_id(self, event_id: str) -> None:
        if not _EVENT_ID_RE.match(event_id or ""):
            raise ValueError("届次 ID 不合法")

    def _read_current_sync(self) -> str:
        with db.connect(self._db_path) as conn:
            return db.get_meta(conn, db.CURRENT_KEY)

    def _list_sync(self) -> list[dict[str, Any]]:
        with db.connect(self._db_path) as conn:
            return db.list_events(conn)

    def _raw_sync(self, event_id: str) -> dict[str, Any] | None:
        """读一届的**原始字典**（已做过出厂示例名单清理），不做模型校验。

        升级路径要看「库里存的到底是什么」（例如计分三件套是否为空），
        拿不到校验后的模型——校验会把旧值就地补成新值，线索就没了。
        """
        with db.connect(self._db_path) as conn:
            data = db.load_event(conn, event_id)
        if data is None:
            return None
        data, purged = _strip_demo_roster(data)
        if purged and event_id == self._current:
            # 标记待落盘：清理结果需要在启动时写回数据库（见 start）
            self._demo_purged = True
            log.warning(
                "检测到出厂示例选手且从未使用，已从数据库清除 | 届=%s | 可在「选手名单」重新录入",
                (data.get("event") or {}).get("name") or "(未命名)",
            )
        return data

    def _load_sync(self, event_id: str) -> Config:
        data = self._raw_sync(event_id)
        if data is None:
            raise FileNotFoundError(f"第 {event_id} 届不存在")
        return Config.model_validate(data)

    def _save_sync(self, event_id: str, cfg: Config, touch_current: bool = True) -> None:
        champion = self._champion(cfg)
        with db.connect(self._db_path) as conn:
            db.save_event(conn, event_id, cfg.dump(), champion=champion)
            if touch_current:
                db.set_meta(conn, db.CURRENT_KEY, event_id)

    def _insert_sync(self, cfg: Config) -> str:
        with db.connect(self._db_path) as conn:
            event_id = db.next_event_id(conn)
            db.save_event(conn, event_id, cfg.dump(), created_at=now_iso(), champion="")
            db.set_meta(conn, db.CURRENT_KEY, event_id)
        return event_id

    def _delete_sync(self, event_id: str) -> None:
        with db.connect(self._db_path) as conn:
            # 公告不挂外键（服务器级公告的 event_id 是空串），所以删届时要自己清干净
            db.delete_event_notices(conn, event_id)
            db.delete_event(conn, event_id)

    def _set_current_sync(self, event_id: str) -> None:
        with db.connect(self._db_path) as conn:
            db.set_meta(conn, db.CURRENT_KEY, event_id)

    def _create_blank_sync(self) -> str:
        cfg = Config.model_validate({**default_config(), "event": {**default_config()["event"], "name": "首届赛事"}})
        return self._insert_sync(cfg)

    # ------------------------------------------------------------------ #
    # 旧 JSON 迁移
    # ------------------------------------------------------------------ #
    def _migrate_sync(self) -> None:
        """数据库为空时，把旧的 config/events/*.json 或 match.json 导入一次。"""
        with db.connect(self._db_path) as conn:
            if conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]:
                return

        sources: list[tuple[str, Config]] = []
        if LEGACY_EVENTS_DIR.is_dir():
            for path in sorted(LEGACY_EVENTS_DIR.glob("e*.json")):
                try:
                    sources.append((path.stem, Config.model_validate(json.loads(path.read_text(encoding="utf-8")))))
                except (OSError, ValueError) as exc:
                    log.error("跳过无法解析的旧届文件 | file=%s | err=%s", path.name, exc)
        if not sources and LEGACY_CONFIG_PATH.exists():
            try:
                sources.append(("e001", Config.model_validate(json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8")))))
            except (OSError, ValueError) as exc:
                log.error("旧配置无法解析，改为创建空白届: %s", exc)

        if not sources:
            return

        id_map: dict[str, str] = {}
        with db.connect(self._db_path) as conn:
            for old_id, cfg in sources:
                new_id = old_id if _EVENT_ID_RE.match(old_id) else db.next_event_id(conn)
                if db.event_exists(conn, new_id):
                    new_id = db.next_event_id(conn)
                id_map[old_id] = new_id
                if not cfg.event.name:
                    cfg = Config.model_validate(
                        {**cfg.dump(), "event": {**cfg.event.dump(), "name": cfg.event.title or new_id}}
                    )
                db.save_event(
                    conn,
                    new_id,
                    cfg.dump(),
                    created_at=cfg.updated_at or now_iso(),
                    champion=self._champion(cfg),
                )
            current_old = ""
            if LEGACY_INDEX_PATH.exists():
                try:
                    current_old = json.loads(LEGACY_INDEX_PATH.read_text(encoding="utf-8")).get("current", "")
                except (OSError, ValueError):
                    current_old = ""
            db.set_meta(conn, db.CURRENT_KEY, id_map.get(current_old) or next(iter(id_map.values())))

        log.warning("已从旧 JSON 迁移 %d 届赛事到 SQLite | 映射=%s", len(sources), id_map)
        self._archive_legacy()

    @staticmethod
    def _archive_legacy() -> None:
        """把旧文件挪到 config/migrated-json/，避免重复导入与误改。"""
        try:
            MIGRATED_DIR.mkdir(parents=True, exist_ok=True)
            if LEGACY_EVENTS_DIR.is_dir():
                shutil.move(str(LEGACY_EVENTS_DIR), str(MIGRATED_DIR / "events"))
            for path in (LEGACY_INDEX_PATH, LEGACY_CONFIG_PATH):
                if path.exists():
                    shutil.move(str(path), str(MIGRATED_DIR / path.name))
            log.info("旧配置已归档到 %s（可安全删除）", MIGRATED_DIR)
        except OSError as exc:
            log.warning("旧文件归档失败（忽略）: %s", exc)

    def _warn_non_ascii_stream_keys(self) -> None:
        """提示历史数据里的**非 ASCII 推流标识**（这类推流地址根本用不了）。

        刻意只警告、不拒绝启动：这些值可能在库里躺了很久，直接报错会让人打不开站点。
        但也不能沉默——它们的表现是「推流地址莫名为空」，不给线索就只能靠猜。
        """
        bad = [m.display_name for m in self._members if m.stream_id and not m.stream_id.isascii()]
        bad += [c.display_name for c in self._channels if c.stream_key and not c.stream_key.isascii()]
        bad += [p.display_name for p in self._config.players if p.stream_key and not p.stream_key.isascii()]
        if bad:
            log.warning(
                "以下推流标识含非 ASCII 字符（推流地址不可用，请改成字母 / 数字 / - / _）| %s",
                "、".join(dict.fromkeys(bad)),
            )

    # ------------------------------------------------------------------ #
    # 统计与回调
    # ------------------------------------------------------------------ #
    @staticmethod
    def _champion(cfg: Config) -> str:
        """总决赛胜者（尚未决出时为空）。"""
        try:
            team = tournament.champion_of(cfg.teams, cfg.rounds)
            return team.label if team else ""
        except Exception:
            log.debug("冠军统计失败（忽略）", exc_info=True)
            return ""

    async def _notify(self, cfg: Config, source: str) -> None:
        if not self._hooks:
            return
        results = await asyncio.gather(
            *(hook(cfg, source) for hook in self._hooks), return_exceptions=True
        )
        for res in results:
            if isinstance(res, BaseException):
                log.exception("变更回调执行失败", exc_info=res)

    # ------------------------------------------------------------------ #
    # 操作日志（最近若干条；只在服务器管理页给管理员看）
    # ------------------------------------------------------------------ #
    def _record_activity_sync(
        self, ts: str, actor: str, actor_uid: str, method: str, path: str, status: int
    ) -> None:
        with db.connect(self._db_path) as conn:
            db.record_activity(
                conn,
                ts=ts,
                actor=actor,
                actor_uid=actor_uid,
                method=method,
                path=path,
                status=status,
            )
            conn.commit()

    async def log_activity(
        self,
        *,
        actor: str,
        actor_uid: str = "",
        method: str,
        path: str,
        status: int,
        ts: str = "",
    ) -> None:
        """记一条操作日志。

        **绝不能因为它失败而影响请求**——它是观测手段，不是业务逻辑；所以这里
        吞掉所有异常，只在日志里留一行 warning（磁盘满 / 库被锁都可能发生）。
        """
        # 复用 now_iso()：时间格式与赛事数据里的时间戳保持同一套（本地时间）
        stamp = ts or now_iso().replace("T", " ")
        try:
            await asyncio.to_thread(
                self._record_activity_sync, stamp, actor, actor_uid, method, path, status
            )
        except Exception:  # pragma: no cover - 观测失败不该冒泡到请求
            log.warning("操作日志写入失败 | %s %s", method, path, exc_info=True)

    async def activity(self, limit: int = 60) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._activity_sync, limit)

    def _activity_sync(self, limit: int) -> list[dict[str, Any]]:
        with db.connect(self._db_path) as conn:
            return db.list_activity(conn, limit)


store = ConfigStore()
