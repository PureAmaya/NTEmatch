"""SQLite 持久层：多届赛事的读写。

表结构（全部按届隔离，``events.id`` 为主键，子表通过外键级联删除）::

    events          届元信息 + 统计（名称/状态/规模/已赛局数/榜首）
    event_rules     赛制
    event_ui        界面展示配置
    event_stream    直播根地址与播放模式
    players         选手（含 UUID / 推流流名 / 替补标记）
    event_participants 本届手动参与名单（有序；空 = 全员参与）
    teams           队伍
    team_players    队伍成员（有序）
    rounds          对局（含各轮成绩 / 时长 / 直播开关）
    round_sides     对局各方（2~4 队同场：队伍、标签、比分、得分、名次）
    round_players   对局出场选手（有序）
    meta            全局键值（当前届 ID 等）

约定：布尔以 0/1 存储，列表顺序用 ``position`` 列保存；
``save_event`` 在同一事务内「覆盖式重写」某一届（数据量小，简单且不会写脏）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .logging_conf import get_logger
from .models import MAX_SIDES

log = get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'active',
  title         TEXT NOT NULL DEFAULT '',
  subtitle      TEXT NOT NULL DEFAULT '',
  brief         TEXT NOT NULL DEFAULT '',
  venue         TEXT NOT NULL DEFAULT '',
  organizer     TEXT NOT NULL DEFAULT '',
  start_time    TEXT NOT NULL DEFAULT '',
  end_time      TEXT NOT NULL DEFAULT '',
  locked        INTEGER NOT NULL DEFAULT 0,
  locked_at     TEXT NOT NULL DEFAULT '',
  rules_text    TEXT NOT NULL DEFAULT '',
  logo_text     TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  revision      INTEGER NOT NULL DEFAULT 0,
  players_count INTEGER NOT NULL DEFAULT 0,
  rounds_count  INTEGER NOT NULL DEFAULT 0,
  played_count  INTEGER NOT NULL DEFAULT 0,
  champion      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_rules (
  event_id            TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  format              TEXT NOT NULL DEFAULT 'tournament',
  team_size           INTEGER NOT NULL DEFAULT 2,
  allow_draw          INTEGER NOT NULL DEFAULT 0,
  target_score        INTEGER NOT NULL DEFAULT 0,
  points_win          INTEGER NOT NULL DEFAULT 3,
  points_lose         INTEGER NOT NULL DEFAULT 0,
  points_draw         INTEGER NOT NULL DEFAULT 1,
  total_rounds        INTEGER NOT NULL DEFAULT 5,
  fair_rotation       INTEGER NOT NULL DEFAULT 1,
  min_rank_played     INTEGER NOT NULL DEFAULT 5,
  group_count         INTEGER NOT NULL DEFAULT 0,
  knockout_size       INTEGER NOT NULL DEFAULT 0,
  teams_per_match     INTEGER NOT NULL DEFAULT 2,
  loser_bracket       INTEGER NOT NULL DEFAULT 1,
  metric              TEXT NOT NULL DEFAULT 'score',
  value_type          TEXT NOT NULL DEFAULT '',
  value_label         TEXT NOT NULL DEFAULT '',
  better              TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_ui (
  event_id       TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  accent         TEXT NOT NULL DEFAULT 'cyan',
  accent_custom  TEXT NOT NULL DEFAULT '',
  og_image       TEXT NOT NULL DEFAULT '',
  show_qq        INTEGER NOT NULL DEFAULT 1,
  show_avatar    INTEGER NOT NULL DEFAULT 1,
  reveal_results INTEGER NOT NULL DEFAULT 1,
  ticker         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_stream (
  event_id   TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  enabled    INTEGER NOT NULL DEFAULT 1,
  provider   TEXT NOT NULL DEFAULT 'mediamtx',
  base_url   TEXT NOT NULL DEFAULT '',
  api_base   TEXT NOT NULL DEFAULT '',
  api_user   TEXT NOT NULL DEFAULT '',
  api_pass   TEXT NOT NULL DEFAULT '',
  hls_base   TEXT NOT NULL DEFAULT '',
  stream_key TEXT NOT NULL DEFAULT 'stream',
  push_token TEXT NOT NULL DEFAULT '',
  mode       TEXT NOT NULL DEFAULT 'auto',
  verify_tls INTEGER NOT NULL DEFAULT 1,
  whip_push  TEXT NOT NULL DEFAULT '',
  poster     TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS players (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  id         TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  uuid       TEXT NOT NULL DEFAULT '',
  qq         TEXT NOT NULL DEFAULT '',
  avatar     TEXT NOT NULL DEFAULT '',
  tag        TEXT NOT NULL DEFAULT '',
  stream_key TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT '',
  substitute INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  position   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, id)
);

CREATE TABLE IF NOT EXISTS event_participants (
  event_id  TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  player_id TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, player_id)
);

CREATE TABLE IF NOT EXISTS teams (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  id         TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  short      TEXT NOT NULL DEFAULT '',
  color      TEXT NOT NULL DEFAULT '',
  position   INTEGER NOT NULL DEFAULT 0,
  group_name TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (event_id, id)
);

CREATE TABLE IF NOT EXISTS team_players (
  event_id  TEXT NOT NULL,
  team_id   TEXT NOT NULL,
  player_id TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, team_id, player_id),
  FOREIGN KEY (event_id, team_id) REFERENCES teams(event_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rounds (
  event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  label         TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'pending',
  winner        TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  scheduled_at  TEXT NOT NULL DEFAULT '',
  started_at    TEXT NOT NULL DEFAULT '',
  finished_at   TEXT NOT NULL DEFAULT '',
  locked        INTEGER NOT NULL DEFAULT 0,
  code          TEXT NOT NULL DEFAULT '',
  stage         TEXT NOT NULL DEFAULT 'group',
  bracket_round INTEGER NOT NULL DEFAULT 0,
  slot          INTEGER NOT NULL DEFAULT 0,
  src_a         TEXT NOT NULL DEFAULT '',
  src_b         TEXT NOT NULL DEFAULT '',
  winner_to     TEXT NOT NULL DEFAULT '',
  loser_to      TEXT NOT NULL DEFAULT '',
  duration_min  INTEGER NOT NULL DEFAULT 0,
  live          INTEGER NOT NULL DEFAULT 0,
  live_note     TEXT NOT NULL DEFAULT '',
  sets_json     TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (event_id, idx)
);

CREATE TABLE IF NOT EXISTS round_sides (
  event_id  TEXT NOT NULL,
  round_idx INTEGER NOT NULL,
  side      TEXT NOT NULL,
  team_id   TEXT NOT NULL DEFAULT '',
  label     TEXT NOT NULL DEFAULT '',
  score     INTEGER NOT NULL DEFAULT 0,
  points    INTEGER NOT NULL DEFAULT 0,
  rank      INTEGER NOT NULL DEFAULT 0,
  forfeit   INTEGER NOT NULL DEFAULT 0,
  source    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (event_id, round_idx, side),
  FOREIGN KEY (event_id, round_idx) REFERENCES rounds(event_id, idx) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS round_players (
  event_id  TEXT NOT NULL,
  round_idx INTEGER NOT NULL,
  side      TEXT NOT NULL,
  player_id TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, round_idx, side, player_id),
  FOREIGN KEY (event_id, round_idx, side) REFERENCES round_sides(event_id, round_idx, side) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- 公告（通知）：scope=event 挂在某一届上，scope=server 是站点级的。
-- 正文是 Markdown 原文，渲染在服务端做（见 app/markdown.py）。
-- **刻意不放进 events 的配置 JSON 里**：那份配置会随每次改动广播给所有在线客户端，
-- 把公告正文塞进去等于让每次改比分都重发一遍公告。
CREATE TABLE IF NOT EXISTS notices (
  id         TEXT PRIMARY KEY,
  scope      TEXT NOT NULL DEFAULT 'event',
  event_id   TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  body       TEXT NOT NULL DEFAULT '',
  author     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  -- 单调递增的序号：**排序的唯一依据**。时间戳只到秒，同一秒里发的两条通知
  -- 用时间排是随机的（id 是随机 uuid），「最新一条」会飘。
  seq        INTEGER NOT NULL DEFAULT 0
);

-- 只按「作用域 + 届」建索引：排序键（seq）刻意不进索引——
-- 否则老库补列时会因为「索引里有个还不存在的列」而升级失败（SQLite 不允许
-- 先建引用不存在列的索引，而补列发生在建表脚本之后）。表本身很小，够用。
CREATE INDEX IF NOT EXISTS idx_notices_scope ON notices(scope, event_id);

-- 成员频道（日常直播）：**全局**，不挂在任何一届赛事上，跨届共享
CREATE TABLE IF NOT EXISTS channels (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  qq          TEXT NOT NULL DEFAULT '',
  avatar      TEXT NOT NULL DEFAULT '',
  stream_key  TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',
  server      TEXT NOT NULL DEFAULT '',
  role        TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  tags_json   TEXT NOT NULL DEFAULT '[]',
  link        TEXT NOT NULL DEFAULT '',
  color       TEXT NOT NULL DEFAULT '',
  sort        INTEGER NOT NULL DEFAULT 0,
  active      INTEGER NOT NULL DEFAULT 1,
  featured    INTEGER NOT NULL DEFAULT 0
);

-- 成员（全局账号）：**不挂在任何一届**，跨届共享。
-- key_hash / bearer_hash 是密钥与 WHIP Bearer 令牌的**加盐**哈希，明文永不落库；
-- key_sha256 / bearer_sha256 是历史无盐格式，只为让老库继续可校验（轮换即升级）。
CREATE TABLE IF NOT EXISTS members (
  uid           TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  qq            TEXT NOT NULL DEFAULT '',
  avatar        TEXT NOT NULL DEFAULT '',
  game_uuid     TEXT NOT NULL DEFAULT '',
  stream_id     TEXT NOT NULL DEFAULT '',
  room_title    TEXT NOT NULL DEFAULT '',
  bili_room     TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  permission    TEXT NOT NULL DEFAULT 'member',
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  key_hash      TEXT NOT NULL DEFAULT '',
  bearer_hash   TEXT NOT NULL DEFAULT '',
  key_sha256    TEXT NOT NULL DEFAULT '',
  bearer_sha256 TEXT NOT NULL DEFAULT ''
);

-- 直播间封禁（全局）：scope=global 由服务器管理员签发；scope=event 由赛事管理员签发。
CREATE TABLE IF NOT EXISTS live_bans (
  id          TEXT PRIMARY KEY,
  scope       TEXT NOT NULL DEFAULT 'global',
  member_uid  TEXT NOT NULL DEFAULT '',
  stream_id   TEXT NOT NULL DEFAULT '',
  name        TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  until       TEXT NOT NULL DEFAULT '',
  event_id    TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL DEFAULT ''
);

-- 操作日志：中间件按「写请求」记一条（谁、什么时候、动了哪个接口），
-- 只在服务器管理页给管理员看。刻意只记方法与路径，**不记请求体**——请求体里
-- 可能有成员密钥、Bearer 令牌之类的敏感值。
CREATE TABLE IF NOT EXISTS activity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL DEFAULT '',
  actor       TEXT NOT NULL DEFAULT '',
  actor_uid   TEXT NOT NULL DEFAULT '',
  method      TEXT NOT NULL DEFAULT '',
  path        TEXT NOT NULL DEFAULT '',
  status      INTEGER NOT NULL DEFAULT 0
);

-- 登录会话：**落库**，因为热更新会换掉进程（见 app/hot.py）。
-- 存的是 token 的 sha256 而**不是明文**：token 是 32 字节随机串，读库的人反推不出它，
-- 也就没法拿这份数据去冒充登录；明文一旦落库，读库就等于拿到了所有人的登录态。
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  uid        TEXT NOT NULL DEFAULT '',
  name       TEXT NOT NULL DEFAULT '',
  permission TEXT NOT NULL DEFAULT '',
  label      TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_players_event ON players(event_id, position);
CREATE INDEX IF NOT EXISTS idx_members_stream ON members(stream_id);
CREATE INDEX IF NOT EXISTS idx_rounds_event ON rounds(event_id, idx);
CREATE INDEX IF NOT EXISTS idx_round_players_player ON round_players(event_id, player_id);
"""

CURRENT_KEY = "current_event"
SIDES = ("A", "B")


@contextmanager
def connect(path: Path):
    """短连接：随用随开，配合 WAL 足够应对本站写入量。"""
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# 后加的列：旧库靠 ALTER 补齐（SQLite 不支持 ADD COLUMN IF NOT EXISTS），
# 因此赛制升级不需要删库，也不会丢历史数据。
# 历史默认值是 http://live.shiyora.net…：本站上 CDN 后是 HTTPS 页面，
# http:// 会被浏览器按混合内容拦掉（连内嵌播放页都打不开）。
# 只把**仍是这两个旧默认 host** 的地址升到 https，自定义地址一律不动——
# 媒体服务器没开 TLS 的话，管理员可以在「直播配置」里自行改回 http://。
_LEGACY_PLAIN_ORIGINS = ("http://live.shiyora.net:8889", "http://live.shiyora.net:8888")


def _upgrade_stream_https(stream: dict[str, Any]) -> dict[str, Any]:
    """把旧默认的 http://live.shiyora.net… 升到 https（幂等，只动默认 host）。"""
    out = dict(stream)
    upgraded: list[str] = []
    for field in ("baseUrl", "hlsBase", "whipPush"):
        raw = str(out.get(field) or "")
        for origin in _LEGACY_PLAIN_ORIGINS:
            if raw.startswith(origin):
                out[field] = "https://" + raw[len("http://") :]
                upgraded.append(field)
                break
    if upgraded:
        log.info(
            "直播地址已由 http 升级为 https（旧默认值） | 字段=%s | 如需改回请在「直播配置」里编辑",
            ", ".join(upgraded),
        )
    return out


_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "events": {
        # 比赛简介（≤30 字，留空则不展示）
        "brief": "TEXT NOT NULL DEFAULT ''",
        "end_time": "TEXT NOT NULL DEFAULT ''",
        "locked": "INTEGER NOT NULL DEFAULT 0",
        "locked_at": "TEXT NOT NULL DEFAULT ''",
        # 届次归属与可见性（见 models.EventInfo.owner_uid / hidden）
        "owner_uid": "TEXT NOT NULL DEFAULT ''",
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        # 比赛类型与排名开关（见 models.EventInfo.sport / ranked）
        "sport": "TEXT NOT NULL DEFAULT 'volleyball'",
        "ranked": "INTEGER NOT NULL DEFAULT 1",
    },
    "event_rules": {
      "format": "TEXT NOT NULL DEFAULT 'tournament'",
      "group_count": "INTEGER NOT NULL DEFAULT 0",
      "knockout_size": "INTEGER NOT NULL DEFAULT 0",
      "teams_per_match": "INTEGER NOT NULL DEFAULT 2",
      "loser_bracket": "INTEGER NOT NULL DEFAULT 1",
      # 旧口径比法名（score / time）：只为老版本兼容保留，见 models.Rules.metric
      "metric": "TEXT NOT NULL DEFAULT 'score'",
      # 计分口径三件套（类型 / 标签 / 判断标准）：见 app/metrics.py
      # 空串 = 老库刚补上的列，由 models.Rules 按 metric 推导后落值
      "value_type": "TEXT NOT NULL DEFAULT ''",
      "value_label": "TEXT NOT NULL DEFAULT ''",
      "better": "TEXT NOT NULL DEFAULT ''",
      },
    "event_ui": {
        # 自定义主题色与分享图（见 models.UiConfig）
        "accent_custom": "TEXT NOT NULL DEFAULT ''",
        "og_image": "TEXT NOT NULL DEFAULT ''",
    },
    "notices": {
        # 排序序号（见 db.save_notice）：老库（本功能刚上线时建的表）靠它补齐
        "seq": "INTEGER NOT NULL DEFAULT 0",
    },
    "event_stream": {
        # 主直播间 / 遗留频道的推流令牌（见 models.StreamConfig.push_token）
        "push_token": "TEXT NOT NULL DEFAULT ''",
        "verify_tls": "INTEGER NOT NULL DEFAULT 1",
        # MediaMTX 控制 API：用来查「谁真的在推流」
        "api_base": "TEXT NOT NULL DEFAULT ''",
        # 控制 API 的 Basic 认证（mediamtx.yml 里配了 authInternalUsers 才需要）
        "api_user": "TEXT NOT NULL DEFAULT ''",
        "api_pass": "TEXT NOT NULL DEFAULT ''",
    },
    "members": {
        # 加盐哈希（新格式）；同表的 *_sha256 是历史无盐格式，仅为兼容旧库保留
        "key_hash": "TEXT NOT NULL DEFAULT ''",
        "bearer_hash": "TEXT NOT NULL DEFAULT ''",
        # B站直播间号（见 models.Member.bili_room）
        "bili_room": "TEXT NOT NULL DEFAULT ''",
    },
    "teams": {"group_name": "TEXT NOT NULL DEFAULT ''"},
    "rounds": {
        "code": "TEXT NOT NULL DEFAULT ''",
        "stage": "TEXT NOT NULL DEFAULT 'group'",
        "bracket_round": "INTEGER NOT NULL DEFAULT 0",
        "slot": "INTEGER NOT NULL DEFAULT 0",
        "src_a": "TEXT NOT NULL DEFAULT ''",
        "src_b": "TEXT NOT NULL DEFAULT ''",
        "winner_to": "TEXT NOT NULL DEFAULT ''",
        "loser_to": "TEXT NOT NULL DEFAULT ''",
        "duration_min": "INTEGER NOT NULL DEFAULT 0",
        "live": "INTEGER NOT NULL DEFAULT 0",
        "live_note": "TEXT NOT NULL DEFAULT ''",
        "sets_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "round_sides": {
        "source": "TEXT NOT NULL DEFAULT ''",
        "points": "INTEGER NOT NULL DEFAULT 0",
        "rank": "INTEGER NOT NULL DEFAULT 0",
        "forfeit": "INTEGER NOT NULL DEFAULT 0",
    },
    "channels": {
        "server": "TEXT NOT NULL DEFAULT ''",
        "role": "TEXT NOT NULL DEFAULT ''",
    },
    "players": {
        # 关联的全局成员（选手就是成员）：空 = 独立选手
        "member_uid": "TEXT NOT NULL DEFAULT ''",
    },
}


# 已废弃的列：RTMP / RTSP / FLV 支持已从代码里移除，旧库启动时一并清掉。
# SQLite 的 DROP COLUMN 需要 3.35+（Python 3.11 自带的通常满足）；不支持时
# 静默跳过——留着这几列不影响功能，模型已经不认它们了。
_OBSOLETE_COLUMNS: dict[str, tuple[str, ...]] = {
    # 「启用直播」开关已移除（只要有赛事就允许直播，见 models.StreamConfig.enabled）
    "event_stream": (
        "rtmp_base",
        "rtsp_base",
        "rtmp_push",
        "rtsp_url",
        "hls_url",
        "flv_url",
        "enabled",
    ),
    # 「替补选手」类别与「系列赛（BO）」都已移除：轮次改成自由增删（见 models.SetScore），
    # 这两列不再被任何代码读取。旧数据在升级时会先落一份只读快照（见 app/legacy.py）。
    "event_rules": ("include_substitutes", "best_of"),
}

# 已退休的表：`event_admin` 存的是「主管理 KEY」——那套凭据已经去掉了（登录只认成员密钥），
# 旧库里留着的哈希不再被任何代码读取。这里直接删表，而不是留着：一张写着
# `NTE-ADMIN` 哈希的死表，除了让人以为它还有用，唯一的用途就是将来被谁误读回去。
_RETIRED_TABLES: tuple[str, ...] = ("event_admin",)


def _drop_tables(conn: sqlite3.Connection) -> None:
    """删除已退休的表（幂等）。"""
    dropped: list[str] = []
    for table in _RETIRED_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        conn.execute(f"DROP TABLE {table}")
        dropped.append(table)
    if dropped:
        log.warning("数据库结构已清理 | 删除退役表=%s", ", ".join(dropped))


def _drop_columns(conn: sqlite3.Connection) -> None:
    """删除已废弃的列（幂等）：让旧库跟上当前结构，而不是一直留着死列。"""
    dropped: list[str] = []
    for table, columns in _OBSOLETE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name in columns:
            if name not in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
                dropped.append(f"{table}.{name}")
            except sqlite3.OperationalError as exc:
                log.warning("废弃列删除失败（忽略，不影响功能） | %s.%s | %s", table, name, exc)
    if dropped:
        log.warning("数据库结构已清理 | 删除废弃列=%s", ", ".join(dropped))


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """按需补列（幂等）：让旧数据库无缝升级到新赛制结构。"""
    added: list[str] = []
    for table, columns in _EXTRA_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
    if added:
        log.warning("数据库结构已升级 | 新增列=%s", ", ".join(added))
    # 补列之后再删废弃列（两者互不重叠；顺序反过来也能跑）
    _drop_columns(conn)


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _drop_tables(conn)


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def next_event_id(conn: sqlite3.Connection) -> str:
    used = {row["id"] for row in conn.execute("SELECT id FROM events")}
    seq = 1
    while f"e{seq:03d}" in used:
        seq += 1
    return f"e{seq:03d}"


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# 登录会话（跨进程存活）
#
# 为什么要有这张表：热更新会**换掉进程**（见 app/hot.py）。会话只在内存里的话，
# 每次更新都等于把所有人踢下线——「不中断业务」也就成了空话。
# 读写都走**短连接 + 主键点查**：热路径（鉴权）只在**内存里没有该 token 时**才来一次，
# 命中一次之后就在内存里了（见 app/auth.py 的 resolve）。
# --------------------------------------------------------------------------- #
def load_session(path: Path, token_hash: str) -> dict[str, Any] | None:
    """按 token 的散列取一条会话（没有 / 已过期都回 ``None``）。"""
    if not token_hash:
        return None
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if float(data.get("expires_at") or 0) <= time.time():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        return data


def save_sessions(path: Path, rows: list[dict[str, Any]]) -> None:
    """批量写入 / 刷新会话（一次事务，避免逐条开关连接）。"""
    if not rows:
        return
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO sessions (token_hash, uid, name, permission, label, created_at, expires_at)
            VALUES (:token_hash, :uid, :name, :permission, :label, :created_at, :expires_at)
            ON CONFLICT(token_hash) DO UPDATE SET
                uid = excluded.uid,
                name = excluded.name,
                permission = excluded.permission,
                label = excluded.label,
                expires_at = excluded.expires_at
            """,
            rows,
        )


def delete_sessions(path: Path, token_hashes: list[str]) -> int:
    """按 token 散列删（登出 / 单点失效）。"""
    clean = [str(item) for item in token_hashes if item]
    if not clean:
        return 0
    with connect(path) as conn:
        removed = 0
        # 分批删：SQLite 的变量上限（旧版本 999）不是我们能假定的事
        for start in range(0, len(clean), 400):
            chunk = clean[start : start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(f"DELETE FROM sessions WHERE token_hash IN ({marks})", chunk)
            removed += int(cur.rowcount or 0)
        return removed


def delete_sessions_of(path: Path, uid: str) -> int:
    """删掉某成员的全部会话（成员被停用 / 改权限 / 轮换密钥时）。"""
    if not uid:
        return 0
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE uid = ?", (uid,))
        return int(cur.rowcount or 0)


def delete_all_sessions(path: Path) -> int:
    """清空所有会话（还原备份 / 全站强制重登）。"""
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM sessions")
        return int(cur.rowcount or 0)


def prune_sessions(path: Path, now: float | None = None) -> int:
    """清掉过期会话（顺手做，免得表随时间无限长）。"""
    moment = time.time() if now is None else float(now)
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (moment,))
        return int(cur.rowcount or 0)


def count_sessions(path: Path) -> int:
    """当前库里的会话数（诊断 / 测试用）。"""
    with connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"] if row else 0)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def save_event(
    conn: sqlite3.Connection,
    event_id: str,
    data: dict[str, Any],
    *,
    created_at: str | None = None,
    champion: str = "",
) -> None:
    """覆盖式写入某一届（含统计列）。数据量小，重写比增量 diff 更不易写脏。"""
    event = data.get("event", {})
    rules = data.get("rules", {})
    ui = data.get("ui", {})
    stream = data.get("stream", {})
    players = data.get("players", [])
    participants = data.get("participants", [])
    teams = data.get("teams", [])
    rounds = data.get("rounds", [])
    played = sum(1 for r in rounds if r.get("status") == "done" and r.get("winner"))

    current_created = conn.execute("SELECT created_at FROM events WHERE id = ?", (event_id,)).fetchone()
    created = created_at or (current_created["created_at"] if current_created else "") or data.get("updatedAt", "")

    conn.execute(
        """
        INSERT INTO events (
            id, name, status, title, subtitle, brief, venue, organizer, start_time, end_time,
            locked, locked_at, rules_text, logo_text, owner_uid, hidden, sport, ranked,
            created_at, updated_at, revision, players_count, rounds_count, played_count, champion
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, status = excluded.status, title = excluded.title,
            subtitle = excluded.subtitle, brief = excluded.brief,
            venue = excluded.venue, organizer = excluded.organizer,
            start_time = excluded.start_time, end_time = excluded.end_time,
            locked = excluded.locked, locked_at = excluded.locked_at,
            rules_text = excluded.rules_text, logo_text = excluded.logo_text,
            owner_uid = excluded.owner_uid, hidden = excluded.hidden,
            sport = excluded.sport, ranked = excluded.ranked,
            updated_at = excluded.updated_at, revision = excluded.revision,
            players_count = excluded.players_count, rounds_count = excluded.rounds_count,
            played_count = excluded.played_count, champion = excluded.champion
        """,
        (
            event_id,
            event.get("name", ""),
            event.get("status", "active"),
            event.get("title", ""),
            event.get("subtitle", ""),
            event.get("brief", ""),
            event.get("venue", ""),
            event.get("organizer", ""),
            event.get("startTime", ""),
            event.get("endTime", ""),
            int(bool(event.get("locked", False))),
            event.get("lockedAt", ""),
            event.get("rulesText", ""),
            event.get("logoText", ""),
            event.get("ownerUid", ""),
            int(bool(event.get("hidden", False))),
            event.get("sport", "volleyball") or "volleyball",
            int(bool(event.get("ranked", True))),
            created,
            data.get("updatedAt", ""),
            int(data.get("revision", 0)),
            len(players),
            len(rounds),
            played,
            champion,
        ),
    )

    _upsert(
        conn,
        "event_rules",
        ("event_id",),
        {
            "event_id": event_id,
            "format": rules.get("format", "tournament"),
            "team_size": rules.get("teamSize", 2),
            "allow_draw": int(bool(rules.get("allowDraw", False))),
            "target_score": rules.get("targetScore", 0),
            "points_win": rules.get("pointsWin", 3),
            "points_lose": rules.get("pointsLose", 0),
            "points_draw": rules.get("pointsDraw", 1),
            "total_rounds": rules.get("totalRounds", 5),
            "fair_rotation": int(bool(rules.get("fairRotation", True))),
            "min_rank_played": rules.get("minRankPlayed", 5),
            "group_count": rules.get("groupCount", 0),
            "knockout_size": rules.get("knockoutSize", 0),
            "teams_per_match": rules.get("teamsPerMatch", 2),
            "loser_bracket": int(bool(rules.get("loserBracket", True))),
            "metric": rules.get("metric", "score"),
            "value_type": rules.get("valueType", ""),
            "value_label": rules.get("valueLabel", ""),
            "better": rules.get("better", ""),
        },
    )
    _upsert(
        conn,
        "event_ui",
        ("event_id",),
        {
            "event_id": event_id,
            "accent": ui.get("accent", "cyan"),
            "accent_custom": ui.get("accentCustom", ""),
            "og_image": ui.get("ogImage", ""),
            "show_qq": int(bool(ui.get("showQq", True))),
            "show_avatar": int(bool(ui.get("showAvatar", True))),
            "reveal_results": int(bool(ui.get("revealResults", True))),
            "ticker": ui.get("ticker", ""),
        },
    )
    _upsert(
        conn,
        "event_stream",
        ("event_id",),
        {
            "event_id": event_id,
            "provider": stream.get("provider", "mediamtx"),
            "base_url": stream.get("baseUrl", ""),
            "api_base": stream.get("apiBase", ""),
            # 控制 API 的 Basic 认证（属于凭据，只在管理端下发）
            "api_user": stream.get("apiUser", ""),
            "api_pass": stream.get("apiPass", ""),
            "hls_base": stream.get("hlsBase", ""),
            "stream_key": stream.get("streamKey", "stream"),
            "push_token": stream.get("pushToken", ""),
            "mode": stream.get("mode", "auto"),
            # 是否校验上游 HTTPS 证书（自签名证书时关闭）
            "verify_tls": int(bool(stream.get("verifyTls", True))),
            "whip_push": stream.get("whipPush", ""),
            "poster": stream.get("poster", ""),
            "title": stream.get("title", ""),
            "note": stream.get("note", ""),
        },
    )
    # 子表：先清空该届再写入
    for table in (
        "round_players",
        "round_sides",
        "rounds",
        "team_players",
        "teams",
        "players",
        "event_participants",
    ):
        conn.execute(f"DELETE FROM {table} WHERE event_id = ?", (event_id,))

    _insert_many(
        conn,
        "event_participants",
        ("event_id", "player_id", "position"),
        ((event_id, str(pid), idx) for idx, pid in enumerate(participants) if pid),
    )

    _insert_many(
        conn,
        "players",
        (
            "event_id", "id", "name", "uuid", "qq", "avatar", "tag", "stream_key", "note",
            "substitute", "active", "member_uid", "position",
        ),
        (
            (
                event_id,
                p.get("id", ""),
                p.get("name", ""),
                p.get("uuid", ""),
                p.get("qq", ""),
                p.get("avatar", ""),
                p.get("tag", ""),
                p.get("streamKey", ""),
                p.get("note", ""),
                int(bool(p.get("substitute", False))),
                int(bool(p.get("active", True))),
                p.get("memberUid", ""),
                idx,
            )
            for idx, p in enumerate(players)
        ),
    )
    _insert_many(
        conn,
        "teams",
        ("event_id", "id", "name", "short", "color", "position", "group_name"),
        (
            (
                event_id,
                t.get("id", ""),
                t.get("name", ""),
                t.get("short", ""),
                t.get("color", ""),
                idx,
                t.get("group", ""),
            )
            for idx, t in enumerate(teams)
        ),
    )
    _insert_many(
        conn,
        "team_players",
        ("event_id", "team_id", "player_id", "position"),
        (
            (event_id, t.get("id", ""), pid, pos)
            for t in teams
            for pos, pid in enumerate(t.get("playerIds", []) or [])
        ),
    )

    round_rows: list[tuple[Any, ...]] = []
    side_rows: list[tuple[Any, ...]] = []
    player_rows: list[tuple[Any, ...]] = []
    for idx, rnd in enumerate(rounds, start=1):
        round_idx = int(rnd.get("index", idx) or idx)
        round_rows.append(
            (
                event_id,
                round_idx,
                rnd.get("label", ""),
                rnd.get("status", "pending"),
                rnd.get("winner", ""),
                rnd.get("note", ""),
                rnd.get("scheduledAt", ""),
                rnd.get("startedAt", ""),
                rnd.get("finishedAt", ""),
                int(bool(rnd.get("locked", False))),
                rnd.get("code", ""),
                rnd.get("stage", "group"),
                int(rnd.get("bracketRound", 0) or 0),
                int(rnd.get("slot", 0) or 0),
                rnd.get("srcA", ""),
                rnd.get("srcB", ""),
                rnd.get("winnerTo", ""),
                rnd.get("loserTo", ""),
                int(rnd.get("durationMinutes", 0) or 0),
                int(bool(rnd.get("live", False))),
                rnd.get("liveNote", ""),
                json.dumps(rnd.get("sets", []) or [], ensure_ascii=False),
            )
        )
        # 2~4 方同场：sides 为准，兼容只给了 sideA/sideB 的旧数据
        sides = rnd.get("sides") or [rnd.get("sideA", {}), rnd.get("sideB", {})]
        for pos, side in enumerate(sides[:MAX_SIDES]):
            side_key = chr(ord("A") + pos)
            side = side or {}
            side_rows.append(
                (
                    event_id,
                    round_idx,
                    side_key,
                    side.get("teamId", ""),
                    side.get("label", ""),
                    int(side.get("score", 0)),
                    int(side.get("points", 0)),
                    int(side.get("rank", 0) or 0),
                    int(bool(side.get("forfeit", False))),
                    side.get("source", ""),
                )
            )
            for order, pid in enumerate(side.get("playerIds", []) or []):
                player_rows.append((event_id, round_idx, side_key, pid, order))

    _insert_many(
        conn,
        "rounds",
        (
            "event_id", "idx", "label", "status", "winner", "note",
            "scheduled_at", "started_at", "finished_at", "locked",
            "code", "stage", "bracket_round", "slot",
            "src_a", "src_b", "winner_to", "loser_to",
            "duration_min", "live", "live_note", "sets_json",
        ),
        round_rows,
    )
    _insert_many(
        conn,
        "round_sides",
        (
            "event_id", "round_idx", "side", "team_id", "label",
            "score", "points", "rank", "forfeit", "source",
        ),
        side_rows,
    )
    _insert_many(
        conn, "round_players", ("event_id", "round_idx", "side", "player_id", "position"), player_rows
    )


def delete_event(conn: sqlite3.Connection, event_id: str) -> bool:
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return cur.rowcount > 0


def update_event_meta(conn: sqlite3.Connection, event_id: str, patch: dict[str, Any]) -> bool:
    fields = {
        k: v
        for k, v in patch.items()
        if k
        in {
            "name",
            "status",
            "updated_at",
            "revision",
            "champion",
            "owner_uid",
            "hidden",
            "sport",
            "ranked",
            "brief",
        }
    }
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(
        f"UPDATE events SET {assignments} WHERE id = ?", (*fields.values(), event_id)
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def list_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, status, title, subtitle, brief, start_time, end_time,
               locked, locked_at, owner_uid, hidden, sport, ranked,
               created_at, updated_at, revision,
               players_count, rounds_count, played_count, champion
        FROM events ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "title": row["title"],
            "subtitle": row["subtitle"],
            "brief": row["brief"],
            "startTime": row["start_time"],
            "endTime": row["end_time"],
            "locked": bool(row["locked"]),
            "lockedAt": row["locked_at"],
            "ownerUid": row["owner_uid"],
            "hidden": bool(row["hidden"]),
            "sport": row["sport"],
            "ranked": bool(row["ranked"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "revision": row["revision"],
            "players": row["players_count"],
            "rounds": row["rounds_count"],
            "played": row["played_count"],
            "champion": row["champion"],
        }
        for row in rows
    ]


def load_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    """按届读取完整配置，返回可直接交给 ``Config.model_validate`` 的字典。"""
    ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        return None
    rules = conn.execute("SELECT * FROM event_rules WHERE event_id = ?", (event_id,)).fetchone()
    ui = conn.execute("SELECT * FROM event_ui WHERE event_id = ?", (event_id,)).fetchone()
    stream = conn.execute("SELECT * FROM event_stream WHERE event_id = ?", (event_id,)).fetchone()

    players = [
        {
            "id": row["id"],
            "name": row["name"],
            "uuid": row["uuid"],
            "qq": row["qq"],
            "avatar": row["avatar"],
            "tag": row["tag"],
            "streamKey": row["stream_key"],
            "note": row["note"],
            "substitute": bool(row["substitute"]),
            "active": bool(row["active"]),
            "memberUid": row["member_uid"],
        }
        for row in conn.execute(
            "SELECT * FROM players WHERE event_id = ? ORDER BY position, id", (event_id,)
        )
    ]

    participants = [
        row["player_id"]
        for row in conn.execute(
            "SELECT player_id FROM event_participants WHERE event_id = ? ORDER BY position, player_id",
            (event_id,),
        )
    ]

    team_players: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT team_id, player_id FROM team_players WHERE event_id = ? ORDER BY team_id, position", (event_id,)
    ):
        team_players.setdefault(row["team_id"], []).append(row["player_id"])
    teams = [
        {
            "id": row["id"],
            "name": row["name"],
            "short": row["short"],
            "color": row["color"],
            "group": row["group_name"],
            "playerIds": team_players.get(row["id"], []),
        }
        for row in conn.execute(
            "SELECT * FROM teams WHERE event_id = ? ORDER BY position, id", (event_id,)
        )
    ]

    side_players: dict[tuple[int, str], list[str]] = {}
    for row in conn.execute(
        "SELECT round_idx, side, player_id FROM round_players WHERE event_id = ? "
        "ORDER BY round_idx, side, position",
        (event_id,),
    ):
        side_players.setdefault((row["round_idx"], row["side"]), []).append(row["player_id"])
    side_meta: dict[tuple[int, str], sqlite3.Row] = {}
    side_keys: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT * FROM round_sides WHERE event_id = ? ORDER BY side", (event_id,)
    ):
        side_meta[(row["round_idx"], row["side"])] = row
        side_keys.setdefault(row["round_idx"], []).append(row["side"])

    def _side(round_idx: int, key: str) -> dict[str, Any]:
        meta = side_meta.get((round_idx, key))
        return {
            "playerIds": side_players.get((round_idx, key), []),
            "teamId": meta["team_id"] if meta else "",
            "label": meta["label"] if meta else "",
            "score": meta["score"] if meta else 0,
            "points": meta["points"] if meta else 0,
            "rank": meta["rank"] if meta else 0,
            "forfeit": bool(meta["forfeit"]) if meta else False,
            "source": meta["source"] if meta else "",
        }

    def _sides(round_idx: int) -> list[dict[str, Any]]:
        keys = side_keys.get(round_idx) or ["A", "B"]
        out = [_side(round_idx, key) for key in keys[:MAX_SIDES]]
        while len(out) < 2:
            out.append(_side(round_idx, chr(ord("A") + len(out))))
        return out

    def _sets(raw: str) -> list[dict[str, int]]:
        try:
            data = json.loads(raw or "[]")
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [{"a": int(item.get("a", 0)), "b": int(item.get("b", 0))} for item in data if isinstance(item, dict)]

    rounds = [
        {
            "index": row["idx"],
            "code": row["code"],
            "stage": row["stage"],
            "bracketRound": row["bracket_round"],
            "slot": row["slot"],
            "label": row["label"],
            "status": row["status"],
            "winner": row["winner"],
            "note": row["note"],
            "sets": _sets(row["sets_json"]),
            "durationMinutes": row["duration_min"],
            "live": bool(row["live"]),
            "liveNote": row["live_note"],
            "scheduledAt": row["scheduled_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "locked": bool(row["locked"]),
            "srcA": row["src_a"],
            "srcB": row["src_b"],
            "winnerTo": row["winner_to"],
            "loserTo": row["loser_to"],
            "sides": _sides(row["idx"]),
        }
        for row in conn.execute("SELECT * FROM rounds WHERE event_id = ? ORDER BY idx", (event_id,))
    ]

    return {
        "version": 1,
        "revision": ev["revision"],
        "updatedAt": ev["updated_at"],
        "event": {
            "name": ev["name"],
            "status": ev["status"],
            "title": ev["title"],
            "subtitle": ev["subtitle"],
            "brief": ev["brief"],
            "venue": ev["venue"],
            "organizer": ev["organizer"],
            "startTime": ev["start_time"],
            "endTime": ev["end_time"],
            # 比赛是否已开始（赛制与参赛名单锁定）
            "locked": bool(ev["locked"]),
            "lockedAt": ev["locked_at"],
            "ownerUid": ev["owner_uid"],
            "hidden": bool(ev["hidden"]),
            "sport": ev["sport"],
            "ranked": bool(ev["ranked"]),
            "rulesText": ev["rules_text"],
            "logoText": ev["logo_text"],
        },
        "rules": {
            "format": rules["format"] if rules else "tournament",
            "teamSize": rules["team_size"] if rules else 2,
            "allowDraw": bool(rules["allow_draw"]) if rules else False,
            "targetScore": rules["target_score"] if rules else 0,
            "pointsWin": rules["points_win"] if rules else 3,
            "pointsLose": rules["points_lose"] if rules else 0,
            "pointsDraw": rules["points_draw"] if rules else 1,
            "totalRounds": rules["total_rounds"] if rules else 5,
            "fairRotation": bool(rules["fair_rotation"]) if rules else True,
            "minRankPlayed": rules["min_rank_played"] if rules else 5,
            "groupCount": rules["group_count"] if rules else 0,
            "knockoutSize": rules["knockout_size"] if rules else 0,
            "teamsPerMatch": rules["teams_per_match"] if rules else 2,
            "loserBracket": bool(rules["loser_bracket"]) if rules else True,
            "metric": rules["metric"] if rules else "score",
            "valueType": rules["value_type"] if rules else "",
            "valueLabel": rules["value_label"] if rules else "",
            "better": rules["better"] if rules else "",
        },
        "ui": {
            "accent": ui["accent"] if ui else "cyan",
            "accentCustom": ui["accent_custom"] if ui else "",
            "ogImage": ui["og_image"] if ui else "",
            "showQq": bool(ui["show_qq"]) if ui else True,
            "showAvatar": bool(ui["show_avatar"]) if ui else True,
            "revealResults": bool(ui["reveal_results"]) if ui else True,
            "ticker": ui["ticker"] if ui else "",
        },
        # 直播配置：没有记录时返回空字典，让模型默认值（HTTPS）生效；
        # 有记录时把仍是旧默认 http:// 的地址升到 https（见 _upgrade_stream_https）。
        "stream": _upgrade_stream_https(
            {
                "provider": stream["provider"],
                "baseUrl": stream["base_url"],
                "apiBase": stream["api_base"],
                "apiUser": stream["api_user"],
                # API 密码是**凭据**：明文只留服务端，这里只回一个布尔。
                # 前端据此显示「已配置（留空 = 不修改）」，提交时留空即保持原值
                # （见 logic.management_stream_config 与 main._apply_stream_patch）。
                "hasApiPass": bool(stream["api_pass"]),
                "hlsBase": stream["hls_base"],
                "streamKey": stream["stream_key"],
            "pushToken": stream["push_token"],
                "mode": stream["mode"],
                "verifyTls": bool(stream["verify_tls"]),
                "whipPush": stream["whip_push"],
                "poster": stream["poster"],
                "title": stream["title"],
                "note": stream["note"],
            }
        )
        if stream
        else {},
        "participants": participants,
        "teams": teams,
        "players": players,
        "rounds": rounds,
    }


# --------------------------------------------------------------------------- #
# 内部小工具
# --------------------------------------------------------------------------- #
def _upsert(conn: sqlite3.Connection, table: str, pk: tuple[str, ...], row: dict[str, Any]) -> None:
    cols = list(row)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
        f"ON CONFLICT({','.join(pk)}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def _insert_many(
    conn: sqlite3.Connection, table: str, cols: tuple[str, ...], rows: Iterable[tuple[Any, ...]]
) -> None:
    payload = list(rows)
    if not payload:
        return
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    conn.executemany(sql, payload)


# --------------------------------------------------------------------------- #
# 成员频道（全局，跨届共享）
#
# 与赛事无关，因此不按 event_id 隔离：读写都是全库一份。
# --------------------------------------------------------------------------- #
def list_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部成员频道，按「置顶优先 → sort → id」排序。"""
    rows = conn.execute(
        "SELECT * FROM channels ORDER BY featured DESC, sort, id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except ValueError:
            tags = []
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "qq": row["qq"],
                "avatar": row["avatar"],
                "streamKey": row["stream_key"],
                "title": row["title"],
                "server": row["server"],
                "role": row["role"],
                "description": row["description"],
                "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
                "link": row["link"],
                "color": row["color"],
                "sort": row["sort"],
                "active": bool(row["active"]),
                "featured": bool(row["featured"]),
            }
        )
    return out


def upsert_channel(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """新增 / 覆盖一个成员频道（按 id）。"""
    _upsert(
        conn,
        "channels",
        ("id",),
        {
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "qq": row.get("qq", ""),
            "avatar": row.get("avatar", ""),
            "stream_key": row.get("streamKey", ""),
            "title": row.get("title", ""),
            "server": row.get("server", ""),
            "role": row.get("role", ""),
            "description": row.get("description", ""),
            "tags_json": json.dumps(row.get("tags", []) or [], ensure_ascii=False),
            "link": row.get("link", ""),
            "color": row.get("color", ""),
            "sort": int(row.get("sort", 0) or 0),
            "active": int(bool(row.get("active", True))),
            "featured": int(bool(row.get("featured", False))),
        },
    )


def delete_channel(conn: sqlite3.Connection, channel_id: str) -> bool:
    cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 成员（全局账号）
# --------------------------------------------------------------------------- #
def _member_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "name": row["name"],
        "qq": row["qq"],
        "avatar": row["avatar"],
        "gameUuid": row["game_uuid"],
        "streamId": row["stream_id"],
        "roomTitle": row["room_title"],
        "biliRoom": row["bili_room"],
        "note": row["note"],
        "permission": row["permission"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "keyHash": row["key_hash"],
        "bearerHash": row["bearer_hash"],
        "keySha256": row["key_sha256"],
        "bearerSha256": row["bearer_sha256"],
    }


def list_members(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM members ORDER BY (permission = 'server_admin') DESC, created_at, uid"
    ).fetchall()
    return [_member_row(row) for row in rows]


def upsert_member(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _upsert(
        conn,
        "members",
        ("uid",),
        {
            "uid": row.get("uid", ""),
            "name": row.get("name", ""),
            "qq": row.get("qq", ""),
            "avatar": row.get("avatar", ""),
            "game_uuid": row.get("gameUuid", ""),
            "stream_id": row.get("streamId", ""),
            "room_title": row.get("roomTitle", ""),
            "bili_room": row.get("biliRoom", ""),
            "note": row.get("note", ""),
            "permission": row.get("permission", "member"),
            "active": int(bool(row.get("active", True))),
            "created_at": row.get("createdAt", ""),
            "updated_at": row.get("updatedAt", ""),
            "key_hash": row.get("keyHash", ""),
            "bearer_hash": row.get("bearerHash", ""),
            "key_sha256": row.get("keySha256", ""),
            "bearer_sha256": row.get("bearerSha256", ""),
        },
    )


def delete_member(conn: sqlite3.Connection, uid: str) -> bool:
    cur = conn.execute("DELETE FROM members WHERE uid = ?", (uid,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 直播间封禁（全局）
# --------------------------------------------------------------------------- #
def list_live_bans(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM live_bans ORDER BY created_at DESC, id DESC").fetchall()
    return [
        {
            "id": row["id"],
            "scope": row["scope"],
            "memberUid": row["member_uid"],
            "streamId": row["stream_id"],
            "name": row["name"],
            "reason": row["reason"],
            "until": row["until"],
            "eventId": row["event_id"],
            "createdAt": row["created_at"],
            "createdBy": row["created_by"],
        }
        for row in rows
    ]


def upsert_live_ban(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    _upsert(
        conn,
        "live_bans",
        ("id",),
        {
            "id": row.get("id", ""),
            "scope": row.get("scope", "global"),
            "member_uid": row.get("memberUid", ""),
            "stream_id": row.get("streamId", ""),
            "name": row.get("name", ""),
            "reason": row.get("reason", ""),
            "until": row.get("until", ""),
            "event_id": row.get("eventId", ""),
            "created_at": row.get("createdAt", ""),
            "created_by": row.get("createdBy", ""),
        },
    )


# --------------------------------------------------------------------------- #
# 公告（通知）
# --------------------------------------------------------------------------- #
def _notice_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "scope": row["scope"],
        "eventId": row["event_id"],
        "title": row["title"],
        "body": row["body"],
        "author": row["author"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def list_notices(
    conn: sqlite3.Connection,
    *,
    scope: str,
    event_id: str = "",
    limit: int = 4,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """分页取公告（最新在前）。返回 ``(当页列表, 总数)``。"""
    where = "scope = ? AND event_id = ?"
    params = (scope, event_id)
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM notices WHERE {where}", params).fetchone()["n"]
    )
    rows = conn.execute(
        f"SELECT * FROM notices WHERE {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
        (*params, max(1, int(limit)), max(0, int(offset))),
    ).fetchall()
    return [_notice_row(row) for row in rows], total


def notice_heads(conn: sqlite3.Connection, limit: int = 400) -> dict[str, dict[str, Any]]:
    """每个 ``(scope, event_id)`` 的最新一条（**轻量字段**，用于「有没有新通知」）。

    只在启动时读一次并留在内存里：状态广播会用到它，不能每次都查库。
    """
    rows = conn.execute(
        "SELECT scope, event_id, id, title, created_at, updated_at FROM notices "
        "ORDER BY seq DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    heads: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['scope']}:{row['event_id']}"
        heads.setdefault(
            key,
            {
                "id": row["id"],
                "scope": row["scope"],
                "title": row["title"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            },
        )
    return heads


def get_notice(conn: sqlite3.Connection, notice_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    return _notice_row(row) if row else None


def save_notice(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    # 序号在这里现取：新建与修改都会拿到一个比现有全部更大的值，
    # 于是「最新的在前面」永远成立，且不依赖时间戳精度
    seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM notices").fetchone()["n"])
    _upsert(
        conn,
        "notices",
        ("id",),
        {
            "id": row.get("id", ""),
            "scope": row.get("scope", "event"),
            "event_id": row.get("eventId", ""),
            "title": row.get("title", ""),
            "body": row.get("body", ""),
            "author": row.get("author", ""),
            "created_at": row.get("createdAt", ""),
            "updated_at": row.get("updatedAt", ""),
            "seq": seq,
        },
    )


def delete_notice(conn: sqlite3.Connection, notice_id: str) -> bool:
    cur = conn.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    return cur.rowcount > 0


def delete_event_notices(conn: sqlite3.Connection, event_id: str) -> int:
    """删除某一届的全部公告（删届时调用——notices 不挂外键，得自己清）。"""
    cur = conn.execute("DELETE FROM notices WHERE scope = 'event' AND event_id = ?", (event_id,))
    return cur.rowcount


def delete_live_ban(conn: sqlite3.Connection, ban_id: str) -> bool:
    cur = conn.execute("DELETE FROM live_bans WHERE id = ?", (ban_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 操作日志
# --------------------------------------------------------------------------- #
# 只留最近这么多条：这是「给人看最近发生了什么」的窗口，不是审计档案，
# 老条目对排查没有价值，却会让库一直长。
ACTIVITY_KEEP = 600


def record_activity(
    conn: sqlite3.Connection,
    *,
    ts: str,
    actor: str,
    actor_uid: str,
    method: str,
    path: str,
    status: int,
) -> None:
    conn.execute(
        "INSERT INTO activity (ts, actor, actor_uid, method, path, status) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, actor, actor_uid, method, path, int(status)),
    )
    # 顺手裁掉超出的老记录（按 id 保留最后 ACTIVITY_KEEP 条）
    conn.execute(
        "DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - ?",
        (ACTIVITY_KEEP,),
    )


def list_activity(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    keep = max(1, min(int(limit or 60), ACTIVITY_KEEP))
    rows = conn.execute(
        "SELECT ts, actor, actor_uid, method, path, status FROM activity ORDER BY id DESC LIMIT ?",
        (keep,),
    ).fetchall()
    return [
        {
            "ts": row["ts"],
            "actor": row["actor"],
            "actorUid": row["actor_uid"],
            "method": row["method"],
            "path": row["path"],
            "status": int(row["status"] or 0),
        }
        for row in rows
    ]
