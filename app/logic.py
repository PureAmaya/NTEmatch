"""赛事状态组装与配置校验。

赛制算法集中在 :mod:`app.tournament`（纯函数）；本模块只负责：

* 参与名单的归一化与筛选（报名池 vs 本届参与名单）
* 组装下发给前端的公开状态（``build_state``）
* 直播地址派生（多机位）
* 配置校验提示（``validate_config``）

本模块不涉及 IO 与 Web 框架。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from . import league, markdown, metrics
from . import tournament as T
from .defaults import SPORT_PRESETS, sport_meta
from .logging_conf import get_logger
from .models import (
    MAX_SIDES,
    Channel,
    Config,
    LiveBan,
    Member,
    Player,
    Round,
    Side,
    StreamConfig,
    Team,
)

log = get_logger("logic")


# --------------------------------------------------------------------------- #
# 报名池与本届参与名单
#
# 报名池（players）是长期名单，可以先把所有人都录进来；
# 参与名单（participants）是**每届单独**勾选的参赛人员。
# 名单为空表示未指定，视为全员参与——旧数据无需迁移即可继续使用。
# --------------------------------------------------------------------------- #
def selection_ids(cfg: Config) -> set[str]:
    """本届手动选定的参与选手 ID 集合（空集 = 未指定）。"""
    return {pid for pid in cfg.participants if pid}


def is_selected(cfg: Config, player_id: str) -> bool:
    """该选手是否参与本届（未指定名单时视为全员参与）。"""
    chosen = selection_ids(cfg)
    return not chosen or player_id in chosen


def joined_players(cfg: Config) -> list[Player]:
    """本届实际参与的选手，保持报名池顺序。"""
    chosen = selection_ids(cfg)
    return [p for p in cfg.players if not chosen or p.id in chosen]


def selectable_players(cfg: Config) -> list[Player]:
    """可被选入参与名单的选手（已启用且有名称）。"""
    return [p for p in cfg.players if p.active and p.name]


def normalize_participants(cfg: Config, ids: Iterable[str]) -> list[str]:
    """规整前端提交的参与名单：去重、丢弃不存在的 ID、按报名池顺序排列。"""
    wanted = {str(pid).strip() for pid in ids if str(pid or "").strip()}
    return [p.id for p in cfg.players if p.id in wanted]


# --------------------------------------------------------------------------- #
# 时间：解析 / 归一化 / 状态推导
#
# 存储统一为**本地无时区**的 ISO 秒级字符串（与 store.now_iso 一致），
# 前端 `new Date(...)` 会直接按本地时间解析，展示与输入不会串时区。
# --------------------------------------------------------------------------- #
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_time(value: str) -> datetime | None:
    """宽松解析时间字符串；无法识别时返回 ``None``。"""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    return None


def normalize_time(value: str) -> str:
    """规整为 ``YYYY-MM-DDTHH:MM:SS``；空值保持空，无法解析时原样返回。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = parse_time(raw)
    return parsed.replace(microsecond=0).isoformat() if parsed else raw


def check_time(value: str, label: str) -> str:
    """校验并规整时间；非法值抛 ``ValueError``，供接口直接回 400。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = parse_time(raw)
    if parsed is None:
        raise ValueError(f"{label}格式不正确，请使用 2026-10-03T20:00 这样的写法")
    return parsed.replace(microsecond=0).isoformat()


def check_order(start: str, end: str, start_label: str = "开始时间", end_label: str = "结束时间") -> None:
    """确保结束时间不早于开始时间；两者需同时有值才有意义。"""
    a, b = parse_time(start), parse_time(end)
    if a is not None and b is not None and b < a:
        raise ValueError(f"{end_label}不能早于{start_label}")


def minutes_between(start: str, end: str) -> int | None:
    """两个时间之间的分钟数；任一为空或顺序颠倒时返回 ``None``。"""
    a, b = parse_time(start), parse_time(end)
    if a is None or b is None or b < a:
        return None
    return int((b - a).total_seconds() // 60)


def round_time_view(rnd: Round) -> dict[str, Any]:
    """一场比赛的时间视图。

    * ``finished``    已结束：已结算，或已登记结束时间
    * ``running``     进行中：状态为 live，或已登记开始时间
    * ``scheduled``   未开始但已排定计划时间
    * ``unscheduled`` 未开始且未排时间
    """
    start, end = normalize_time(rnd.started_at), normalize_time(rnd.finished_at)
    if rnd.status == "done" or end:
        state = "finished"
    elif rnd.status == "live" or start:
        state = "running"
    elif rnd.scheduled_at:
        state = "scheduled"
    else:
        state = "unscheduled"
    return {
        "state": state,
        "scheduledAt": normalize_time(rnd.scheduled_at),
        "startedAt": start,
        "finishedAt": end,
        "durationMinutes": minutes_between(start, end),
        # 登记了结束时间但还没结算比分：前端会提示补录，晋级不受影响
        "pendingSettlement": state == "finished" and rnd.status != "done",
    }


def event_time_view(cfg: Config, progress: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """整届赛事的时间视图（顶栏与总览展示「是否结束 / 何时开始 / 何时结束」）。"""
    evt = cfg.event
    start, end = normalize_time(evt.start_time), normalize_time(evt.end_time)
    now = datetime.now()  # noqa: DTZ005
    start_dt = parse_time(start)
    rows = progress if progress is not None else T.stage_progress(cfg.rounds)
    total = sum(row["total"] for row in rows)
    done = sum(row["done"] for row in rows)
    schedule_done = bool(total) and done >= total
    # 已登记结束时间或标记结束 = 已结束；「赛程已经打完」时不再自称未开赛
    if end or evt.status == "closed":
        state = "finished"
    elif evt.status == "draft" and not done:
        state = "draft"
    elif start_dt is not None and start_dt > now and not done:
        state = "upcoming"
    else:
        state = "running"
    return {
        "state": state,
        "status": evt.status,
        "startAt": start,
        "endAt": end,
        "durationMinutes": minutes_between(start, end),
        # 赛程全部打完但管理员还没登记结束时间时的提示依据
        "scheduleDone": schedule_done,
        "done": done,
        "total": total,
    }


def close_on_champion(data: dict[str, Any]) -> dict[str, Any]:
    """总冠军决出后自动把这一届标记为「已结束」（锦标赛制的收尾器）。

    在 :meth:`app.store.Store.mutate` 的 ``final`` 阶段执行——即所有比分写入与
    上下游阵容重算都完成之后，所以不需要前端再手动点一次「标记结束」。

    只做「未结束 → 已结束」这一件事：已经结束的届原样返回（幂等），手动
    「恢复进行」之后只要不再产生新结果，就不会被重新关上。
    """
    if data.get("rules", {}).get("format", "tournament") != "tournament":
        return data
    event = dict(data.get("event") or {})
    if (event.get("status") or "active") == "closed":
        return data
    try:
        cfg = Config.model_validate(data)
        champion = T.champion_of(cfg.teams, cfg.rounds)
    except Exception:
        log.warning("自动结束本届的判断失败（忽略，不影响比分写入）", exc_info=True)
        return data
    if champion is None:
        return data
    event["status"] = "closed"
    # 结束时间没登记就顺手补上：赛后「用时」与「已结束」的展示都靠它
    if not str(event.get("endTime") or "").strip():
        event["endTime"] = datetime.now().replace(second=0, microsecond=0).isoformat()  # noqa: DTZ005
    log.info("总冠军已决出，自动结束本届 | 冠军=%s", champion.label)
    return {**data, "event": event}


# --------------------------------------------------------------------------- #
# 对外脱敏
#
# 用户端只能看到**名字与头像**等展示信息；
# UUID / QQ / 推流流名（推流凭据）一律不下发，管理端需要时走 /api/private。
# --------------------------------------------------------------------------- #
def public_player(player: Player, *, has_stream: bool | None = None) -> dict[str, Any]:
    """选手的对外结构（脱敏），实现见 :meth:`models.Player.public`。"""
    data = player.public()
    if has_stream is not None:
        data["hasStream"] = has_stream
    return data


# --------------------------------------------------------------------------- #
# 推流标识：只有选手，没有比赛
#
# **选手的推流标识就是他自己的流名**（唯一，在选手名单里设置一次），
# 与「在哪场比赛」无关——选手只要在 OBS 里配一次，整届比赛都不用改地址。
# 哪台机位对应哪场比赛是「组织信息」，由对局里出场的选手决定，
# 观众在直播页按比赛筛选机位即可。
#
# 观看地址（8889 / 8888）可对用户端公开；**推流地址（WHIP）只在管理端出现**。
# --------------------------------------------------------------------------- #
def clean_key(text: str) -> str:
    """推流路径片段：只保留 **ASCII** 字母数字与 - _，避免拼出越界路径。

    为什么显式判 ``isascii()``：``str.isalnum()`` 对中文 / 全角字符**也返回 True**，
    旧实现会把「中文id」原样当成流名——于是推流地址里出现非 ASCII，
    OBS、媒体服务器、以及各种字符串比较（``hmac.compare_digest`` 会直接抛
    TypeError）都得跟着擦屁股。流名就该是主机名 / 路径里那一小撮安全字符。
    """
    return "".join(ch for ch in (text or "").strip() if ch.isascii() and (ch.isalnum() or ch in "-_"))


def check_stream_key(raw: str, label: str = "推流 ID") -> str:
    """校验并规整推流标识：**含非法字符就直接报错**，不静默丢弃。

    静默清洗看着「宽容」，其实更坑：用户填「中文id」，存进去变成「id」，
    推流地址对不上，要排查半天。宁可当场把话说清楚（调用方把 ``ValueError``
    转成 400）。
    """
    text = (raw or "").strip()
    clean = clean_key(text)
    if text and clean != text:
        raise ValueError(
            f"{label}只能使用 ASCII 字母、数字、连字符(-)与下划线(_)；"
            f"不能包含中文、空格或其它符号：{text}"
        )
    return clean


def player_stream_key(player: Player) -> str:
    """选手的推流标识：**选手自己的唯一流名**，与比赛无关。

    直播只按选手区分——没有「本场整体」这种按比赛编号的推流地址，
    免得选手每场都要改一次地址。
    """
    return clean_key(player.stream_key)


def round_player_ids(rnd: Round) -> list[str]:
    """一场比赛出场的全部选手 ID（去重、保持顺序）。"""
    out: list[str] = []
    for side in rnd.sides:
        for pid in side.player_ids:
            if pid not in out:
                out.append(pid)
    return out


def current_round_of(cfg: Config, player_id: str) -> Round | None:
    """选手当前所在的对局：正在进行 > 最近的未开始 > 最近的已结束。"""
    containing = [r for r in cfg.rounds if player_id in round_player_ids(r)]
    if not containing:
        return None
    for status in ("live", "pending"):
        hit = sorted((r for r in containing if r.status == status), key=lambda r: r.index)
        if hit:
            return hit[0]
    return max(containing, key=lambda r: r.index)


# 对外可见地址的字段白名单。全是**媒体服务器的源地址**（不做反代），前端直连：
#   ``webrtc`` = ``<baseUrl>/<流名>``（8889）观看；
#   ``hls``    = ``<hlsBase>/<流名>``（8888）观看。
# 观看地址就是「端口 + 流名」，没有 ``/whep`` 之类的子路由。
PLAY_ENDPOINT_KEYS = ("key", "webrtc", "hls")


# 对外可见的直播配置字段（白名单，避免日后新增字段时又把凭据漏出去）。
# ``verifyTls`` 不是凭据（只是一个证书校验开关），管理端表单要读它，因此也在白名单里；
# ``apiUser`` / ``apiPass``（控制 API 的 Basic 认证）是凭据，绝不在此列。
PUBLIC_STREAM_FIELDS = (
    "enabled",
    "mode",
    "provider",
    "title",
    "note",
    "poster",
    "baseUrl",
    "verifyTls",
)


def public_stream_config(stream: StreamConfig) -> dict[str, Any]:
    """对外可见的直播配置。

    白名单式剥离：默认流名、WHIP 推流地址、HLS 根地址都不下发，
    用户端只需要「是否启用 / 播放模式 / 展示文案 / 播放器同源地址」。
    """
    data = stream.dump()
    return {key: data[key] for key in PUBLIC_STREAM_FIELDS if key in data}


# 推流凭据：只有 WHIP（WebRTC / UDP）——仅管理端可见
PUSH_ENDPOINT_KEYS = ("key", "whipPush")


def push_endpoints(stream: StreamConfig, key: str) -> dict[str, str]:
    """推流地址（WHIP）——**仅管理端可见**，绝不下发用户端。"""
    full = key_endpoints(stream, key)
    return {name: full[name] for name in PUSH_ENDPOINT_KEYS if full.get(name)}


def protocol_sets(stream: StreamConfig) -> list[dict[str, Any]]:
    """推流与观看的**协议列定义**（不含具体地址），供管理端渲染复制表格。

    * ``webrtc``（8889）：``/<流名>/whip`` 推流 + ``/<流名>`` 观看（UDP，延迟最低）；
    * ``hls``（8888）：``/<流名>`` 观看（TCP，抗抖动）。

    只返回**已配置**的协议列。每一列带 ``kind``：``push`` 推流 / ``play`` 观看，
    具体地址按流名放在各行的 ``endpoints`` 里（见 :func:`key_endpoints`）。
    """
    base = (stream.base_url or "").strip()
    hls = (stream.hls_base or "").strip()

    sets: list[dict[str, Any]] = []
    if base:
        sets.append(
            {
                "id": "webrtc",
                "label": "WebRTC · 8889",
                "preferred": True,
                "badge": "优先",
                "note": "推流用 /whip；观众打开不带 /whip 的那一条即可（延迟最低）",
                "columns": [
                    {
                        "key": "whipPush",
                        "label": "WHIP 推流",
                        "kind": "push",
                        "hint": "选手 / 频道在 OBS 里填这个地址；记得把 B 帧设为 0",
                    },
                    {"key": "webrtc", "label": "观看 8889", "kind": "play", "hint": "浏览器直接打开就能看"},
                ],
            }
        )
    if hls:
        sets.append(
            {
                "id": "hls",
                "label": "HLS · 8888",
                "preferred": False,
                "badge": "备选",
                "note": "走 TCP 不易丢帧（延迟 2~10 秒）；观众打开 8888 的那一条即可",
                "columns": [
                    {"key": "hls", "label": "观看 8888", "kind": "play", "hint": "浏览器直接打开就能看"},
                ],
            }
        )
    return sets


def play_endpoints(stream: StreamConfig, key: str) -> dict[str, str]:
    """只取**观看地址**（媒体服务器直连），不含推流地址。

    返回 :data:`PLAY_ENDPOINT_KEYS` 那几个键（8889 与 8888 各一条），数量很少，
    调用方无需再裁剪。
    """
    full = key_endpoints(stream, key)
    return {name: full[name] for name in PLAY_ENDPOINT_KEYS if full.get(name)}


def round_view(cfg: Config, rnd: Round, *, historical: bool = False) -> dict[str, Any]:
    """把一场比赛渲染成前端直接可用的结构。

    ``sides`` 是 2~4 方同场的权威数组；同时保留 ``sideA`` / ``sideB``
    供只看 2 队对阵的界面（对阵图、录分弹窗的兼容路径）继续使用。

    直播：``live`` 表示本场**当前有效**的直播状态——已结束的比赛一律视为
    未直播；查看过往届次（``historical``）时也按关闭处理。
    """
    players = {p.id: p for p in cfg.players}
    teams = {t.id: t for t in cfg.teams}

    def build(side: Side, key: str) -> dict[str, Any]:
        team = teams.get(side.team_id)
        # 积分制的常规局没有固定队伍，此时用「A 队 / B 队」兜底
        label = side.label or (team.label if team else f"{key} 队")
        index = ord(key) - ord("A")
        return {
            "key": key,
            "label": label,
            "short": (team.short if team else "") or label,
            "teamId": side.team_id,
            "color": (team.color if team else "") or T.PALETTE[index % len(T.PALETTE)],
            "score": side.score,
            "points": side.points,
            "rank": side.rank,
            "forfeit": side.forfeit,
            "source": side.source,
            "winner": rnd.winner == key,
            # 出场选手：脱敏后下发（只有名字 / 头像 / 编号等展示信息）
            "players": [public_player(players[pid]) for pid in side.player_ids if pid in players],
        }

    times = round_time_view(rnd)
    sides = [build(side, chr(ord("A") + i)) for i, side in enumerate(rnd.sides)]
    while len(sides) < 2:
        sides.append(build(Side(), chr(ord("A") + len(sides))))
    live_on = bool(rnd.live) and not historical and rnd.status != "done"
    # 本场各选手的机位：标识就是选手自己的流名（与比赛无关，选手只需配一次）
    cast = [
        {
            "playerId": pid,
            "name": players[pid].name or pid,
            "key": player_stream_key(players[pid]),
            "play": play_endpoints(cfg.stream, player_stream_key(players[pid])),
        }
        for pid in round_player_ids(rnd)
        if pid in players and players[pid].stream_key
    ]
    return {
        "index": rnd.index,
        "code": rnd.code,
        "stage": rnd.stage,
        "stageName": T.STAGE_NAMES.get(rnd.stage, rnd.stage),
        "bracketRound": rnd.bracket_round,
        "slot": rnd.slot,
        "label": rnd.label or (f"第 {rnd.index} 局" if rnd.stage == "league" else rnd.code),
        "status": rnd.status,
        "winner": rnd.winner,
        "note": rnd.note,
        "locked": rnd.locked,
        # 时间：计划 / 开始 / 结束 + 推导出的时间状态与用时
        "scheduledAt": times["scheduledAt"],
        "startedAt": times["startedAt"],
        "finishedAt": times["finishedAt"],
        "timeState": times["state"],
        "durationMinutes": times["durationMinutes"],
        "pendingSettlement": times["pendingSettlement"],
        # 用时：优先用手填值，否则回退到起止时间差
        "duration": rnd.duration_minutes or times["durationMinutes"] or 0,
        # 各局小分与直播
        "sets": [item.dump() for item in rnd.sets],
        "live": live_on,
        "livePlaying": live_on and rnd.status == "live",
        "liveNote": rnd.live_note,
        "sideCount": len(sides),
        # 本场的机位：每位选手用他自己的固定流名（没有按比赛编号的推流地址）
        "streams": {"cast": cast},
        "srcA": rnd.src_a,
        "srcB": rnd.src_b,
        "winnerTo": rnd.winner_to,
        "loserTo": rnd.loser_to,
        "sides": sides,
        "sideA": sides[0],
        "sideB": sides[1],
    }


# --------------------------------------------------------------------------- #
# 比赛规则（用户端展示）
#
# 规则不是另写一份文案，而是**从当前赛制参数推导出来**：
# 管理员切换赛制或改动任何参数，用户端看到的条目立即跟着变，不会与实际赛制脱节。
# --------------------------------------------------------------------------- #
def knockout_round_names(size: int, loser_bracket: bool) -> list[str]:
    """淘汰赛逐轮名称，如 ``八强 / 半决赛 / 决赛``。"""
    size = max(2, size)
    count = size.bit_length() - 1
    names: list[str] = []
    for rnd in range(1, count + 1):
        if rnd == count:
            names.append("胜者组决赛" if loser_bracket and size >= 4 else "决赛")
        else:
            names.append(T.wb_round_title(size, rnd))
    return names


# 系列赛的中文说法（BO1 不是「系列赛」）
_SERIES_NAME = {3: "三局两胜", 5: "五局三胜", 7: "七局四胜"}


def series_label(best_of: int) -> str:
    """把 ``best_of`` 说成人话；1（一局定胜负）返回空串。"""
    n = int(best_of or 1)
    if n <= 1:
        return ""
    return _SERIES_NAME.get(n, f"{n} 局 {n // 2 + 1} 胜")


def rulebook(cfg: Config) -> dict[str, Any]:
    """把当前赛制与参数翻译成用户端可读的规则条目。"""
    rules = cfg.rules
    league = rules.format == "league"
    teams = cfg.teams
    rounds = cfg.rounds
    players = joined_players(cfg)
    meta = sport_meta(cfg.event.sport)
    per_match = max(2, min(MAX_SIDES, rules.teams_per_match or 2))
    shape = "组 vs 组" if per_match == 2 else f"{per_match} 队同场"
    loser = bool(rules.loser_bracket)
    sections: list[dict[str, Any]] = []

    # ---- 比法（app/metrics.py）：方向只有一处定义，这里翻译成人话 ----
    time_based = metrics.lower_is_better(rules.metric)
    # 「同分再比什么」在两种比法下不是同一个东西
    tie_break = "完成场次、总用时" if time_based else "净胜分、总得分"
    if time_based:
        verdict = "每局用时短者胜；用时相同视为并列，需人工指定胜方或记平局。"
    elif rules.target_score:
        verdict = f"单局目标分 {rules.target_score} 分，先到者胜。"
    else:
        verdict = "单局不设目标分，按录入比分判定胜负。"

    # ---- 赛制概览 ----
    if league:
        headline = f"积分制 · {rules.team_size}v{rules.team_size} · 共 {rules.total_rounds} 局"
        overview = [
            f"赛制：积分制（不淘汰），每局 {rules.team_size} 人对 {rules.team_size} 人。",
            f"参赛：{len(players)} 名选手，共 {rules.total_rounds} 局。",
        ]
        if rules.include_substitutes:
            overview.append("正式选手不足时启用替补；替补上场同样计入本人成绩。")
        if rules.fair_rotation:
            overview.append("自动排阵：均衡出场，尽量不重复搭档、不重复对手。")
    else:
        headline = (
            f"锦标赛制 · 每队 {rules.team_size} 人 · 小组赛每场 {shape} · "
            f"{'双败' if loser else '单败'}淘汰"
        )
        overview = [
            f"赛制：固定队伍 —— 随机分配队友后全程固定、不换人，每个组 {rules.team_size} 人。",
            f"参赛：{len(players)} 名选手 / {len(teams)} 支队伍。",
        ]
    sections.append({"title": "赛制概览", "items": overview})

    if not cfg.event.ranked:
        # 娱乐模式：规则面板直说「不排名」，避免用户找积分榜
        sections.append(
            {
                "title": "娱乐模式（不排名）",
                "items": [
                    f"本场是娱乐性质的{meta['label']}：只记录{meta['round']}与{meta['score']}。",
                    "不计算名次与积分、不判晋级、不产生冠军。",
                    f"{meta['score']}相同时直接记为平局，不必指定胜方。",
                ],
            }
        )

    groups = sorted({t.group or "A" for t in teams}) if teams else []
    size = T.size_from_rounds(rounds) or (T.bracket_size(len(teams)) if teams else 0)

    if league:
        # ---- 积分与排名 ----
        points = [f"胜 +{rules.points_win}", f"负 +{rules.points_lose}"]
        points.append(f"平 +{rules.points_draw}" if rules.allow_draw else "不允许平局")
        sections.append(
            {
                "title": "积分与排名",
                "items": [
                    "每局积分：" + "、".join(points) + "。",
                    (
                        f"排名依据：总得分 ÷ 出场次数（均分）降序，同分再比{tie_break}；"
                        f"出场不足 {rules.min_rank_played} 局不参与名次。"
                    ),
                    verdict,
                ],
            }
        )
    else:
        # ---- 小组赛 ----
        group_items = [
            (
                f"小组赛：{'分 ' + str(len(groups)) + ' 组' if groups else '按队伍数分组'}"
                f"轮转，每场 {shape}；每队每轮最多出场一次，出场次数保持均衡。"
            ),
            (
                f"排名依据：名次分 —— 同场 {per_match} 队时第 1 名得 {per_match} 分，"
                f"依次递减，最低 1 分；同分再比{tie_break}。"
                if per_match > 2
                else f"排名依据：名次分 —— 胜 2 分、负 1 分；同分再比{tie_break}。"
            ),
            (
                "小组赛允许平局。"
                if rules.allow_draw
                else "小组赛必须分出胜负（不设平局）。"
            ),
            # 「怎么算赢」：比法决定方向，必须写在最显眼的地方
            verdict,
        ]
        if size:
            left = len(teams) - size
            group_items.append(
                f"晋级：各组名次靠前者优先，取总排名前 {size} 名进入淘汰赛"
                + (f"，其余 {left} 支队淘汰。" if left > 0 else "（全部队伍晋级，小组赛决定种子）。")
            )
        else:
            group_items.append("晋级：小组赛结束后按总排名确定晋级名额。")
        sections.append({"title": "小组赛", "items": group_items})

        # ---- 淘汰赛 ----
        knockout_items = []
        if size:
            names = knockout_round_names(size, loser)
            branch = "胜者组逐轮为 " if loser else "逐轮为 "
            knockout_items.append(
                f"淘汰赛：{size} 强{'双败' if loser else '单败'}，{branch}"
                + " → ".join(names)
                + "；对阵恒为 2 队一组。"
            )
        if loser:
            rounds_word = f"（共 {2 * (size.bit_length() - 1) - 2} 轮）" if size >= 4 else ""
            knockout_items.append(
                f"双败淘汰：胜者组落败者进入败者组{rounds_word}，输两场才被淘汰；"
                "败者组比赛成员随比赛进程自动生成。"
            )
            knockout_items.append("总决赛：胜者组冠军 对 败者组冠军，单场定胜负。")
        else:
            knockout_items.append(
                "单败淘汰：输一场即被淘汰（没有败者组），最后一轮直接决出冠军。"
            )
        sections.append({"title": "淘汰赛", "items": knockout_items})

    # ---- 其他 ----
    if league:
        result_note = "比赛结果按录入的成绩自动结算积分；每局独立结算，不影响其它局。"
    elif time_based:
        result_note = "比赛结果按录入的用时自动判定胜负与名次，录入各局用时还能自动汇总局分与总用时。"
    else:
        result_note = "比赛结果按录入的比分自动判定胜负与名次，录入各局小分还能自动汇总局分与总得分。"
    others = [result_note]
    if time_based:
        others.append(
            "用时制：成绩按毫秒存储与比较（界面写 1:23.456 这样的时间）；"
            "没填或填 0 视为「未完赛」，名次垫底。"
        )
    series = series_label(rules.best_of)
    if series:
        others.append(
            f"系列赛：每场 {rules.best_of} 局小局（{series}），"
            f"先赢 {(rules.best_of + 1) // 2} 局小局者赢下整场；"
            "局数与胜负由「各局小分」自动汇总，不需要另外填大比分。"
        )
    if cfg.stream.enabled:
        others.append("直播：每场比赛可单独开启推流，并标注直播选手提示。")
    if cfg.rules.target_score and not league and not time_based:
        others.append(f"单局目标分：{cfg.rules.target_score} 分。")
    sections.append({"title": "其他", "items": others})

    return {
        "headline": headline,
        "sections": sections,
        "facts": {
            "format": "league" if league else "tournament",
            "formatLabel": "积分制" if league else "锦标赛制",
            "teamSize": rules.team_size,
            "teamsPerMatch": per_match,
            "loserBracket": loser,
            "allowDraw": rules.allow_draw,
            "teams": len(teams),
            "players": len(players),
            "groups": len(groups),
            "size": size,
            "targetScore": rules.target_score,
            "metric": metrics.norm(rules.metric),
            "metricLabel": metrics.LABELS[metrics.norm(rules.metric)],
            "metricNote": metrics.DESCRIPTIONS[metrics.norm(rules.metric)],
            "timeBased": time_based,
            "bestOf": rules.best_of,
            "series": series_label(rules.best_of),
            "groupMatches": sum(1 for r in rounds if r.stage == "group"),
            "knockoutMatches": sum(1 for r in rounds if r.stage != "group"),
            "totalRounds": rules.total_rounds,
            "minRankPlayed": rules.min_rank_played,
        },
        "note": cfg.event.rules_text,
        # 赛事信息按 Markdown 渲染（服务端一次渲染，前端直接展示；见 app/markdown.py）
        "noteHtml": markdown.render(cfg.event.rules_text),
    }


# --------------------------------------------------------------------------- #
# 汇总状态
# --------------------------------------------------------------------------- #
def build_state(cfg: Config, *, historical: bool = False) -> dict[str, Any]:
    """组装下发给前端的完整公开状态（不含任何凭据）。

    按 ``rules.format`` 走两套人马：积分制下发 standings，锦标赛制下发 bracket/groups。
    ``historical=True`` 表示这是往届回看，此时比赛一律按「没有直播」渲染。
    """
    progress = T.stage_progress(cfg.rounds)
    state: dict[str, Any] = {
        "revision": cfg.revision,
        "updatedAt": cfg.updated_at,
        "event": cfg.event.dump(),
        # 比赛类型（文案）与排名开关：前端据此换称呼、并决定是否展示排名 / 晋级相关内容
        "sport": sport_meta(cfg.event.sport),
        "sportPresets": [{"key": key, **meta} for key, meta in SPORT_PRESETS.items()],
        "ranked": bool(cfg.event.ranked),
        # 整届的时间状态（是否结束 / 起止时间 / 用时）
        "eventTime": event_time_view(cfg, progress),
        "rules": cfg.rules.dump(),
        # 用户端展示的「比赛规则」：完全由当前赛制与参数推导
        "rulebook": rulebook(cfg),
        # 直播配置：剥掉推流凭据后再下发
        "stream": public_stream_config(cfg.stream),
        "ui": cfg.ui.dump(),
        # 选手：脱敏下发（UUID / QQ / 推流流名不下发用户端）
        "players": [public_player(p) for p in cfg.players],
        "participants": [p.id for p in joined_players(cfg)],
        "participantsSet": bool(selection_ids(cfg)),
        "teams": [t.dump() for t in cfg.teams],
        "rounds": [round_view(cfg, r, historical=historical) for r in cfg.rounds],
        "progress": progress,
        "streams": build_stream_map(cfg),
        "livePlayers": live_stream_player_ids(cfg),
    }
    if cfg.rules.format == "league":
        state.update(_league_state(cfg))
    else:
        state.update(_tournament_state(cfg))
    return state


def _league_state(cfg: Config) -> dict[str, Any]:
    """积分制：均分排名 + 赛程质量。"""
    standings = league.compute_standings(cfg)
    prog = standings["progress"]
    total = prog["total"]
    if not total:
        phase = "idle"
    elif prog["played"] >= total:
        phase = "finished"
    else:
        phase = "playing"
    rounds = cfg.rounds
    return {
        "phase": phase,
        "format": {
            "kind": "league",
            "teamSize": cfg.rules.team_size,
            "totalRounds": total,
            "minRankPlayed": standings["minRankPlayed"],
        },
        "standings": standings,
        "schedule": standings["quality"],
        "current": [round_view(cfg, r) for r in league_live_or_next(rounds)],
    }


def _tournament_state(cfg: Config) -> dict[str, Any]:
    """锦标赛制：小组赛积分表 + 双败对阵图。"""
    teams = cfg.teams
    rounds = cfg.rounds
    by_id = {t.id: t for t in teams}
    tables = T.group_tables(teams, rounds, cfg.rules.metric)
    ranking = T.overall_ranking(tables, cfg.rules.metric)
    size = T.size_from_rounds(rounds) or (T.bracket_size(len(teams)) if teams else 0)
    champion = T.champion_of(teams, rounds)
    return {
        "phase": T.phase_of(rounds),
        "format": {
            "kind": "tournament",
            "teams": len(teams),
            "size": size,
            "maxSize": T.bracket_size(len(teams)) if teams else 0,
            "sizeOptions": T.size_options(len(teams)) if teams else [],
            "groupCount": len(tables),
            "groupMatches": sum(1 for r in rounds if r.stage == "group"),
            "knockoutMatches": sum(1 for r in rounds if r.stage != "group"),
            "groupStageDone": T.group_stage_done(rounds),
        },
        "groups": [{"key": key, "rows": rows} for key, rows in tables.items()],
        "ranking": [
            {"seed": pos + 1, "team": by_id[tid].dump(), "advanced": pos < size}
            for pos, tid in enumerate(ranking)
            if tid in by_id
        ],
        "bracket": T.bracket_view(teams, rounds),
        "current": [round_view(cfg, r) for r in T.live_or_next(rounds)],
        "champion": champion.dump() if champion else None,
        "playerProgress": T.player_progress(teams, rounds),
    }


def league_live_or_next(rounds: list[Round]) -> list[Round]:
    """积分制：正在进行的局；没有则给出最靠前的待赛局。"""
    playing = [r for r in rounds if r.status == "live"]
    if playing:
        return playing
    pending = [r for r in rounds if r.status == "pending"]
    return pending[:1]


def tournament_plan(cfg: Config, **overrides: Any) -> dict[str, Any]:
    """按给定参数**预估**赛程结构（只读，不落库）。

    用于「快速创建分组」的实时预览：参赛人数 → 队伍数 → 小组数 / 每组队数 →
    淘汰赛规模（8 强 / 16 强 / 32 强…）→ 总场次，让管理员在动手前就看清结构。

    可覆盖参数：``team_size`` / ``teams_per_match`` / ``loser_bracket`` /
    ``group_count`` / ``knockout_size`` / ``merge_remainder`` / ``seed``。
    """
    mapping = {
        "team_size": "team_size",
        "teams_per_match": "teams_per_match",
        "loser_bracket": "loser_bracket",
        "group_count": "group_count",
        "knockout_size": "knockout_size",
    }
    patch = {
        field: overrides[key]
        for key, field in mapping.items()
        if overrides.get(key) is not None
    }
    rules = cfg.rules.model_copy(update=patch) if patch else cfg.rules
    players = joined_players(cfg)
    # 人数不够时不要抛错——预览要给出「还差多少人」而不是报错
    teams: list[Team] = []
    team_warnings: list[str] = []
    plan_warnings: list[str] = []
    summary: dict[str, Any] = {}
    ok = True
    try:
        teams, team_warnings = T.auto_form_teams(
            players,
            rules.team_size,
            overrides.get("seed"),
            merge_remainder=bool(overrides.get("merge_remainder")),
            allow_substitutes=rules.format == "league",
        )
        _rounds, plan_warnings, summary = T.build_tournament(teams, rules)
    except ValueError as exc:
        plan_warnings = [str(exc)]
        ok = False
    groups: dict[str, int] = {}
    for team in teams:
        groups[team.group or "A"] = groups.get(team.group or "A", 0) + 1
    size = int(summary.get("size") or 0)
    return {
        "ok": ok,
        "format": rules.format,
        "players": len(players),
        "teams": len(teams),
        "teamSize": rules.team_size,
        "teamsPerMatch": rules.teams_per_match,
        "loserBracket": bool(rules.loser_bracket),
        "groupCount": len(groups),
        "groupSizes": [{"key": key, "teams": count} for key, count in sorted(groups.items())],
        "size": size,
        "sizeOptions": T.size_options(len(teams)) if teams else [],
        "knockoutRounds": knockout_round_names(size, bool(rules.loser_bracket)) if size else [],
        "groupMatches": summary.get("groupMatches", 0),
        "knockoutMatches": summary.get("knockoutMatches", 0),
        "total": summary.get("total", 0),
        "warnings": [*team_warnings, *plan_warnings],
    }


def validate_config(cfg: Config) -> list[str]:
    """返回配置层面的提示信息（不阻断保存，仅提示）。按赛制分别检查。"""
    issues: list[str] = []
    per_team = max(1, cfg.rules.team_size or 2)
    need = per_team * 2
    joined = joined_players(cfg)
    meta = sport_meta(cfg.event.sport)

    if not cfg.event.ranked:
        # 娱乐模式：只记录，不排名 / 不晋级
        issues.append(
            f"娱乐模式（{meta['label']} · 不排名）：只记录{meta['round']}与{meta['score']}，"
            "不计算名次、不判晋级、不产生冠军；胜负可留空（分不出时记为平局）。"
        )
        if cfg.rules.format == "tournament":
            issues.append(
                "娱乐模式下锦标赛制没有意义（淘汰赛必须有胜者才能推进）；"
                "建议改用积分制逐场记录，或直接把排名开关打开。"
            )

    if not cfg.players:
        # 全新的一届最容易卡在这里：页面各处都空着，却没人说「先去加选手」
        issues.append(
            f"还没有录入任何{meta['participant']}：先到「{meta['participant']}」页添加，"
            "再勾选本届参与名单。"
        )
    elif len(joined) < need:
        # 注意：参与名单留空 = 全员参与（见 joined_players），所以这里只可能是真的不够人
        issues.append(f"参与选手 {len(joined)} 人，不足 {need} 人（{per_team} 人一队至少需要 2 队）。")

    chosen = selection_ids(cfg)
    if chosen:
        known = {p.id for p in cfg.players}
        missing = chosen - known
        if missing:
            issues.append(f"参与名单中有 {len(missing)} 个已不存在的选手，请重新保存名单。")
        idle = [p for p in cfg.players if p.id in chosen and (not p.active or not p.name)]
        if idle:
            names = "、".join(p.display_name for p in idle[:5])
            issues.append(f"参与名单中的 {names} 未启用或缺名称，不会被排入比赛。")

    ids = [p.id for p in cfg.players]
    if len(set(ids)) != len(ids):
        issues.append("存在重复的选手 ID，请检查名单。")

    # 历史数据里可能存着非 ASCII 流名（旧版 clean_key 把中文也留下了）。
    # 载入不能因此报错，但必须**说出来**：这类流名的推流地址根本用不了。
    bad_keys = [
        p.display_name for p in cfg.players if p.stream_key and clean_key(p.stream_key) != p.stream_key
    ]
    if bad_keys:
        issues.append(
            f"{'、'.join(bad_keys[:5])} 的推流流名含非 ASCII 字符（中文 / 空格 / 符号），"
            "推流地址不可用，请改成字母、数字、连字符(-)或下划线(_)。"
        )

    # 推流流名必须唯一：重复会让两个选手推/播同一个地址，直接串流
    seen_keys: dict[str, list[str]] = {}
    for player in cfg.players:
        if player.stream_key:
            seen_keys.setdefault(player.stream_key, []).append(player.display_name)
    duplicated = {key: who for key, who in seen_keys.items() if len(who) > 1}
    if duplicated:
        detail = "；".join(f"{key}（{'、'.join(who)}）" for key, who in sorted(duplicated.items()))
        issues.append(f"推流流名重复：{detail}。请为每位选手指定互不相同的流名，否则会串流。")

    if cfg.rules.format == "league":
        if joined and len(joined) % per_team:
            issues.append(
                f"参与选手 {len(joined)} 人不是 {per_team} 的整数倍，每局仍按 {per_team}v{per_team} 排阵。"
            )
        if cfg.rounds:
            stale: set[str] = set()
            for rnd in cfg.rounds:
                if rnd.status == "done" or rnd.locked:
                    continue
                stale.update(pid for pid in (rnd.side_a.player_ids + rnd.side_b.player_ids) if not is_selected(cfg, pid))
            if stale:
                issues.append(
                    f"未开赛对局中仍有 {len(stale)} 名非参与选手，重新保存参与名单时会自动移出。"
                )
        return issues

    # ---- 锦标赛制：必须有固定队伍（且不接受替补）----
    substitutes = [p for p in joined if p.substitute]
    if substitutes:
        names = "、".join(p.display_name for p in substitutes[:5])
        issues.append(
            f"锦标赛制不支持替补：{names} 不会进入队伍（固定队伍全程不换人；只有积分制才需要替补）。"
        )
    eligible = [p for p in joined if not p.substitute]
    if eligible and len(eligible) % per_team:
        issues.append(
            f"可组队选手 {len(eligible)} 人不是 {per_team} 的整数倍，"
            f"将有 {len(eligible) % per_team} 人无法组队。"
        )
    if cfg.teams:
        known = {p.id for p in cfg.players}
        empty = [t.label for t in cfg.teams if not t.player_ids]
        if empty:
            issues.append(f"{'、'.join(empty[:5])} 还没有队员，请补人或在组队台删除该队。")
        thin = [t.label for t in cfg.teams if 0 < len(t.player_ids) < per_team]
        if thin:
            issues.append(
                f"{'、'.join(thin[:5])} 人数少于 {per_team} 人：若届时到不齐人，"
                "可在该场比赛点「弃权」直接让对手晋级。"
            )
        sizes = sorted({len(t.player_ids) for t in cfg.teams if t.player_ids})
        if len(sizes) > 1:
            issues.append(
                f"各队人数不一致（{'/'.join(f'{n} 人' for n in sizes)}），"
                "人数少的队伍会以少打多；确认无误可忽略。"
            )
        if cfg.rules.teams_per_match > len(cfg.teams):
            issues.append(
                f"每场同场 {cfg.rules.teams_per_match} 支队，但只有 {len(cfg.teams)} 支队，"
                "小组赛会按实际队数进行。"
            )
        unknown = {pid for t in cfg.teams for pid in t.player_ids} - known
        if unknown:
            issues.append(f"已有 {len(unknown)} 名队员已从报名池移除，建议重新组队。")
    elif joined and len(joined) >= need:
        issues.append("尚未组队：确定参与名单后执行「随机组队」生成固定队伍。")

    if not cfg.teams or not cfg.rounds:
        return issues
    size = T.size_from_rounds(cfg.rounds)
    if size and len(cfg.teams) < size:
        issues.append(f"淘汰赛规模为 {size} 强，但只有 {len(cfg.teams)} 支队伍，请重新生成赛程。")

    # ---- 系列赛（BO）与录入是否对得上 ----
    # 判定本身不需要额外规则（填了各局小分就是「谁赢的局多谁赢」，见 judge_round），
    # 这里只负责把「对不上」的情况说出来：局数超了、或者赛制没设 BO 却记了多局。
    best_of = int(cfg.rules.best_of or 1)
    if best_of > 1:
        over = [
            r for r in cfg.rounds if len(r.sets) > best_of and len(r.sides) == 2
        ]
        if over:
            names = "、".join((r.label or r.code) for r in over[:4])
            issues.append(
                f"{names} 记录的小局数超过 BO{best_of}（一场最多 {best_of} 局），"
                "多出来的局不会被计入胜负，建议核对后删掉。"
            )
        # 「已经分出胜负、后面还接着记」的场次：小局的顺序是有意义的，
        # 所以从前往后数，谁先到 ⌈n/2⌉ 局就是终结点，之后的记录都不该存在。
        half = (best_of + 1) // 2
        trailing: list[Round] = []
        for rnd in cfg.rounds:
            if len(rnd.sides) != 2:
                continue
            wins = [0, 0]
            for i, item in enumerate(rnd.sets):
                # 每局谁赢由比法决定（计分制比大、用时制比小，见 app/metrics.py）
                left = metrics.value_key(item.a, cfg.rules.metric)
                right = metrics.value_key(item.b, cfg.rules.metric)
                if left < right:
                    wins[0] += 1
                elif right < left:
                    wins[1] += 1
                if max(wins) >= half and i < len(rnd.sets) - 1:
                    trailing.append(rnd)
                    break
        if trailing:
            names = "、".join((r.label or r.code) for r in trailing[:4])
            issues.append(
                f"{names} 已经先到 {half} 局（胜负已定），后面还记了小局，请核对。"
            )
    elif any(len(r.sets) > 1 for r in cfg.rounds):
        issues.append(
            "有场次记了多局小分，但赛制是「一局定胜负（BO1）」："
            "按现有规则会以「赢的局数」作为局分。若本来就想打三局两胜，"
            "请到「赛制」里把系列赛改成 BO3。"
        )

    if metrics.lower_is_better(cfg.rules.metric):
        # 用时制下 0 = 未完赛 / 退赛：已分出胜负却有一方没成绩，多半是漏填。
        # 弃权方本来就记 0，要排除掉，否则每次弃权都会冒一条无意义的提示。
        blank = [
            r
            for r in cfg.rounds
            if r.status == "done"
            and r.winner not in ("", "DRAW")
            and any(
                not side.forfeit
                and metrics.round_total(side.score, side.points, bool(r.sets)) <= 0
                for side in r.sides
            )
        ]
        if blank:
            names = "、".join((r.label or r.code) for r in blank[:4])
            issues.append(
                f"{names} 有一方没有用时（0 = 未完赛）：确认是退赛，还是漏填了。"
            )
    return issues


# --------------------------------------------------------------------------- #
# 直播地址派生（按流名生成推流 / 播放地址，支持多组并行各自推流）
# --------------------------------------------------------------------------- #
def key_endpoints(stream: StreamConfig, key: str) -> dict[str, str]:
    """按流名派生**推流与观看**的源站地址（媒体服务器直连，不做反代）。

    一共三个地址，都只在端口上按流名区分，没有 ``/whep`` 这类子路由：

    * 推流（WHIP，8889）：``{baseUrl}/{流名}/whip``；
    * 观看（WebRTC，8889）：``{baseUrl}/{流名}``；
    * 观看（HLS，8888）：``{hlsBase}/{流名}``。

    ``whipPush`` 属于凭据，只走 :func:`push_endpoints` 下发管理端；
    两个观看地址走 :func:`play_endpoints`，可对用户端公开。
    """
    key = (key or "").strip().strip("/")
    if not key:
        return {}
    base = (stream.base_url or "").rstrip("/")
    hls = (stream.hls_base or "").rstrip("/")
    return {
        "key": key,
        # —— 8889：推流多一个 /whip，观看就是流名本身 ——
        "whipPush": f"{base}/{key}/whip" if base else "",
        "webrtc": f"{base}/{key}" if base else "",
        # —— 8888：只能观看 ——
        "hls": f"{hls}/{key}" if hls else "",
    }


def build_stream_map(cfg: Config) -> dict[str, dict[str, Any]]:
    """选手 ID → 其机位的**播放**地址集合。

    地址只由**选手自己的流名**决定（与比赛无关），所以同一个地址在整届赛事里
    都是这位选手的机位；附带他当前所在的对局，供直播页按比赛分组显示。
    推流地址不在这里出现。
    """
    rooms: dict[str, dict[str, Any]] = {}
    for player in cfg.players:
        key = player_stream_key(player)
        if not key:
            continue
        rnd = current_round_of(cfg, player.id)
        room = play_endpoints(cfg.stream, key)
        room["roundCode"] = rnd.code if rnd is not None else ""
        room["roundLabel"] = (rnd.label or rnd.code) if rnd is not None else ""
        rooms[player.id] = room
    return rooms


def channel_view(cfg: Config, channel: Channel) -> dict[str, Any]:
    """把一个成员频道渲染成前端直接可用的结构（**脱敏**）。

    与选手一致：公开状态里不含 QQ 与推流凭据，只给播放地址；
    头像走 ``/api/avatar/c/<频道 ID>``，客户端请求里不会出现 QQ 号。
    """
    key = clean_key(channel.stream_key)
    return {
        "id": channel.id,
        "name": channel.name or channel.id,
        "title": channel.title,
        "server": channel.server,
        "role": channel.role,
        "description": channel.description,
        "tags": list(channel.tags),
        "link": channel.link,
        "color": channel.color,
        "sort": channel.sort,
        "featured": bool(channel.featured),
        "active": bool(channel.active),
        "avatar": channel.avatar,
        "hasAvatar": channel.has_avatar_source,
        "hasStream": bool(key),
        # 观看地址（8889 / 8888）——源站直连，不含推流凭据
        "play": play_endpoints(cfg.stream, key) if key else {},
    }


def channel_views(cfg: Config, channels: list[Channel]) -> list[dict[str, Any]]:
    """全部成员频道的公开视图（含已停用的）。

    已停用的频道由**前端**对访客隐藏、对管理员保留（否则管理员把频道停用后
    就再也看不到、无法重新启用）。顺序由 store 保证：置顶优先，再按 sort / id。
    """
    return [channel_view(cfg, ch) for ch in channels]


def live_stream_player_ids(cfg: Config) -> list[str]:
    """正在比赛（所在对局为 live）且已配置推流流名的选手 ID。"""
    playing: set[str] = set()
    for rnd in cfg.rounds:
        if rnd.status == "live":
            playing.update(rnd.side_a.player_ids)
            playing.update(rnd.side_b.player_ids)
    return [p.id for p in cfg.players if p.stream_key and p.id in playing]


# --------------------------------------------------------------------------- #
# 成员（全局账号）与直播封禁
# --------------------------------------------------------------------------- #
def ban_active(ban: LiveBan, now: datetime | None = None) -> bool:
    """封禁当前是否生效：``until`` 为空 = 永久；否则到期自动失效。"""
    until = (ban.until or "").strip()
    if not until:
        return True
    parsed = parse_time(until)
    if parsed is None:
        return True
    return parsed > (now or datetime.now())  # noqa: DTZ005


def member_ban(bans: list[LiveBan], member: Member, event_id: str = "") -> LiveBan | None:
    """成员当前生效的封禁（全局优先，其次永久优先、解禁越晚越优先）。"""
    now = datetime.now()  # noqa: DTZ005
    hits: list[LiveBan] = []
    for ban in bans:
        if not ban_active(ban, now):
            continue
        hit_uid = bool(ban.member_uid) and ban.member_uid == member.uid
        hit_stream = bool(ban.stream_id) and bool(member.stream_id) and ban.stream_id == member.stream_id
        if not (hit_uid or hit_stream):
            continue
        # 赛事级封禁只作用于它所属的那一届（其它届不受影响）
        if ban.scope == "event" and event_id and ban.event_id != event_id:
            continue
        if ban.scope == "event" and not ban.event_id:
            continue
        hits.append(ban)
    if not hits:
        return None
    hits.sort(key=lambda b: (b.scope != "global", bool(b.until), b.until))
    return hits[0]


def duplicate_streams(members: list[Member], channels: list[Channel]) -> dict[str, list[str]]:
    """推流 ID / 流名重复检查（成员之间、以及成员与传统频道之间）。

    重复会让两方推到同一个地址（串流），因此管理端要高亮提示管理员改掉。
    返回 ``{流名: [占用者说明, …]}``（只含真正重复的）。
    """
    seen: dict[str, list[str]] = {}
    for member in members:
        key = clean_key(member.stream_id)
        if key:
            seen.setdefault(key, []).append(f"成员 {member.display_name}")
    for channel in channels:
        key = clean_key(channel.stream_key)
        if key:
            seen.setdefault(key, []).append(f"频道 {channel.display_name}")
    return {key: who for key, who in seen.items() if len(who) > 1}


def ban_view(ban: LiveBan) -> dict[str, Any]:
    """封禁的对外结构（不含执行者等内部字段）。"""
    return {
        "id": ban.id,
        "scope": ban.scope,
        "memberUid": ban.member_uid,
        "streamId": ban.stream_id,
        "name": ban.name,
        "reason": ban.reason,
        "until": ban.until,
        "eventId": ban.event_id,
        "createdAt": ban.created_at,
    }


def player_round_map(cfg: Config) -> dict[str, Round]:
    """一次遍历建立「选手 ID → 当前所在对局」，语义与 :func:`current_round_of` 一致。

    对「整份成员列表」批量渲染时用它替代逐人扫描，避免 O(成员数 × 对局数)。
    """
    live: dict[str, Round] = {}
    pending: dict[str, Round] = {}
    latest: dict[str, Round] = {}
    for rnd in sorted(cfg.rounds, key=lambda r: r.index):
        for pid in round_player_ids(rnd):
            latest[pid] = rnd
            if rnd.status == "live" and pid not in live:
                live[pid] = rnd
            elif rnd.status == "pending" and pid not in pending:
                pending[pid] = rnd
    return {pid: (live.get(pid) or pending.get(pid) or rnd) for pid, rnd in latest.items()}


def member_round(
    cfg: Config,
    member: Member,
    *,
    player: Player | None = None,
    rounds: dict[str, Round] | None = None,
) -> Round | None:
    """成员当前所在的比赛：按 ``memberUid`` 关联选手，流名相同也可兜底关联。"""
    if player is None:
        if not cfg.players:
            return None
        player = next(
            (
                p
                for p in cfg.players
                if (member.uid and p.member_uid == member.uid)
                or (member.stream_id and p.stream_key == member.stream_id)
            ),
            None,
        )
    if player is None:
        return None
    if rounds is not None:
        return rounds.get(player.id)
    return current_round_of(cfg, player.id)


def member_view(
    cfg: Config,
    member: Member,
    *,
    bans: list[LiveBan] | None = None,
    live_keys: set[str] | frozenset[str] | None = None,
    event_id: str = "",
    historical: bool = False,
    player: Player | None = None,
    rounds: dict[str, Round] | None = None,
) -> dict[str, Any]:
    """把一位成员渲染成前端直接可用的「直播间」结构（**脱敏**）。

    含：展示信息、观看地址、是否在推流、封禁状态、以及他当前所在的比赛
    （用于「直播间里显示比赛信息」）。密钥 / 令牌永不出现在这里。

    ``player`` / ``rounds`` 是批量渲染时的预计算入参（见 :func:`player_round_map`），
    单个人渲染时可省略。
    """
    data = member.public()
    key = clean_key(member.stream_id)
    live = bool(key) and not historical and key in (live_keys or set())
    ban = None if historical else member_ban(list(bans or []), member, event_id)
    rnd = None if historical else member_round(cfg, member, player=player, rounds=rounds)
    data.update(
        {
            "live": live,
            "play": play_endpoints(cfg.stream, key) if key else {},
            "roundCode": rnd.code if rnd is not None else "",
            "roundLabel": (rnd.label or rnd.code) if rnd is not None else "",
            "banned": (
                {
                    "id": ban.id,
                    "scope": ban.scope,
                    "reason": ban.reason,
                    "until": ban.until,
                    "eventId": ban.event_id,
                }
                if ban is not None
                else None
            ),
        }
    )
    return data


def member_views(
    cfg: Config,
    members: list[Member],
    *,
    bans: list[LiveBan] | None = None,
    live_keys: set[str] | frozenset[str] | None = None,
    event_id: str = "",
    historical: bool = False,
) -> list[dict[str, Any]]:
    """全部成员的公开直播间视图（批量渲染：选手索引与对局映射只算一次）。"""
    if not members:
        return []
    by_member = {p.member_uid: p for p in cfg.players if p.member_uid}
    by_stream = {p.stream_key: p for p in cfg.players if p.stream_key}
    rounds = player_round_map(cfg) if not historical else None
    return [
        member_view(
            cfg,
            m,
            bans=bans,
            live_keys=live_keys,
            event_id=event_id,
            historical=historical,
            player=by_member.get(m.uid) or by_stream.get(m.stream_id),
            rounds=rounds,
        )
        for m in members
    ]
