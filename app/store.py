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
import re
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, league, tournament
from .defaults import default_config
from .logging_conf import get_logger
from .models import Config

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
CONFIG_DIR = PROJECT_ROOT / "config"
DB_PATH = CONFIG_DIR / "nte.sqlite3"
LEGACY_EVENTS_DIR = CONFIG_DIR / "events"
LEGACY_INDEX_PATH = CONFIG_DIR / "index.json"
LEGACY_CONFIG_PATH = CONFIG_DIR / "match.json"
MIGRATED_DIR = CONFIG_DIR / "migrated-json"
DATA_DIR = PROJECT_ROOT / "data"

_EVENT_ID_RE = re.compile(r"^e\d{3,}$")

Mutator = Callable[[dict[str, Any]], dict[str, Any]]
ChangeHook = Callable[[Config, str], Awaitable[None]]
# 收尾器：在所有变更与阵容重算都完成之后再过一遍（见 Mutate.final）
Finalizer = Callable[[dict[str, Any]], dict[str, Any]]


def now_iso() -> str:
    """本地时区的 ISO 秒级时间戳（刻意使用本地无时区表示，便于前端直接展示）。"""
    return datetime.now().replace(microsecond=0).isoformat()  # noqa: DTZ005


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

        current = await asyncio.to_thread(self._read_current_sync)
        if not current:
            current = await asyncio.to_thread(self._create_blank_sync)
            log.info("数据库为空，已初始化首届赛事 | id=%s", current)
        self._current = current
        self._config = await asyncio.to_thread(self._load_sync, current)
        if self._demo_purged:
            # 把出厂示例名单的清理结果落盘：数据库里也不该留这些假数据，
            # 顺便刷新 events 表缓存的选手数（往届列表会读它）
            self._demo_purged = False
            await self.mutate(lambda data: data, actor="purge-demo-roster", resolve=False)
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
        """届次列表（含当前标记），按创建时间倒序。"""
        events = await asyncio.to_thread(self._list_sync)
        for item in events:
            item["current"] = item["id"] == self._current
        return events

    async def read_event(self, event_id: str) -> Config:
        """只读载入任意一届（用于查看历史战绩，不影响当前届）。"""
        self._check_id(event_id)
        return await asyncio.to_thread(self._load_sync, event_id)

    async def event_count(self) -> int:
        return len(await self.list_events())

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
        dumped = [r.dump() for r in tournament.resolve_tournament(cfg.teams, cfg.rounds)]
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

    # ------------------------------------------------------------------ #
    # 本届参与名单 / 组队 / 赛程
    # ------------------------------------------------------------------ #
    async def set_participants(self, ids: list[str], actor: str = "api") -> tuple[Config, list[str]]:
        """设定本届参与名单，并按赛制自动跟进。

        * 积分制：名单变化后可在同一事务内重排未开赛对局（人数不足则整体拒绝）；
        * 锦标赛制：只改名单，提示需要重新组队并生成赛程。
        """
        from .logic import joined_players, normalize_participants  # 局部导入，避免模块级循环依赖

        warnings: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            cfg = Config.model_validate(data)
            before = {p.id for p in joined_players(cfg)}
            chosen = normalize_participants(cfg, ids)
            merged = {**data, "participants": chosen}
            if cfg.rounds and before == set(chosen):
                return merged
            if cfg.rules.format == "league" and cfg.rounds:
                rounds, warns = league.reconcile_rounds(Config.model_validate(merged))
                merged["rounds"] = [r.dump() for r in rounds]
                warnings.extend(warns)
            elif cfg.rounds:
                warnings.append("参与名单已变化：现有队伍与赛程仍是按旧名单生成的，请重新组队并生成赛程。")
            return merged

        cfg = await self.mutate(_mutate, actor=actor, resolve=False)
        log.warning(
            "已更新本届参与名单 | 届=%s | 参与=%d/%d 人",
            self._current,
            len(cfg.participants),
            len(cfg.players),
        )
        return cfg, warnings

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
            # 锦标赛制没有替补：替补选手不进入队伍（积分制仍允许）
            teams, warns = tournament.auto_form_teams(
                joined_players(cfg),
                size,
                seed,
                merge_remainder=merge_remainder,
                allow_substitutes=cfg.rules.format == "league",
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
                    allow_substitutes=cfg.rules.format == "league",
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
            cfg = Config.model_validate(data)
            # 未指定参与名单时按「全部启用选手」落成显式名单，避免后续歧义
            if not cfg.participants:
                data = {
                    **data,
                    "participants": normalize_participants(cfg, [p.id for p in joined_players(cfg)]),
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
        self, name: str, copy_roster: bool = False, fmt: str = "tournament"
    ) -> Config:
        """新建一届并切换过去；可选沿用当前届的名单、队伍、规则与界面配置，并指定赛制。"""
        async with self._lock:
            template = default_config()
            if copy_roster:
                current = self._config.dump()
                template["players"] = current.get("players", [])
                template["participants"] = current.get("participants", [])
                # 沿用固定队伍（积分制的「固定队伍」模式也依赖它）；
                # 新一届没有赛程，可随时在组队台重新随机
                template["teams"] = current.get("teams", [])
                for key in ("rules", "stream", "ui", "admin"):
                    if key in current:
                        template[key] = current[key]
            # 赛制以新建时选择的为准（覆盖沿用的规则）
            template["rules"] = {**template.get("rules", {}), "format": fmt}
            events = await asyncio.to_thread(self._list_sync)
            clean_name = (name or "").strip() or f"第 {len(events) + 1} 届"
            template["event"]["name"] = clean_name
            template["event"]["status"] = "active"
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
        clean = {k: v for k, v in patch.items() if k in {"name", "status"} and v not in (None, "")}
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

    def _load_sync(self, event_id: str) -> Config:
        with db.connect(self._db_path) as conn:
            data = db.load_event(conn, event_id)
        if data is None:
            raise FileNotFoundError(f"第 {event_id} 届不存在")
        data, purged = _strip_demo_roster(data)
        if purged and event_id == self._current:
            # 标记待落盘：清理结果需要在启动时写回数据库（见 start）
            self._demo_purged = True
            log.warning(
                "检测到出厂示例选手且从未使用，已从数据库清除 | 届=%s | 可在「选手名单」重新录入",
                (data.get("event") or {}).get("name") or "(未命名)",
            )
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


store = ConfigStore()
