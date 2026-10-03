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

from . import league
from . import tournament as T
from .logging_conf import get_logger
from .models import MAX_SIDES, Channel, Config, Player, Round, Side, StreamConfig, Team

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
# 播放地址（WHEP / HLS）可对用户端公开；**推流地址（WHIP / RTMP）只在管理端出现**。
# --------------------------------------------------------------------------- #
def clean_key(text: str) -> str:
    """推流路径片段：只保留字母数字与 - _，避免拼出越界路径。"""
    return "".join(ch for ch in (text or "").strip() if ch.isalnum() or ch in "-_")


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


# 播放相关字段白名单：推流凭据（whipPush / rtmpPush）绝不外发。
# 全是**媒体服务器的源地址**（不做反代），前端直连：
#   ``page`` = 自带播放页（内嵌观看）、``whep`` = WebRTC 拉流、``hls`` = HLS。
PLAY_ENDPOINT_KEYS = ("key", "page", "whep", "hlsPage", "hls")


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

    白名单式剥离：默认流名、WHIP / RTMP 推流地址、RTMP / HLS 源地址都不下发，
    用户端只需要「是否启用 / 播放模式 / 展示文案 / 播放器同源地址」。
    """
    data = stream.dump()
    return {key: data[key] for key in PUBLIC_STREAM_FIELDS if key in data}


# 推流凭据：WHIP（WebRTC）与 RTMP / RTSP（TCP）——仅管理端可见
PUSH_ENDPOINT_KEYS = ("key", "whipPush", "rtmpPush", "rtspPush")


def push_endpoints(stream: StreamConfig, key: str) -> dict[str, str]:
    """推流地址（WHIP / RTMP / RTSP）——**仅管理端可见**，绝不下发用户端。"""
    full = key_endpoints(stream, key)
    return {name: full[name] for name in PUSH_ENDPOINT_KEYS if full.get(name)}


def protocol_sets(stream: StreamConfig) -> list[dict[str, Any]]:
    """**两套协议**的列定义（不含具体地址），供管理端渲染复制表格。

    两套都能推流与播放，各取所长：

    * ``webrtc``：WHIP 推流 / WHEP 播放（UDP）——延迟最低，弱网下可能抖；
    * ``tcp``：RTMP 推流、RTSP 推·播 / HLS 播放（TCP）——抗抖动，
      其中 HLS 任何浏览器都能直接播，RTSP / RTMP 供 VLC、ffplay、OBS 等工具使用。

    只返回**已配置**的协议列（没填 RTSP 根地址就不会出现 RTSP 列）。
    每一列带 ``kind``：``push`` 推流 / ``play`` 播放 / ``both`` 推播同址，
    具体地址按流名放在各行的 ``endpoints`` 里（见 :func:`key_endpoints`）。
    """
    base = (stream.base_url or "").strip()
    rtmp = (stream.rtmp_base or "").strip()
    rtsp = (stream.rtsp_base or "").strip()
    hls = (stream.hls_base or "").strip()

    webrtc_cols = [
        {
            "key": "whipPush",
            "label": "WHIP 推流（优先）",
            "kind": "push",
            "hint": "优先用这个：OBS 30+ 原生支持，记得把 B 帧设为 0",
        },
        {"key": "whep", "label": "WHEP 播放", "kind": "play", "hint": "网页播放器"},
    ]
    tcp_cols = [
        {"key": "rtmpPush", "label": "RTMP 推流", "kind": "push", "hint": "备选：OBS 经典模式"},
        {"key": "rtspPush", "label": "RTSP 推 · 播", "kind": "both", "hint": "备选：VLC / ffplay"},
        # HLS 分两条：给人看的播放页（地址到 /<流名>/ 为止）与给播放器用的列表
        {"key": "hlsPage", "label": "HLS 播放页", "kind": "play", "hint": "浏览器直接打开就能看，地址到 /<流名>/ 为止"},
        {"key": "hls", "label": "HLS 播放列表", "kind": "play", "hint": "播放器 / hls.js 用（…/index.m3u8）"},
    ]

    sets: list[dict[str, Any]] = []
    if base:
        sets.append(
            {
                "id": "webrtc",
                "label": "WebRTC 套 · UDP",
                # 推流优先用这一套：延迟最低，WHIP 是 OBS 30+ 的原生协议
                "preferred": True,
                "badge": "优先",
                "note": "延迟最低（约 1 秒）；推流请优先用这里的 WHIP（OBS 里不要开 B 帧）",
                "columns": webrtc_cols,
            }
        )
    if rtmp or rtsp or hls:
        sets.append(
            {
                "id": "tcp",
                "label": "TCP 套 · 抗抖动",
                "preferred": False,
                "badge": "备选",
                "note": "WHIP 推不上去时再用：走 TCP 不易丢帧（延迟 2~10 秒）；RTMP / RTSP 推流与播放同址",
                "columns": [
                    col
                    for col in tcp_cols
                    if {"rtmpPush": rtmp, "rtspPush": rtsp, "hlsPage": hls, "hls": hls}.get(col["key"])
                ],
            }
        )
    return sets


def play_endpoints(stream: StreamConfig, key: str, *, compact: bool = False) -> dict[str, str]:
    """只取**源站播放地址**（媒体服务器直连），不含任何推流地址。

    ``compact=True`` 时只保留前端实际用到的地址（WHEP / HLS 播放页 / HLS 播放列表 /
    内嵌观看页），避免状态体过大——每场比赛都会带一份。
    """
    full = key_endpoints(stream, key)
    names = (
        ("key", "page", "whep", "hlsPage", "hls") if compact else PLAY_ENDPOINT_KEYS
    )
    return {name: full[name] for name in names if full.get(name)}


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
            "play": play_endpoints(cfg.stream, player_stream_key(players[pid]), compact=True),
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


def rulebook(cfg: Config) -> dict[str, Any]:
    """把当前赛制与参数翻译成用户端可读的规则条目。"""
    rules = cfg.rules
    league = rules.format == "league"
    teams = cfg.teams
    rounds = cfg.rounds
    players = joined_players(cfg)
    per_match = max(2, min(MAX_SIDES, rules.teams_per_match or 2))
    shape = "组 vs 组" if per_match == 2 else f"{per_match} 队同场"
    loser = bool(rules.loser_bracket)
    sections: list[dict[str, Any]] = []

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
                    f"排名依据：总得分 ÷ 出场次数（均分）降序；出场不足 {rules.min_rank_played} 局不参与名次。",
                    (
                        f"单局目标分 {rules.target_score} 分，先到者胜。"
                        if rules.target_score
                        else "单局不设目标分，按录入比分判定胜负。"
                    ),
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
                f"依次递减，最低 1 分；同分再比净胜分、总得分。"
                if per_match > 2
                else "排名依据：名次分 —— 胜 2 分、负 1 分；同分再比净胜分、总得分。"
            ),
            (
                "小组赛允许平局。"
                if rules.allow_draw
                else "小组赛必须分出胜负（不设平局）。"
            ),
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
    others = [
        "比赛结果按录入的比分自动判定胜负与名次，录入各局小分还能自动汇总局分与总得分。"
        if not league
        else "比赛结果按录入的比分自动结算积分；每局独立结算，不影响其它局。"
    ]
    if cfg.stream.enabled:
        others.append("直播：每场比赛可单独开启推流，并标注直播选手提示。")
    if cfg.rules.target_score and not league:
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
            "groupMatches": sum(1 for r in rounds if r.stage == "group"),
            "knockoutMatches": sum(1 for r in rounds if r.stage != "group"),
            "totalRounds": rules.total_rounds,
            "minRankPlayed": rules.min_rank_played,
        },
        "note": cfg.event.rules_text,
    }


# --------------------------------------------------------------------------- #
# 汇总状态
# --------------------------------------------------------------------------- #
def build_state(cfg: Config, *, historical: bool = False) -> dict[str, Any]:
    """组装下发给前端的完整公开状态（不含管理 KEY）。

    按 ``rules.format`` 走两套人马：积分制下发 standings，锦标赛制下发 bracket/groups。
    ``historical=True`` 表示这是往届回看，此时比赛一律按「没有直播」渲染。
    """
    progress = T.stage_progress(cfg.rounds)
    state: dict[str, Any] = {
        "revision": cfg.revision,
        "updatedAt": cfg.updated_at,
        "event": cfg.event.dump(),
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
    tables = T.group_tables(teams, rounds)
    ranking = T.overall_ranking(tables)
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

    if joined and len(joined) < need:
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
    return issues


# --------------------------------------------------------------------------- #
# 直播地址派生（按流名生成推流 / 播放地址，支持多组并行各自推流）
# --------------------------------------------------------------------------- #
def key_endpoints(stream: StreamConfig, key: str) -> dict[str, str]:
    """按流名派生**两套协议**的源站地址（媒体服务器直连，不做反代）。

    两套都能推流与播放：

    * WebRTC（UDP）：``whipPush`` 推流 / ``whep`` 播放，延迟最低；
    * TCP：``rtmpPush`` / ``rtspPush`` 推流，``hls`` / ``rtspPlay`` / ``rtmpPlay`` 播放。

    ⚠️ RTMP 与 RTSP 的推流与播放**是同一个地址**（方向由客户端决定），
    所以 ``rtmpPush``/``rtmpPlay``（及 rtsp 对应项）值相同；
    对外只下发 WebRTC / HLS 的播放地址，避免暴露可推流路径。
    """
    key = (key or "").strip().strip("/")
    if not key:
        return {}
    base = (stream.base_url or "").rstrip("/")
    rtmp = (stream.rtmp_base or "").rstrip("/")
    rtsp = (stream.rtsp_base or "").rstrip("/")
    hls = (stream.hls_base or "").rstrip("/")
    return {
        "key": key,
        # —— WebRTC 套（UDP）——
        "whipPush": f"{base}/{key}/whip" if base else "",
        "whep": f"{base}/{key}/whep" if base else "",
        "page": f"{base}/{key}/" if base else "",
        # —— TCP 套：RTMP（推播同址）——
        "rtmpPush": f"{rtmp}/{key}" if rtmp else "",
        "rtmpPlay": f"{rtmp}/{key}" if rtmp else "",
        # —— TCP 套：RTSP（推播同址）——
        "rtspPush": f"{rtsp}/{key}" if rtsp else "",
        "rtspPlay": f"{rtsp}/{key}" if rtsp else "",
        # —— TCP 套：HLS（只能播放，浏览器可直接播）——
        # ``hlsPage``  = 媒体服务器的 HLS 播放页，地址到 ``/<流名>/`` 为止，贴浏览器就能看
        # ``hls``      = 播放列表（``index.m3u8``），给 hls.js / 原生 Safari 播放用
        "hlsPage": f"{hls}/{key}/" if hls else "",
        "hls": f"{hls}/{key}/index.m3u8" if hls else "",
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
        # 播放地址（WHEP / HLS / 内嵌页）——源站直连，不含推流凭据
        "play": play_endpoints(cfg.stream, key, compact=True) if key else {},
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
