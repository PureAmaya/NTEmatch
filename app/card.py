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
3. **Pillow 是正式依赖**（``pyproject.toml`` 的 ``dependencies`` 里）：图片推送就是靠它
   渲染，默认装好就有图。万一某个环境没装上（自建镜像漏了依赖、装了坏版本），
   :func:`available` 是 ``False``、:func:`card_for_event` 回 ``None``，调用方
   **自动退回纯文本**（信息一条不少，只是排版朴素）——那是**兜底**，不是常态。

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
import importlib
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any

from . import fonts, logic, markdown, metrics
from .logging_conf import get_logger
from .models import Config
from .store import DATA_ROOT

log = get_logger("card")

#: 卡片落盘目录（与备份、公告图片分开存放）
CARD_ROOT = DATA_ROOT / "cards"

#: 画布宽度（与帮助图同一档，手机上缩放后仍清晰）
WIDTH = 1080
PAD = 64
#: 高度上限：规则再多也不至于出一张几万像素的图（超出部分写到「见站点」）
MAX_HEIGHT = 6000
#: 一张卡片最多几张（内容定址的目录会攒文件，按 mtime 清最旧的）
MAX_FILES = 240
#: 渲染超时：超了就当作「这次没图」，退回文本推送（总比把请求挂在这儿强）
RENDER_TIMEOUT = 20.0
#: 同时渲染数（Pillow 是纯 CPU，放开跑会把核占满，界面就卡了）
MAX_CONCURRENT = 2
#: 卡片文件名的白名单：只认我们自己写出去的形态
_NAME_RE = re.compile(r"^[0-9a-f]{16,64}\.png$")

# 站点配色（与 static/css/nte.css 的变量一致，图与网页是一套观感）
INK = (4, 7, 12)
PANEL = (16, 23, 37)
ACCENT = (34, 224, 232)
ACCENT_2 = (255, 47, 142)
DIM = (147, 167, 193)
TXT = (230, 240, 255)
LINE = (34, 224, 232, 60)
WARN = (255, 196, 92)
#: 结果卡片的对阵框（比面板略亮一点，衬出「一个一个方框」）
BOX_FILL = (23, 32, 50, 255)
BOX_EDGE = (48, 63, 92, 255)

# 字体不在本模块里写死：中文必须是 CJK 字体，而「它装在哪」各发行版都不一样，
# 统一由 app/fonts.py 分四层去找（环境变量 → 常见路径 → 扫字体目录 → 内置兜底）。

#: 已签发的卡片（哈希 → 签发信息）。只读内存，见模块说明。
_ISSUED: dict[str, dict[str, Any]] = {}
_render_locks: dict[str, asyncio.Lock] = {}
_semaphore: asyncio.Semaphore | None = None


# --------------------------------------------------------------------------- #
# Pillow（图片推送的渲染器）
# --------------------------------------------------------------------------- #
def available() -> bool:
    """这台机器上能不能画图（Pillow 装没装）。

    Pillow 是正式依赖，正常情况下这里一直是 ``True``；留着这道判断是为了
    **兜底**：环境没装全 / 装坏了时，与其整个推送报错，不如安静退回纯文本。

    只探「装没装」（``find_spec``），**不导入**：这条判断每次推送都会问一次，
    没必要为它真把 Pillow 拉进来（真装坏了也由 :func:`render` 兜住，同样退回文本）。
    """
    try:
        return importlib.util.find_spec("PIL") is not None
    except (ImportError, ValueError):  # 包元数据坏掉之类：当作没有
        return False


def _pil():
    """延迟导入 Pillow（导入失败由调用方兜住，退回纯文本）。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    return Image, ImageDraw, ImageFilter, ImageFont


# --------------------------------------------------------------------------- #
# 内容：**纯函数**（不碰 Pillow、不碰网络），所以卡片内容本身能直接单测
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
#: 结果卡片最多画多宽（树再宽也不至于出一张几万像素的图）
MAX_RESULT_WIDTH = 5200

#: 树状图布局常量：它们**进指纹**（改了布局就是另一张图）
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
                    "name": str(row.get("short") or row.get("name") or team_id),
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
                "m": rnd,
            }
    for node in nodes.values():
        for ref in (node["m"].get("srcA"), node["m"].get("srcB")):
            kid = nodes.get(str(ref or "").split(":")[0])
            if kid is None:
                continue
            # 胜者组落败者掉进败者组这类跨带引用**不画线**（否则满屏长线）；总决赛例外
            if kid["band"] != node["band"] and node["band"] != "gf":
                continue
            node["kids"].append(kid)

    pitch = _TREE_BOX + _TREE_GAP_X
    gap = _TREE_BOX_H + 10
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
                kid_y = [k["y"] for k in node["kids"] if k["y"] is not None]
                node["y"] = round(sum(kid_y) / len(kid_y)) if kid_y else leaf_top + i * gap
        bottoms = [nodes[key_of(r)]["y"] + _TREE_BOX_H for r in band_cols[0]["matches"]]
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
        cursor += (len(band_cols[0]["matches"]) - 1) * gap + _TREE_BOX_H + _TREE_BAND_GAP

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
            champion.get("short") or champion.get("name") or champion.get("id") or ""
        )
    all_nodes = [nodes[key] for key in nodes]
    height = max([node["y"] + _TREE_BOX_H for node in all_nodes] or [0]) + _TREE_PAD
    return {
        "single": single,
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
                "y1": kid["y"] + _TREE_BOX_H // 2,
                "x2": node["x"],
                "y2": node["y"] + _TREE_BOX_H // 2,
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
            champion.get("short") or champion.get("name") or champion.get("id") or ""
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

    **字体也算进指纹**：图是拿当时的字体画出来的——换了字体（或者这台机器从
    「没装中文字体」变成「装了」），同一份内容画出来就是另一张图。不算进去的话
    会一直发着旧那张（真踩过：中文是一排方块的那版）。
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((blob + _font_signature()).encode("utf-8")).hexdigest()


def _font_signature() -> str:
    """画这张图用的字体（找不到就是 ``-``）：并入指纹，换字体就自动重画。

    用 :func:`app.fonts.identity` 而不是裸路径：多语言合集里 JP / SC 是**同一个文件的不同
    face**——只认路径的话，把日文那支换成简体那支以后，缓存里那些「日文字形」的卡片
    还会被一直发出去。
    """
    return "|" + "|".join(fonts.identity(kind) for kind in ("cjk", "latin", "mono"))


# --------------------------------------------------------------------------- #
# 渲染（Pillow）—— 所有函数都在线程里跑，见 card_for_event
# --------------------------------------------------------------------------- #
#: 不该出现在**行首**的标点：折行时它们要跟着前一个字走（「。」独占一行很难看）
_TRAILING_PUNCT = "。，、；：？！）】》」』”’…%,.;:?!)]}"


def _tokens(text: str) -> list[str]:
    """按「中文逐字、西文成串」切词——中文没有空格，只能逐字折行。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_/+.:%()"):
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    # 收尾标点并进前一个词：否则折行时会出现「……跟谁打」+「。」的孤行
    merged: list[str] = []
    for token in out:
        if merged and token and all(ch in _TRAILING_PUNCT for ch in token):
            merged[-1] += token
        else:
            merged.append(token)
    return merged or [""]


def _wrap_multiline(text: str, font, width: int) -> list[str]:
    """折行，且**尊重原文里的换行**。

    简介是**可以换行**的（地图 / 规则 / 注意事项分行写），所以先按 ``\\n`` 分段、
    每段各自折行；空行直接跳过（免得白占一行高度）。以前一律当成一整段，
    用户敲的回车全被吃掉了。
    """
    out: list[str] = []
    for paragraph in str(text or "").replace("\r\n", "\n").split("\n"):
        if not paragraph.strip():
            continue
        out.extend(_wrap(paragraph, font, width))
    return out


def _wrap(text: str, font, width: int) -> list[str]:
    """把一段文字折成若干行；**单个词比整行还宽**时硬切，避免溢出画布。"""
    lines: list[str] = []
    cur = ""
    for token in _tokens(text):
        probe = cur + token
        if font.getlength(probe) <= width or not cur:
            while font.getlength(probe) > width and len(probe) > 1:
                # 单个长词（一长串英文 / 数字）：按宽度硬切
                cut = max(1, len(probe) // 2)
                while cut > 1 and font.getlength(probe[:cut]) > width:
                    cut -= 1
                lines.append(probe[:cut])
                probe = probe[cut:]
            cur = probe
            continue
        lines.append(cur)
        cur = token
    if cur:
        lines.append(cur)
    return lines or [""]


def _brand_bar(img, x: int, y: int, width: int, *, height: int = 4, step: int = 26) -> int:
    """画品牌渐变小条（与站点品牌线同色），返回**下一行的 y**。"""
    Image, ImageDraw, _Filter, _ImageFont = _pil()
    bar = Image.new("RGBA", (max(1, width), height), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for i in range(max(1, width)):
        t = i / max(1, width - 1)
        bd.line(
            [(i, 0), (i, height)],
            fill=(
                round(ACCENT[0] + (ACCENT_2[0] - ACCENT[0]) * t),
                round(ACCENT[1] + (ACCENT_2[1] - ACCENT[1]) * t),
                round(ACCENT[2] + (ACCENT_2[2] - ACCENT[2]) * t),
                235,
            ),
        )
    img.alpha_composite(bar, (x, y))
    return y + step


def render(payload: dict[str, Any], *, site: str = "") -> bytes | None:
    """把 :func:`payload_for` 的内容画成 PNG（**阻塞**，调用方负责放进线程）。

    画法：先画到一张够高的画布上，最后按实际用掉的高度裁掉——省掉一堆高度预估，
    也让「规则条目多一条」这种事不需要改任何布局常量。
    """
    try:
        Image, ImageDraw, _Filter, _ImageFont = _pil()
    except Exception as exc:  # noqa: BLE001  (Pillow 装坏了 / 缺依赖：这次没图，退回文本)
        log.info("没有装 Pillow，卡片渲染跳过 | %s", exc)
        return None
    try:
        img = Image.new("RGBA", (WIDTH, MAX_HEIGHT), (*PANEL, 255))
        draw = ImageDraw.Draw(img)
        f_title = fonts.load("cjk", 56)
        # 副标题那行是「编号 · 状态 · 一句话」，**含中文**（「筹备中」「第 1 届」…）：
        # 必须用 CJK 字体。曾经这里用等宽西文字体（bahnschrift），中文全成了空心方块。
        f_sub = fonts.load("cjk", 24)
        f_key = fonts.load("cjk", 24)
        f_val = fonts.load("cjk", 26)
        f_body = fonts.load("cjk", 26)
        f_tab = fonts.load("cjk", 26)
        f_note = fonts.load("cjk", 22)
        inner = WIDTH - PAD * 2

        y = PAD
        # ---- 标题区 ----
        for line in _wrap(str(payload.get("title") or "比赛"), f_title, inner)[:2]:
            draw.text((PAD, y), line, font=f_title, fill=TXT)
            y += int(f_title.size * 1.25)
        sub_bits = [b for b in (payload.get("id"), payload.get("status"), payload.get("headline")) if b]
        if sub_bits:
            draw.text((PAD, y), " · ".join(str(b) for b in sub_bits), font=f_sub, fill=ACCENT)
            y += int(f_sub.size * 1.6)
        # 渐变小条：与站点品牌线同色
        y = _brand_bar(img, PAD, y, inner)

        def panel_lines(pairs: list[tuple[str, str]], *, label: str = "") -> int:
            """画一块「键：值」面板，返回新的 y。

            「键」在左边（暗色），值的第一行跟它**同一行**，长值折行时缩进对齐到值那一列。
            """
            nonlocal y
            if not pairs:
                return y
            if label:
                draw.text((PAD + 18, y), label, font=f_key, fill=ACCENT)
                y += int(f_key.size * 1.9)
            for key, value in pairs:
                key_span = int(f_key.getlength(key) + 18)
                lines = _wrap(str(value), f_val, inner - key_span - 36) or [""]
                draw.text((PAD + 8, y), key, font=f_key, fill=DIM)
                for index, line in enumerate(lines):
                    draw.text((PAD + 8 + key_span, y), line, font=f_val, fill=TXT)
                    if index < len(lines) - 1:
                        y += 44
                y += 44
            draw.line([(PAD, y - 14), (WIDTH - PAD, y - 14)], fill=LINE, width=1)
            y += 10
            return y

        y = panel_lines(list(payload.get("meta") or []))
        y = panel_lines(list(payload.get("groups") or []), label="分组名单")

        # ---- 比赛规则（就是「规则」面板那一份，自动跟着赛制变）----
        for section in payload.get("sections") or []:
            items = [item for item in (section.get("items") or []) if item]
            if not items:
                continue
            title = str(section.get("title") or "")
            if title:
                tab_w = int(f_tab.getlength(title)) + 40
                tab = Image.new("RGBA", (tab_w, f_tab.size + 20), (*ACCENT, 235))
                img.alpha_composite(tab, (PAD, y))
                ImageDraw.Draw(img).text(
                    (PAD + 20, y + 9), title, font=f_tab, fill=INK
                )
                y += f_tab.size + 34
            for item in items:
                wrapped = _wrap(str(item), f_body, inner - 26)
                for index, line in enumerate(wrapped):
                    if index == 0:
                        draw.text((PAD + 6, y), "·", font=f_body, fill=ACCENT)
                    draw.text((PAD + 26, y), line, font=f_body, fill=TXT)
                    y += int(f_body.size * 1.42)
            y += 14
            if y > MAX_HEIGHT - 400:
                draw.text((PAD + 26, y), "…（还有更多规则，见站点「比赛规则」）", font=f_note, fill=WARN)
                y += 40
                break

        # ---- 比赛简介（原文照排，**尊重用户敲的换行**）----
        note = str(payload.get("note") or "").strip()
        if note and y < MAX_HEIGHT - 260:
            title = str(payload.get("noteTitle") or "比赛简介")
            tab_w = int(f_tab.getlength(title)) + 40
            tab = Image.new("RGBA", (tab_w, f_tab.size + 20), (*DIM, 60))
            img.alpha_composite(tab, (PAD, y))
            ImageDraw.Draw(img).text((PAD + 20, y + 9), title, font=f_tab, fill=TXT)
            y += f_tab.size + 34
            for line in _wrap_multiline(note, f_note, inner):
                if y > MAX_HEIGHT - 140:
                    break
                draw.text((PAD + 6, y), line, font=f_note, fill=DIM)
                y += int(f_note.size * 1.5)
            y += 16

        # ---- 页脚 ----
        y = min(y, MAX_HEIGHT - 110)
        draw.line([(PAD, y), (WIDTH - PAD, y)], fill=LINE, width=1)
        y += 26
        draw.text((PAD, y), str(payload.get("caption") or ""), font=f_note, fill=DIM)
        if site:
            draw.text((PAD, y + 34), f"{site} · 规则随赛制自动生成", font=f_note, fill=ACCENT)
            y += 34
        cropped = img.crop((0, 0, WIDTH, min(MAX_HEIGHT, y + PAD)))

        import io

        buf = io.BytesIO()
        # 转 RGB 再存：截图类内容用不着 alpha，JPEG 化也小得多
        cropped.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        log.warning("卡片渲染失败（本次退回纯文本推送）", exc_info=True)
        return None


def _hex_color(raw: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """``#rrggbb`` / ``#rgb`` → RGB；认不出来就退回给定颜色（队伍没配色时用）。"""
    text = str(raw or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return fallback
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return fallback


def _clip(text: str, font, width: int) -> str:
    """把一段文字截到给定宽度（放不下就加省略号）——方框里没有滚动条。"""
    if font.getlength(text) <= width:
        return text
    trimmed = text
    while trimmed and font.getlength(trimmed + "…") > width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else ""


def render_result(payload: dict[str, Any], *, site: str = "") -> bytes | None:
    """把 :func:`payload_for_result` 的内容画成 PNG（**阻塞**，调用方负责放进线程）。

    画法：小组赛逐组列出（积分表 + **每一场**比分），淘汰赛画成树状图（方框 + 折线 +
    冠军框）——就是站点「对阵总览」的那张图。先画到一张够大（也够宽）的画布上，
    最后按实际用掉的高度裁掉。
    """
    try:
        Image, ImageDraw, _Filter, _ImageFont = _pil()
    except Exception as exc:  # noqa: BLE001  (Pillow 装坏了：这次没图，退回文本)
        log.info("没有装 Pillow，结果卡片渲染跳过 | %s", exc)
        return None
    try:
        tree = dict(payload.get("tree") or {})
        width = min(MAX_RESULT_WIDTH, max(WIDTH, int(tree.get("width") or 0) + PAD * 2))
        img = Image.new("RGBA", (width, MAX_HEIGHT), (*PANEL, 255))
        draw = ImageDraw.Draw(img)
        f_title = fonts.load("cjk", 56)
        f_sub = fonts.load("cjk", 24)
        f_sec = fonts.load("cjk", 30)
        f_key = fonts.load("cjk", 22)
        f_body = fonts.load("cjk", 24)
        f_small = fonts.load("cjk", 20)
        f_note = fonts.load("cjk", 22)
        f_box = fonts.load("cjk", 21)
        f_code = fonts.load("cjk", 17)
        inner = width - PAD * 2
        right = PAD + inner

        y = PAD
        for line in _wrap(str(payload.get("title") or "比赛"), f_title, inner)[:2]:
            draw.text((PAD, y), line, font=f_title, fill=TXT)
            y += int(f_title.size * 1.25)
        sub_bits = [
            b for b in (payload.get("id"), payload.get("statusLabel"), payload.get("headline")) if b
        ]
        if sub_bits:
            draw.text((PAD, y), " · ".join(str(b) for b in sub_bits), font=f_sub, fill=ACCENT)
            y += int(f_sub.size * 1.6)
        y = _brand_bar(img, PAD, y, inner)

        def section(title: str, note: str = "") -> None:
            """区块标题：强调色底板 + 右边一句说明。"""
            nonlocal y
            tab = Image.new(
                "RGBA", (int(f_sec.getlength(title)) + 40, f_sec.size + 18), (*ACCENT, 235)
            )
            img.alpha_composite(tab, (PAD, y))
            ImageDraw.Draw(img).text((PAD + 20, y + 8), title, font=f_sec, fill=INK)
            if note:
                draw.text((PAD + int(f_sec.getlength(title)) + 58, y + 12), note, font=f_key, fill=DIM)
            y += f_sec.size + 34

        def gap(px: int = 18) -> None:
            nonlocal y
            y += px

        # ---- 小组赛：每组一张积分表 + 逐场比分 ----
        for group in payload.get("groups") or []:
            if y > MAX_HEIGHT - 320:
                break
            section(f"{group.get('key')} 组", str(group.get("note") or ""))
            rows = group.get("rows") or []
            if rows:
                heads = [("#", 8, "left"), ("队伍", 62, "left"), ("场次", 230, "right"),
                         ("胜", 170, "right"), ("负", 110, "right"), ("名次分", 0, "right")]
                for text, offset, align in heads:
                    x = (right - offset) if align == "right" else (PAD + offset)
                    if align == "right":
                        x -= int(f_small.getlength(text))
                    draw.text((x, y), text, font=f_small, fill=DIM)
                y += int(f_small.size * 1.6)
                draw.line([(PAD, y - 6), (right, y - 6)], fill=LINE, width=1)
                for row in rows:
                    seed = int(row.get("seed") or 0)
                    cells = [
                        (str(row.get("rank") or "—"), 8, "left", ACCENT),
                        (_clip(str(row.get("name") or ""), f_body, 300), 62, "left", TXT),
                        (str(row.get("played") or 0), 230, "right", TXT),
                        (str(row.get("win") or 0), 170, "right", TXT),
                        (str(row.get("lose") or 0), 110, "right", TXT),
                        (str(row.get("placement") or 0), 0, "right", ACCENT),
                    ]
                    for text, offset, align, color in cells:
                        x = (right - offset) if align == "right" else (PAD + offset)
                        if align == "right":
                            x -= int(f_body.getlength(text))
                        draw.text((x, y), text, font=f_body, fill=color)
                    if seed:
                        tag = f"晋级 #{seed}"
                        draw.text(
                            (PAD + 62 + int(f_body.getlength(_clip(str(row.get('name') or ''), f_body, 300))) + 14, y + 4),
                            tag, font=f_small, fill=WARN,
                        )
                    y += int(f_body.size * 1.5)
                gap(10)
            for match in group.get("matches") or []:
                if y > MAX_HEIGHT - 200:
                    break
                head = str(match.get("round") or "")
                draw.text((PAD + 6, y), head, font=f_small, fill=DIM)
                draw.text(
                    (PAD + 6 + int(f_small.getlength(head)) + 14, y + 2),
                    str(match.get("line") or ""),
                    font=f_body,
                    fill=TXT if match.get("done") else DIM,
                )
                y += int(f_body.size * 1.45)
            gap(14)

        # ---- 积分制：均分榜 + 逐局比分（没有小组赛 / 淘汰赛之分）----
        standings = payload.get("standings") or []
        if standings:
            section("积分榜", "均分排名 · 满场次才参与名次")
            draw.text((PAD + 8, y), "#", font=f_small, fill=DIM)
            draw.text((PAD + 62, y), "队伍", font=f_small, fill=DIM)
            for text, edge in (("场次", right - 230), ("均分", right - 120), ("总分", right)):
                draw.text((edge - int(f_small.getlength(text)), y), text, font=f_small, fill=DIM)
            y += int(f_small.size * 1.6)
            draw.line([(PAD, y - 6), (right, y - 6)], fill=LINE, width=1)
            for row in standings:
                numbers = [
                    (str(row.get("played") or 0), right - 230, TXT),
                    (str(row.get("average") or ""), right - 120, TXT),
                    (str(row.get("points") or 0), right, ACCENT),
                ]
                draw.text((PAD + 8, y), str(row.get("rank") or "—"), font=f_body, fill=ACCENT)
                draw.text(
                    (PAD + 62, y),
                    _clip(str(row.get("name") or ""), f_body, 320),
                    font=f_body,
                    fill=TXT,
                )
                for text, x, color in numbers:
                    draw.text((x - int(f_body.getlength(text)), y), text, font=f_body, fill=color)
                y += int(f_body.size * 1.5)
            gap(14)
        matches = payload.get("matches") or []
        if matches:
            section("逐局比分", f"共 {len(matches)} 局")
            for match in matches:
                if y > MAX_HEIGHT - 200:
                    break
                head = str(match.get("round") or "")
                draw.text((PAD + 6, y), head, font=f_small, fill=DIM)
                draw.text(
                    (PAD + 6 + int(f_small.getlength(head)) + 14, y + 2),
                    str(match.get("line") or ""),
                    font=f_body,
                    fill=TXT if match.get("done") else DIM,
                )
                y += int(f_body.size * 1.45)
            gap(14)

        # ---- 淘汰赛：树状图（坐标已在 payload 里算好，这里只负责画）----
        if tree:
            single = bool(tree.get("single"))
            section(
                "淘汰赛",
                "单败淘汰 · 输一场即淘汰" if single else "双败淘汰 · 胜者组落败者进败者组",
            )
            ox, oy = PAD, y
            for band in tree.get("bands") or []:
                bw, bh = max(1, int(band.get("w") or 1)), max(1, int(band.get("h") or 1))
                img.alpha_composite(
                    Image.new("RGBA", (bw, bh), (*ACCENT, 12)),
                    (ox + int(band.get("x") or 0), oy + int(band.get("y") or 0)),
                )
                draw.text(
                    (ox + int(band.get("x") or 0) + 12, oy + int(band.get("titleY") or 0)),
                    str(band.get("title") or ""),
                    font=f_key,
                    fill=ACCENT,
                )
            for col in tree.get("columns") or []:
                draw.text(
                    (ox + int(col.get("x") or 0), oy + int(col.get("y") or 0)),
                    _clip(str(col.get("title") or ""), f_small, _TREE_BOX),
                    font=f_small,
                    fill=DIM,
                )
            for link in tree.get("links") or []:
                x1, y1 = ox + int(link.get("x1") or 0), oy + int(link.get("y1") or 0)
                x2, y2 = ox + int(link.get("x2") or 0), oy + int(link.get("y2") or 0)
                mx = x2 - _TREE_GAP_X // 2
                color = (*ACCENT, 200) if link.get("done") else (70, 92, 124, 200)
                draw.line([(x1, y1), (mx, y1), (mx, y2), (x2, y2)], fill=color, width=2)
            for node in tree.get("nodes") or []:
                nx, ny = ox + int(node.get("x") or 0), oy + int(node.get("y") or 0)
                live = node.get("status") == "live"
                draw.rounded_rectangle(
                    [nx, ny, nx + _TREE_BOX, ny + _TREE_BOX_H],
                    radius=10,
                    fill=BOX_FILL,
                    outline=WARN if live else BOX_EDGE,
                    width=2,
                )
                draw.text((nx + 12, ny + 7), str(node.get("code") or ""), font=f_code, fill=DIM)
                tag = str(node.get("statusLabel") or "")
                if tag:
                    draw.text(
                        (nx + _TREE_BOX - 12 - int(f_code.getlength(tag)), ny + 7),
                        tag,
                        font=f_code,
                        fill=WARN if live else DIM,
                    )
                sides = (node.get("sides") or [])[:2]
                row_h = max(30, (_TREE_BOX_H - 30) // max(1, len(sides)))
                for index, side in enumerate(sides):
                    sy = ny + 30 + index * row_h
                    dot = _hex_color(str(side.get("color") or ""), ACCENT)
                    draw.ellipse([nx + 12, sy + 5, nx + 24, sy + 17], fill=dot)
                    name = _clip(str(side.get("name") or ""), f_box, _TREE_BOX - 96)
                    draw.text(
                        (nx + 32, sy),
                        name,
                        font=f_box,
                        fill=TXT if (side.get("win") or node.get("status") != "done") else DIM,
                    )
                    score = str(side.get("score") or "")
                    if score:
                        draw.text(
                            (nx + _TREE_BOX - 12 - int(f_box.getlength(score)), sy),
                            score,
                            font=f_box,
                            fill=ACCENT if side.get("win") else DIM,
                        )
            champ = tree.get("champ") or {}
            cx, cy = ox + int(champ.get("x") or 0), oy + int(champ.get("y") or 0)
            draw.rounded_rectangle(
                [cx, cy, cx + _TREE_CHAMP_W, cy + _TREE_BOX_H],
                radius=10,
                fill=(26, 40, 62, 255),
                outline=ACCENT,
                width=2,
            )
            draw.text((cx + 14, cy + 8), "CHAMPION", font=f_code, fill=ACCENT)
            name = str(champ.get("name") or "")
            draw.text(
                (cx + 14, cy + 40),
                _clip(name or "—", f_box, _TREE_CHAMP_W - 28),
                font=f_box,
                fill=TXT if name else DIM,
            )
            y = oy + int(tree.get("height") or 0) + 26

        # ---- 页脚 ----
        y = min(y, MAX_HEIGHT - 110)
        draw.line([(PAD, y), (right, y)], fill=LINE, width=1)
        y += 26
        draw.text((PAD, y), str(payload.get("caption") or ""), font=f_note, fill=DIM)
        if site:
            draw.text((PAD, y + 34), f"{site} · 逐场结果与对阵自动生成", font=f_note, fill=ACCENT)
            y += 34
        cropped = img.crop((0, 0, width, min(MAX_HEIGHT, y + PAD)))

        import io

        buf = io.BytesIO()
        cropped.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        log.warning("结果卡片渲染失败（本次退回纯文本推送）", exc_info=True)
        return None


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
