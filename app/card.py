"""比赛信息卡片：**服务器实时渲染**的图片推送。

为什么要图片
------------

比赛信息推到群里时，除了名字 / 简介 / 时间 / 赛制，还要带上**比赛规则**；
而规则是随赛制自动生成的（见 :func:`app.logic.rulebook`），条目一多，纯文本在群里
就是一屏接一屏地刷（还得被切成好几段发）。渲染成一张图：信息密度高、一眼看得完、
方便群友转发，也不再受「单条消息长度」的限制。

三条硬约束（都直接关系到「别把服务搞卡」）
------------------------------------------

1. **内容定址 + 落盘缓存**：卡片内容的 sha256 就是文件名（``data/cards/<hash>.png``）。
   同一份内容**只渲染一次**——只要没人改赛制 / 赛程，反复推送都不会再花一次 CPU；
2. **渲染在线程里、并发受限**：:func:`asyncio.to_thread` + 信号量 + 超时，事件循环一秒都
   不等；同一份内容的并发请求还会合并成一次渲染（单飞锁），不会出现「十个人同时点预览，
   十次全量渲染」；
3. **出图在 Go 侧车**（``go/cmd/nterender``，见 :mod:`app.render`）：这里只组内容
   （:func:`payload_for` / :func:`payload_for_result`），画成什么样全由
   ``go/internal/render`` 决定——配色与排版只有那一份实现，不会出现「网页一套、
   图一套」。万一这台机器上没有那个二进制，:func:`available` 是 ``False``、
   :func:`card_for_event` 回 ``None``，调用方**自动退回纯文本**（信息一条不少，
   只是排版朴素）——那是**兜底**，不是常态。

安全性
------

卡片里只有**本站本来就会公开展示**的东西（届名 / 赛制 / 规则 / 分组），但地址不是
「换个链接就能下载点什么」的口子：

* 文件名是内容哈希，猜不出来；
* :func:`resolve` **只认本站签发过的哈希**（:data:`_ISSUED`）——没经推送 / 预览
  流出去的卡片，就算有人凑出哈希也读不到。

签到表只在内存里（重启即清空）。代价是「重启后，历史上发出去的那张图链接会 404」；
好处是**永远不会**因为一份陈年文件而泄露一届已经被隐藏的赛事——群里那张图早已送达，
而链接重新签发一次（再推一次）成本几乎为零。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import logic, markdown, metrics
from . import render as renderer
from .logging_conf import get_logger
from .models import MAX_SIDES, Config
from .store import DATA_ROOT

log = get_logger("card")

#: 卡片落盘目录（与备份、公告图片分开存放）
CARD_ROOT = DATA_ROOT / "cards"

#: 一张卡片最多几张（内容定址的目录会攒文件，按 mtime 清最旧的）
MAX_FILES = 240
#: 渲染超时：超了就当作「这次没图」，退回文本推送（总比把请求挂在这儿强）
RENDER_TIMEOUT = 20.0
#: 同时渲染数（出图是纯 CPU：子进程 + PNG 编码，放开跑会把核占满）
MAX_CONCURRENT = 2
#: 卡片文件名的白名单：只认我们自己写出去的形态
_NAME_RE = re.compile(r"^[0-9a-f]{16,64}\.png$")


#: 已签发的卡片（哈希 → 签发信息）。只读内存，见模块说明。
_ISSUED: dict[str, dict[str, Any]] = {}
_render_locks: dict[str, asyncio.Lock] = {}
_semaphore: asyncio.Semaphore | None = None


# --------------------------------------------------------------------------- #
# 出图（Go 侧车，见 app/render.py）
# --------------------------------------------------------------------------- #
def available() -> bool:
    """这台机器上能不能出图（``nterender`` 二进制在不在、版本对不对）。

    没有二进制就安静退回纯文本：文案里本来就带规则摘要与逐场比分，信息不缺，
    只是没有图——比整条推送报错强。
    """
    return renderer.available()


# --------------------------------------------------------------------------- #
# 内容：**纯函数**（不碰渲染器、不碰网络），所以卡片内容本身能直接单测
# --------------------------------------------------------------------------- #
def _clean_text(text: str, limit: int) -> str:
    """压平空白、去掉 Markdown 记号、按字符截断。

    这里**必须**过一遍 :func:`app.markdown.to_text`：规则条目里带着 ``**强调**``
    （那是给网页用的），直接画进图里就是一堆星号——渲染成图时最容易漏的一步。
    图片里没有滚动条，太长也只能截。
    """
    return markdown.to_text(str(text or ""), limit)


def _key_pairs(cfg: Config, facts: dict[str, Any]) -> list[tuple[str, str]]:
    """卡片上半部分的「一栏一项」：赛制 / 人数 / 分组 / 打法 / 计分口径…"""
    evt = cfg.event
    rules = cfg.rules
    sc = rules.scoring
    players = logic.joined_players(cfg)
    status = {"draft": "筹备中", "active": "进行中", "closed": "已结束"}.get(
        str(evt.status), str(evt.status)
    )
    pairs: list[tuple[str, str]] = [("状态", status)]
    if evt.start_time:
        pairs.append(("开赛", str(evt.start_time).replace("T", " ")[:16]))
    if evt.end_time:
        pairs.append(("结束", str(evt.end_time).replace("T", " ")[:16]))
    pairs.append(("赛制", str(facts.get("formatLabel") or "")))
    if facts.get("format") == "league":
        pairs.append(("每局", f"{rules.team_size} 人对 {rules.team_size} 人"))
        pairs.append(("局数", f"共 {rules.total_rounds} 局"))
    else:
        pairs.append(("每队", f"{rules.team_size} 人"))
    pairs.append(("参赛", f"{len(players)} 人" + (f" · {facts.get('teams')} 队" if facts.get("teams") else "")))
    if facts.get("groups"):
        pairs.append(("分组", f"{facts['groups']} 组（{facts.get('groupSizes') or '—'}）"))
    pairs.append(("每场", str(facts.get("shape") or "")))
    if facts.get("format") != "league":
        if facts.get("groupRounds"):
            per = str(facts.get("perTeamMatches") or "")
            pairs.append(("小组赛", f"{facts['groupRounds']} 轮" + (f" · {per}" if per else "")))
        pairs.append(
            (
                "淘汰赛",
                f"{facts['size']} 强 · {'双败' if facts.get('loserBracket') else '单败'}"
                if facts.get("size")
                else ("双败" if facts.get("loserBracket") else "单败"),
            )
        )
    pairs.append(("计分", f"{sc.type_label}（{sc.label_text}）· {sc.better_label}"))
    if facts.get("targetScore") and facts.get("format") != "league" and not sc.low_wins:
        pairs.append(("单轮目标", f"{facts['targetScore']} 分"))
    pairs.append(("名次", "计算名次与晋级" if cfg.event.ranked else "娱乐模式（不排名）"))
    return pairs


def _group_lines(cfg: Config) -> list[tuple[str, str]]:
    """分组名单：让群友一眼看到「我跟谁一组」。队伍太多时不画（免得卡片变成花名册）。"""
    teams = cfg.teams
    if not teams or len(teams) > 24:
        return []
    buckets: dict[str, list[str]] = {}
    for team in teams:
        buckets.setdefault(team.group or "A", []).append(team.label)
    return [(f"{key} 组", "、".join(names)) for key, names in sorted(buckets.items())]


def _caption(cfg: Config, facts: dict[str, Any]) -> str:
    """配在图前面的一行话：图挂了 / 不方便看图的人也能知道这是什么。"""
    evt = cfg.event
    name = evt.name or evt.title or "比赛"
    bits = [f"【{name}】"]
    if evt.start_time:
        bits.append(str(evt.start_time).replace("T", " ")[:16])
    if facts.get("teams"):
        bits.append(f"{facts['teams']} 队 / {facts.get('players')} 人")
    if facts.get("shape"):
        bits.append(str(facts["shape"]))
    bits.append("完整赛制与规则见图")
    return " · ".join(bits)


def payload_for(cfg: Config, event_id: str = "", state: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一届的「信息 + 规则」整理成卡片内容（纯函数，方便单测与指纹计算）。

    规则部分**直接来自** :func:`app.logic.rulebook`——它就是「比赛规则」面板上那一份，
    所以卡片永远不会跟实际赛制脱节（改了赛制，卡片的指纹就变了，下次推送自动重画）。
    """
    rb = logic.rulebook(cfg)
    facts = rb.get("facts") or {}
    evt = cfg.event
    sections = [
        {
            "title": str(section.get("title") or ""),
            "items": [_clean_text(item, 160) for item in (section.get("items") or [])],
        }
        for section in (rb.get("sections") or [])
    ]
    # 「比赛简介」块：内容取**简介**（不是规则文本）。
    # 简介支持换行（地图 / 规则 / 注意事项分行写），渲染时按原有换行折行，见 _wrap_multiline。
    note = str(evt.brief or "").strip()
    return {
        "title": str(evt.name or evt.title or "比赛"),
        "id": str(event_id or ""),
        "status": {"draft": "筹备中", "active": "进行中", "closed": "已结束"}.get(
            str(evt.status), str(evt.status)
        ),
        "brief": _clean_text(evt.brief, 60),
        "headline": _clean_text(str(rb.get("headline") or ""), 80),
        "caption": _caption(cfg, facts),
        "meta": _key_pairs(cfg, facts),
        "groups": _group_lines(cfg),
        "sections": sections,
        "note": note,
        "noteTitle": "比赛简介",
        "ranks": bool(cfg.event.ranked),
    }


# --------------------------------------------------------------------------- #
# 比赛结果卡片：小组赛逐场结果 + 淘汰赛树状图
#
# 与「比赛信息」卡片的区别：那张讲**还没发生的事**（赛制与规则），这张讲**已经打完的
# 事**——每一场小组赛的比分、每一场淘汰赛的比分与晋级走向。淘汰赛用树状图（就是站点
# 「对阵总览」那种画法），一眼能看出谁从哪来、往哪去。
#
# 树状图的坐标在 :func:`payload_for_result` 里就算成整数（纯函数，能单测、进指纹），
# 渲染只负责把方框与折线画上去。父子关系以对局自带的 ``srcA`` / ``srcB`` 为准，
# 所以单败 / 双败、4 强到 32 强都是同一套代码。
# --------------------------------------------------------------------------- #
#: 树状图布局常量：坐标全在这里算好，渲染器只按坐标画。
#: **这些值会随 payload 一起交给 Go**（``tree.box``），所以「方框多大」只有这一处定义。
_TREE_BOX = 250
_TREE_BOX_H = 96
_TREE_GAP_X = 40
_TREE_HEAD = 64
_TREE_BAND_GAP = 56
_TREE_PAD = 14
_TREE_CHAMP_W = 190

#: 每条带的标题（``gf`` 不铺底块，它夹在两条带之间）
_TREE_BAND_TITLE = {"main": "淘汰赛", "wb": "胜者组", "lb": "败者组"}
_RESULT_STATUS = {"pending": "待赛", "live": "进行中", "done": "已结束"}

#: 对阵框内每一方（一支队伍）占用的高度；框高 = 顶栏 + 方数 × 行高
_TREE_SIDE_ROW = 33


def _tree_box_h(side_count: int) -> int:
    """对阵框高度：按同场队伍数伸缩（多队同场时更高）。"""
    return 30 + max(2, min(MAX_SIDES, int(side_count or 2))) * _TREE_SIDE_ROW


def _side_cells(
    rnd: dict[str, Any], sc: Any, colors: dict[str, str]
) -> list[dict[str, Any]]:
    """一场比赛各方的短信息（树状图方框用）：队名 / 成绩 / 是否胜方 / 颜色。"""
    sides = rnd.get("sides") or []
    counted = bool(rnd.get("sets"))
    scored = rnd.get("status") == "done" or any(sc.has_entered(s.get("score")) for s in sides)
    cells: list[dict[str, Any]] = []
    for side in sides:
        team_id = str(side.get("teamId") or "")
        cells.append(
            {
                "name": logic.side_label(side),
                "score": sc.format_score(side.get("score", 0), counted=counted) if scored else "",
                "win": bool(side.get("winner")),
                "color": str(side.get("color") or colors.get(team_id) or ""),
            }
        )
    return cells


def _group_sections(
    state: dict[str, Any], sc: Any, per_match: int, teams_total: int = 0
) -> list[dict[str, Any]]:
    """小组赛：每组一张积分表 + 该组**逐场**比分（用户要的「每一场小组赛的结果」）。"""
    rounds = [r for r in (state.get("rounds") or []) if r.get("stage") == "group"]
    if not rounds:
        return []
    buckets: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for rnd in rounds:
        key = str(rnd.get("label") or "").split(" · ")[0].strip() or "A 组"
        buckets.setdefault(key, {}).setdefault(int(rnd.get("bracketRound") or 1), []).append(rnd)
    tables = {
        str(group.get("key") or ""): group.get("rows") or []
        for group in (state.get("groups") or [])
    }
    qualified = {
        str(row.get("team", {}).get("id") or ""): int(row.get("seed") or 0)
        for row in (state.get("ranking") or [])
        if row.get("advanced")
    }
    shape = "两两对阵" if per_match <= 2 else f"{per_match} 队同场"
    advance = int((state.get("format") or {}).get("size") or 0)
    advance_text = "全部晋级" if teams_total and advance >= teams_total else (
        f"前 {advance} 名晋级" if advance else ""
    )
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = []
        for row in tables.get(key.replace(" 组", ""), []):
            team_id = str(row.get("teamId") or "")
            rows.append(
                {
                    "rank": int(row.get("rank") or 0),
                    "name": str(row.get("name") or team_id),
                    "played": int(row.get("played") or 0),
                    "win": int(row.get("win") or 0),
                    "lose": int(row.get("lose") or 0),
                    "placement": int(row.get("placement") or 0),
                    "seed": qualified.get(team_id, 0),
                }
            )
        matches: list[dict[str, Any]] = []
        for round_no in sorted(buckets[key]):
            items = sorted(buckets[key][round_no], key=lambda r: int(r.get("slot") or 0))
            for rnd in items:
                # 一轮里有多场：带上场次，否则同一轮的两场看起来一模一样
                label = str(rnd.get("label") or "")
                matches.append(
                    {
                        "round": label.split(" · ", 1)[1] if " · " in label else f"第 {round_no} 轮",
                        "line": logic.round_sides_text(rnd, sc),
                        "done": rnd.get("status") == "done",
                    }
                )
        out.append(
            {
                "key": key,
                "note": " · ".join(
                    part
                    for part in (f"{len(rows)} 支队", f"每场 {shape}", advance_text if qualified else "")
                    if part
                ),
                "rows": rows,
                "matches": matches,
            }
        )
    return out


def _tree_section(cfg: Config, state: dict[str, Any], sc: Any) -> dict[str, Any] | None:
    """淘汰赛树状图：列（轮次）+ 方框坐标 + 连线 + 冠军框（纯计算，不含任何绘制）。"""
    rounds = [r for r in (state.get("rounds") or []) if r.get("stage") in ("wb", "lb", "gf")]
    if not rounds:
        return None
    single = cfg.rules.loser_bracket is False
    band_of = {"wb": "main" if single else "wb", "lb": "lb", "gf": "main" if single else "gf"}
    colors = {team.id: team.color for team in cfg.teams if team.color}

    def key_of(rnd: dict[str, Any]) -> str:
        """一场对局的键：席位引用按 ``code``；没有 code 的老数据用序号兜底。"""
        return str(rnd.get("code") or f"#{rnd.get('index')}")

    cols: list[dict[str, Any]] = []
    for stage in (("wb", "gf") if single else ("wb", "lb", "gf")):
        buckets: dict[int, list[dict[str, Any]]] = {}
        for rnd in rounds:
            if rnd.get("stage") != stage:
                continue
            buckets.setdefault(int(rnd.get("bracketRound") or 1), []).append(rnd)
        for round_no in sorted(buckets):
            items = sorted(buckets[round_no], key=lambda r: int(r.get("slot") or 0))
            title = str(items[0].get("label") or "").split(" · ")[0].strip() or f"第 {round_no} 轮"
            cols.append({"band": band_of[stage], "title": title, "matches": items, "x": 0, "labelY": 0})

    nodes: dict[str, dict[str, Any]] = {}
    for col in cols:
        for rnd in col["matches"]:
            nodes[key_of(rnd)] = {
                "band": col["band"],
                "kids": [],
                "x": 0,
                "y": None,
                "h": _tree_box_h(len(rnd.get("sides") or [])),
                "m": rnd,
            }
    for node in nodes.values():
        for ref in (
            node["m"].get("srcA"),
            node["m"].get("srcB"),
            node["m"].get("srcC"),
            node["m"].get("srcD"),
        ):
            kid = nodes.get(str(ref or "").split(":")[0])
            if kid is None:
                continue
            # 胜者组落败者掉进败者组这类跨带引用**不画线**（否则满屏长线）；总决赛例外
            if kid["band"] != node["band"] and node["band"] != "gf":
                continue
            node["kids"].append(kid)

    pitch = _TREE_BOX + _TREE_GAP_X
    gap = max([_TREE_BOX_H, *[n["h"] for n in nodes.values()]]) + 10
    bands = ["main"] if single else ["wb", "lb", "gf"]
    band_boxes: list[dict[str, Any]] = []
    band_tops: dict[str, int] = {}
    cursor = _TREE_PAD + _TREE_HEAD
    for band in bands:
        band_cols = [col for col in cols if col["band"] == band]
        if not band_cols:
            continue
        band_tops[band] = cursor
        leaf_top = cursor
        # 总决赛列落在最后一条带的右侧（两条带都汇进它），不是从最左边开始
        gf_shift = 0
        if band == "gf":
            others = [
                len([col for col in cols if col["band"] == other])
                for other in bands
                if other != "gf" and any(col["band"] == other for col in cols)
            ]
            gf_shift = max(others or [0]) * pitch
        for ci, col in enumerate(band_cols):
            col["x"] = _TREE_PAD + (gf_shift if band == "gf" else ci * pitch)
            for i, rnd in enumerate(col["matches"]):
                node = nodes[key_of(rnd)]
                node["x"] = col["x"]
                kid_center = [k["y"] + k["h"] / 2 for k in node["kids"] if k["y"] is not None]
                node["y"] = (
                    round(sum(kid_center) / len(kid_center) - node["h"] / 2)
                    if kid_center
                    else leaf_top + i * gap
                )
        bottoms = [nodes[key_of(r)]["y"] + nodes[key_of(r)]["h"] for r in band_cols[0]["matches"]]
        if band != "gf":
            band_boxes.append(
                {
                    "key": band,
                    "title": _TREE_BAND_TITLE[band],
                    "x": _TREE_PAD - 10,
                    "y": cursor - 52,
                    "titleY": cursor - 60,
                    "w": band_cols[-1]["x"] + _TREE_BOX + 10 - (_TREE_PAD - 10),
                    "h": max(bottoms) - (cursor - 52) + 12,
                }
            )
        band_h = max(
            [_TREE_BOX_H, *[nodes[key_of(r)]["h"] for r in band_cols[0]["matches"]]]
        )
        cursor += (len(band_cols[0]["matches"]) - 1) * gap + band_h + _TREE_BAND_GAP

    for col in cols:
        col["labelY"] = (
            min(nodes[key_of(r)]["y"] for r in col["matches"]) - 24
            if col["band"] == "gf"
            else band_tops.get(col["band"], cursor) - 24
        )

    last_cols = cols if single else [col for col in cols if col["band"] == "gf"]
    last = last_cols[-1] if last_cols else None
    champ_x = (last["x"] if last else _TREE_PAD) + _TREE_BOX + _TREE_GAP_X + 16
    champ_y = (
        nodes[key_of(last["matches"][0])]["y"]
        if last and last["matches"]
        else _TREE_PAD + _TREE_HEAD
    )
    champion = state.get("champion") or None
    champ_name = ""
    if champion:
        champ_name = str(
            champion.get("name") or champion.get("id") or ""
        )
    all_nodes = [nodes[key] for key in nodes]
    height = max([node["y"] + node["h"] for node in all_nodes] or [0]) + _TREE_PAD
    return {
        "single": single,
        # 画方框要用到的尺寸：跟着坐标一起交给渲染器，免得两边各写一套常量
        "box": {"w": _TREE_BOX, "h": _TREE_BOX_H, "sideRow": _TREE_SIDE_ROW, "champW": _TREE_CHAMP_W},
        "bands": band_boxes,
        "columns": [{"x": col["x"], "y": col["labelY"], "title": col["title"]} for col in cols],
        "nodes": [
            {
                "x": node["x"],
                "y": node["y"],
                "code": str(node["m"].get("code") or ""),
                "label": str(node["m"].get("label") or ""),
                "status": str(node["m"].get("status") or ""),
                "statusLabel": _RESULT_STATUS.get(str(node["m"].get("status")), ""),
                "sides": _side_cells(node["m"], sc, colors),
            }
            for node in all_nodes
        ],
        "links": [
            {
                "x1": kid["x"] + _TREE_BOX,
                "y1": kid["y"] + kid["h"] // 2,
                "x2": node["x"],
                "y2": node["y"] + node["h"] // 2,
                "done": kid["m"].get("status") == "done",
            }
            for node in all_nodes
            for kid in node["kids"]
        ],
        "champ": {"x": champ_x, "y": champ_y, "name": champ_name},
        "width": champ_x + _TREE_CHAMP_W + _TREE_PAD,
        "height": height,
    }


def payload_for_result(
    cfg: Config, event_id: str = "", state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把一届的「结果」整理成卡片内容（纯函数，方便单测与指纹计算）。

    * 锦标赛制：小组赛积分表 + 逐场比分，外加淘汰赛树状图；
    * 积分制：均分榜 + 逐局比分（没有小组赛 / 淘汰赛之分，也就不画树）。
    """
    st = state if state is not None else logic.build_state(cfg)
    sc = metrics.as_scoring(cfg.rules.scoring)
    evt = cfg.event
    name = evt.name or evt.title or "比赛"
    fmt = dict(st.get("format") or {})
    rounds = list(st.get("rounds") or [])
    done = [rnd for rnd in rounds if rnd.get("status") == "done"]
    champion = st.get("champion") or None
    champ_name = ""
    if champion:
        champ_name = str(
            champion.get("name") or champion.get("id") or ""
        )
    season = (
        f"已赛 {len(done)} / {len(rounds)} 场"
        + (f" · 冠军 {champ_name}" if champ_name else "")
    )

    groups: list[dict[str, Any]] = []
    standings: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    tree: dict[str, Any] | None = None
    if fmt.get("kind") == "league":
        rows = [row for row in ((st.get("standings") or {}).get("players") or []) if row.get("played")]
        for row in rows:
            standings.append(
                {
                    "rank": int(row.get("rank") or 0),
                    "name": _player_name(cfg, str(row.get("playerId") or "")),
                    "played": int(row.get("played") or 0),
                    "average": str(row.get("average") or 0),
                    "points": int(row.get("points") or 0),
                }
            )
        for rnd in rounds:
            matches.append(
                {
                    "round": str(rnd.get("label") or rnd.get("code") or ""),
                    "line": logic.round_sides_text(rnd, sc),
                    "done": rnd.get("status") == "done",
                }
            )
    else:
        groups = _group_sections(st, sc, int(cfg.rules.teams_per_match or 2), len(cfg.teams))
        tree = _tree_section(cfg, st, sc)

    caption = f"【{name}】比赛结果"
    if champ_name:
        caption += f" · 冠军 {champ_name}"
    caption += " · 完整对阵见图"
    return {
        "kind": "result",
        "title": str(name),
        "id": str(event_id or ""),
        "headline": season,
        "statusLabel": {"draft": "筹备中", "active": "进行中", "closed": "已结束"}.get(
            str(evt.status), str(evt.status)
        ),
        "champion": champ_name,
        "scoring": {"typeLabel": sc.type_label, "label": sc.label_text, "better": sc.better_label},
        "groups": groups,
        "standings": standings,
        "matches": matches,
        "tree": tree,
        "caption": caption,
    }


def _player_name(cfg: Config, player_id: str) -> str:
    """选手 ID → 展示名（积分榜的行里只有 ``playerId``，没有内嵌选手对象）。"""
    player = next((p for p in cfg.players if p.id == player_id), None)
    return (player.display_name if player is not None else "") or player_id or "—"


def digest(payload: dict[str, Any]) -> str:
    """内容指纹：同样的内容永远得到同一个文件名（这就是「不重复生成」的凭据）。

    ``sort_keys`` + ``ensure_ascii=False``：两边算出来的必须一模一样，
    否则「同样的内容」会被当成两份，缓存也就白做了。

    **画法与字体也算进指纹**：图是拿当时的渲染器与字体画出来的——换了二进制、
    换了字体（或者这台机器从「没装中文字体」变成「装了」），同一份内容画出来就是
    另一张图。不算进去的话会一直发着旧那张（真踩过：中文是一排方块的那版）。
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((blob + _render_signature()).encode("utf-8")).hexdigest()


def _render_signature() -> str:
    """「画法」的指纹：侧车的协议版本 / 构建标记 / 实际字体（见 :func:`app.render.signature`）。

    缓存是内容定址的，指纹里少一项，旧图就会一直躺在 ``data/cards`` 里被反复发出去
    （真踩过：换了字体重启，群里还是旧那张）。
    """
    return "|" + renderer.signature()


# --------------------------------------------------------------------------- #
# 渲染：交给 Go 侧车（排版、配色、取景全在 go/internal/render 里）
# --------------------------------------------------------------------------- #
def render(payload: dict[str, Any], *, site: str = "") -> bytes | None:
    """把 :func:`payload_for` 的内容画成 PNG（**阻塞**，调用方负责放进线程）。

    画法（警示胶带 + 渐变标题 + 切角面板 + 双色网点）由 ``go/internal/render`` 决定，
    这里只负责把内容递过去。画不出来（没二进制 / 超时 / 出错）返回 ``None``。
    """
    result = renderer.render("event", payload, site=site)
    return result[0] if result else None


def render_result(payload: dict[str, Any], *, site: str = "") -> bytes | None:
    """把 :func:`payload_for_result` 的内容画成 PNG（同上）。"""
    result = renderer.render("result", payload, site=site)
    return result[0] if result else None


# --------------------------------------------------------------------------- #
# 缓存 / 签发 / 读取
# --------------------------------------------------------------------------- #
def _prune() -> None:
    """卡片攒太多时清最旧的（内容定址的目录只增不减，得有人扫一下）。"""
    try:
        files = sorted(CARD_ROOT.glob("*.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[: max(0, len(files) - MAX_FILES)]:
        try:
            path.unlink()
            _ISSUED.pop(path.stem, None)
        except OSError:  # 正被读取 / 没权限：下次再说
            continue


def resolve(name: str) -> Path | None:
    """按文件名取卡片；**只认签发过的哈希**（见模块说明）。"""
    clean = str(name or "").strip()
    if not _NAME_RE.fullmatch(clean):
        return None
    if clean[:-4] not in _ISSUED:
        return None
    path = CARD_ROOT / clean
    return path if path.exists() else None


def card_bytes(name: str) -> bytes | None:
    """读回一张已签发卡片的 PNG 字节；读不到（名字非法 / 文件没了）回 ``None``。

    给推送用：新版 AstrBot 要「先把图传上去换 attachment_id」，而
    :func:`card_for_event` 缓存命中时不带字节（省一次读盘），那就从磁盘补读。

    ``name`` 既可以是文件名（``<哈希>.png``）也可以只给哈希——
    :func:`card_for_event` 返回的就是后者，别让调用方自己拼后缀。
    """
    clean = str(name or "").strip()
    if clean and not clean.endswith(".png"):
        clean += ".png"
    path = resolve(clean)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def stats() -> dict[str, Any]:
    """卡片缓存占用（管理端展示用）。"""
    files = list(CARD_ROOT.glob("*.png")) if CARD_ROOT.exists() else []
    return {
        "cards": len(files),
        "bytes": sum(path.stat().st_size for path in files if path.exists()),
        "issued": len(_ISSUED),
        "available": available(),
    }


async def card_for_event(
    cfg: Config,
    event_id: str = "",
    state: dict[str, Any] | None = None,
    *,
    site: str = "",
    kind: str = "event",
) -> dict[str, Any] | None:
    """拿到这一届的卡片（**没有就渲染**）；不可用 / 失败一律回 ``None``。

    ``kind``：``event`` = 比赛信息 + 规则（见 :func:`payload_for`）；
    ``result`` = 比赛结果（小组赛逐场 + 淘汰赛树状图，见 :func:`payload_for_result`）。
    两者共用同一套缓存 / 签发 / 超时兜底——指纹跟着内容走，不会互相覆盖。

    回 ``{hash, caption, url, bytes}``：``url`` 是给人看的相对地址（调用方补上站点域名），
    ``bytes`` 只在刚刚渲染出来时非空（缓存命中时为 ``None``，省一次读盘）。
    """
    if not available():
        return None
    is_result = kind == "result"
    payload = (
        payload_for_result(cfg, event_id, state) if is_result else payload_for(cfg, event_id, state)
    )
    key = digest(payload)
    path = CARD_ROOT / f"{key}.png"
    fresh = b""
    if not path.exists():
        lock = _render_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not path.exists():  # 单飞：等锁的后来者会看到文件已经在了
                global _semaphore
                if _semaphore is None:
                    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
                async with _semaphore:
                    try:
                        data = await asyncio.wait_for(
                            asyncio.to_thread(
                                render_result if is_result else render, payload, site=site
                            ),
                            timeout=RENDER_TIMEOUT,
                        )
                    except TimeoutError:
                        log.warning("卡片渲染超时（%s 秒），本次退回纯文本", RENDER_TIMEOUT)
                        return None
                    except Exception:
                        # render 内部已经兜了一层；这里再兜一层是因为**推送绝不能因图挂掉**：
                        # 线程里漏出来的异常会一路冒到请求上，变成群里收不到任何消息。
                        log.warning("卡片渲染异常（本次退回纯文本）", exc_info=True)
                        return None
                if not data:
                    return None
                try:
                    CARD_ROOT.mkdir(parents=True, exist_ok=True)
                    # 先写临时文件再改名：并发 / 中断都不会留下半张图
                    tmp = path.with_suffix(".png.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    fresh = data
                except OSError as exc:
                    log.warning("卡片落盘失败（本次仍可推送）| %s", exc)
        _render_locks.pop(key, None)
        _prune()
    if not path.exists():
        return None
    _ISSUED[key] = {"at": time.time(), "eventId": str(event_id or "")}
    return {
        "hash": key,
        "caption": str(payload.get("caption") or ""),
        "url": f"/api/cards/{key}.png",
        "title": str(payload.get("title") or ""),
        "bytes": fresh,
    }
