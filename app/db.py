"""SQLite 持久层：多届赛事的读写。

表结构（全部按届隔离，``events.id`` 为主键，子表通过外键级联删除）::

    events          届元信息 + 统计（名称/状态/规模/已赛局数/榜首）
    event_rules     赛制
    event_ui        界面展示配置
    event_stream    直播根地址与播放模式
    event_admin     管理 KEY（明文或 sha256）
    players         选手（含 UUID / 推流流名 / 替补标记）
    event_participants 本届手动参与名单（有序；空 = 全员参与）
    teams           队伍
    team_players    队伍成员（有序）
    rounds          对局（含各局小分 / 用时 / 直播开关）
    round_sides     对局各方（2~4 队同场：队伍、标签、比分、得分、名次）
    round_players   对局出场选手（有序）
    meta            全局键值（当前届 ID 等）

约定：布尔以 0/1 存储，列表顺序用 ``position`` 列保存；
``save_event`` 在同一事务内「覆盖式重写」某一届（数据量小，简单且不会写脏）。
"""

from __future__ import annotations

import json
import sqlite3
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
  include_substitutes INTEGER NOT NULL DEFAULT 1,
  fair_rotation       INTEGER NOT NULL DEFAULT 1,
  min_rank_played     INTEGER NOT NULL DEFAULT 5,
  group_count         INTEGER NOT NULL DEFAULT 0,
  knockout_size       INTEGER NOT NULL DEFAULT 0,
  teams_per_match     INTEGER NOT NULL DEFAULT 2,
  loser_bracket       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS event_ui (
  event_id       TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  accent         TEXT NOT NULL DEFAULT 'cyan',
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
  mode       TEXT NOT NULL DEFAULT 'auto',
  verify_tls INTEGER NOT NULL DEFAULT 1,
  whip_push  TEXT NOT NULL DEFAULT '',
  poster     TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_admin (
  event_id   TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  key        TEXT NOT NULL DEFAULT '',
  key_sha256 TEXT NOT NULL DEFAULT ''
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
-- key_sha256 / bearer_sha256 是密钥与 WHIP Bearer 令牌的哈希，明文永不落库。
CREATE TABLE IF NOT EXISTS members (
  uid           TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  qq            TEXT NOT NULL DEFAULT '',
  avatar        TEXT NOT NULL DEFAULT '',
  game_uuid     TEXT NOT NULL DEFAULT '',
  stream_id     TEXT NOT NULL DEFAULT '',
  room_title    TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  permission    TEXT NOT NULL DEFAULT 'member',
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
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
    },
    "event_stream": {
        "verify_tls": "INTEGER NOT NULL DEFAULT 1",
        # MediaMTX 控制 API：用来查「谁真的在推流」
        "api_base": "TEXT NOT NULL DEFAULT ''",
        # 控制 API 的 Basic 认证（mediamtx.yml 里配了 authInternalUsers 才需要）
        "api_user": "TEXT NOT NULL DEFAULT ''",
        "api_pass": "TEXT NOT NULL DEFAULT ''",
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
    "event_stream": ("rtmp_base", "rtsp_base", "rtmp_push", "rtsp_url", "hls_url", "flv_url"),
}


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
    admin = data.get("admin", {})
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
            id, name, status, title, subtitle, venue, organizer, start_time, end_time,
            locked, locked_at, rules_text, logo_text, owner_uid, hidden, sport, ranked,
            created_at, updated_at, revision, players_count, rounds_count, played_count, champion
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, status = excluded.status, title = excluded.title,
            subtitle = excluded.subtitle, venue = excluded.venue, organizer = excluded.organizer,
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
            "include_substitutes": int(bool(rules.get("includeSubstitutes", True))),
            "fair_rotation": int(bool(rules.get("fairRotation", True))),
            "min_rank_played": rules.get("minRankPlayed", 5),
            "group_count": rules.get("groupCount", 0),
            "knockout_size": rules.get("knockoutSize", 0),
            "teams_per_match": rules.get("teamsPerMatch", 2),
            "loser_bracket": int(bool(rules.get("loserBracket", True))),
        },
    )
    _upsert(
        conn,
        "event_ui",
        ("event_id",),
        {
            "event_id": event_id,
            "accent": ui.get("accent", "cyan"),
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
            "enabled": int(bool(stream.get("enabled", True))),
            "provider": stream.get("provider", "mediamtx"),
            "base_url": stream.get("baseUrl", ""),
            "api_base": stream.get("apiBase", ""),
            # 控制 API 的 Basic 认证（属于凭据，只在管理端下发）
            "api_user": stream.get("apiUser", ""),
            "api_pass": stream.get("apiPass", ""),
            "hls_base": stream.get("hlsBase", ""),
            "stream_key": stream.get("streamKey", "stream"),
            "mode": stream.get("mode", "auto"),
            # 是否校验上游 HTTPS 证书（自签名证书时关闭）
            "verify_tls": int(bool(stream.get("verifyTls", True))),
            "whip_push": stream.get("whipPush", ""),
            "poster": stream.get("poster", ""),
            "title": stream.get("title", ""),
            "note": stream.get("note", ""),
        },
    )
    _upsert(
        conn,
        "event_admin",
        ("event_id",),
        {
            "event_id": event_id,
            "key": admin.get("key", ""),
            "key_sha256": admin.get("keySha256", ""),
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
        SELECT id, name, status, title, subtitle, start_time, end_time,
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
    admin = conn.execute("SELECT * FROM event_admin WHERE event_id = ?", (event_id,)).fetchone()

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
            "includeSubstitutes": bool(rules["include_substitutes"]) if rules else True,
            "fairRotation": bool(rules["fair_rotation"]) if rules else True,
            "minRankPlayed": rules["min_rank_played"] if rules else 5,
            "groupCount": rules["group_count"] if rules else 0,
            "knockoutSize": rules["knockout_size"] if rules else 0,
            "teamsPerMatch": rules["teams_per_match"] if rules else 2,
            "loserBracket": bool(rules["loser_bracket"]) if rules else True,
        },
        "ui": {
            "accent": ui["accent"] if ui else "cyan",
            "showQq": bool(ui["show_qq"]) if ui else True,
            "showAvatar": bool(ui["show_avatar"]) if ui else True,
            "revealResults": bool(ui["reveal_results"]) if ui else True,
            "ticker": ui["ticker"] if ui else "",
        },
        # 直播配置：没有记录时返回空字典，让模型默认值（HTTPS）生效；
        # 有记录时把仍是旧默认 http:// 的地址升到 https（见 _upgrade_stream_https）。
        "stream": _upgrade_stream_https(
            {
                "enabled": bool(stream["enabled"]),
                "provider": stream["provider"],
                "baseUrl": stream["base_url"],
                "apiBase": stream["api_base"],
                "apiUser": stream["api_user"],
                "apiPass": stream["api_pass"],
                "hlsBase": stream["hls_base"],
                "streamKey": stream["stream_key"],
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
        "admin": {
            "key": admin["key"] if admin else "",
            "keySha256": admin["key_sha256"] if admin else "",
        },
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
        "note": row["note"],
        "permission": row["permission"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
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
            "note": row.get("note", ""),
            "permission": row.get("permission", "member"),
            "active": int(bool(row.get("active", True))),
            "created_at": row.get("createdAt", ""),
            "updated_at": row.get("updatedAt", ""),
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


def delete_live_ban(conn: sqlite3.Connection, ban_id: str) -> bool:
    cur = conn.execute("DELETE FROM live_bans WHERE id = ?", (ban_id,))
    return cur.rowcount > 0
