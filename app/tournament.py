"""赛制引擎：固定队伍 + 小组赛单循环 + 标准双败淘汰。

   参与选手 ──随机组队──▶ T 支固定队伍（每队 team_size 人，全程不换人）
     └─ 小组赛：分 G 组单循环，得出 1..T 的总排名
         └─ 淘汰赛：总排名前 B 名晋级（B = 不大于 T 的最大 2 的幂，最小 2）
             ├─ 胜者组 WB：单败淘汰，B/2 → B/4 → … → 1
             ├─ 败者组 LB：胜者组落败者依次进入，共 2k-2 轮（minor / major 交替）
             └─ 总决赛 GF：胜者组冠军 vs 败者组冠军（单场定胜负）

   T ≤ 2 时跳过小组赛，直接进行总决赛。

设计要点：淘汰赛每一场只声明「席位来源」（``src_a`` / ``src_b``）与胜负结果，
双方阵容由 :func:`resolve_rounds` 依据上游结果推导——纯函数、可反复重算，
因此「败者组成员随比赛进程自动生成」不存在中间状态错乱；
上游结果被改写（重置）时，下游对局会在同一次前向遍历中自动作废。

本模块不涉及 IO 与 Web 框架，便于单独测试。
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from . import metrics
from .logging_conf import get_logger
from .models import MAX_SIDES, Player, Round, Rules, Side, Team

log = get_logger("tournament")

PALETTE = ("#22e0e8", "#ff2f8e", "#ffd83d", "#7d5cff", "#43e58a", "#ff8a3d", "#3db2ff", "#e05cff")

# 淘汰赛轮次名称：按该轮开始时的剩余队数命名（十六强 / 八强 / 半决赛 …）
ROUND_NAMES = {32: "三十二强", 16: "十六强", 8: "八强", 4: "半决赛", 2: "决赛"}

STAGE_ORDER = {"league": 0, "group": 1, "wb": 2, "lb": 3, "gf": 4}
STAGE_NAMES = {"league": "积分赛", "group": "小组赛", "wb": "胜者组", "lb": "败者组", "gf": "总决赛"}

# 淘汰赛规模可选值（2 的幂）
_MIN_SIZE = 2


# --------------------------------------------------------------------------- #
# 基础数学
# --------------------------------------------------------------------------- #
def power_of_two_floor(n: int) -> int:
    """不大于 ``n`` 的最大 2 的幂（n ≤ 1 时为 1）。"""
    if n <= 1:
        return 1
    return 1 << (n.bit_length() - 1)


def bracket_size(team_count: int) -> int:
    """淘汰赛规模上限：不大于队伍数的最大 2 的幂，最小 2。

    这样轮次名称自然落在「三十二强 / 十六强 / 八强 / 半决赛 / 决赛」上，
    并且永远不会出现轮空（B ≤ 队伍数），双败结构因此保持完整。
    """
    return max(_MIN_SIZE, power_of_two_floor(max(_MIN_SIZE, team_count)))


def size_options(team_count: int) -> list[int]:
    """可选的淘汰赛规模：从 2 到可行上限的所有 2 的幂。"""
    top = bracket_size(team_count)
    out: list[int] = []
    size = _MIN_SIZE
    while size <= top:
        out.append(size)
        size *= 2
    return out


def resolve_size(team_count: int, requested: int = 0, *, strict: bool = True) -> int:
    """确定实际使用的淘汰赛规模；未指定时取可行上限（最大 2 的幂）。

    ``strict=True``（用户显式指定）时，非法值抛 ``ValueError``；
    ``strict=False``（沿用配置里的偏好）时，不可行则回退到可行上限，
    避免队伍数变化后旧设定把赛程生成卡住。
    """
    allowed = size_options(team_count)
    if not requested:
        return allowed[-1]
    want = int(requested)
    if want in allowed:
        return want
    if strict:
        raise ValueError(
            f"淘汰赛规模需为 2 的幂且不超过队伍数（{team_count} 支队可选："
            + " / ".join(f"{s} 强" for s in allowed)
            + "）"
        )
    return allowed[-1]


def group_count_for(team_count: int, per_match: int = 2) -> int:
    """小组数：**在「每组排得满、又不至于太小」的前提下，尽量多分组**。

    为什么倾向多分：人越多，一组里塞太多队的代价是**小组赛没完没了**（单循环场次按
    平方涨），出线名额少、冷门队伍也没机会。分细一点，小组赛更短、淘汰赛更热闹——
    这也是组织者最常手改的一项，所以自动值就该尽量靠向它。

    约束（按优先级）：

    1. 每组都排得满一场：``per_match=2`` 时每组至少 3 队（2 队一组等于一场定胜负，
       没有「小组」的意义）；``per_match=3/4`` 时每组至少 ``per_match`` 队；
    2. 分得匀（尽量整除，每组队数最多差 1）；
    3. 满足前两条的前提下**组数越多越好**；
    4. 都不满足时退回一组。

    组数不要求整除队数，所以质数队数（7 / 11 / 13…）也能拆开：7 队 → 3 + 2 + 2。
    """
    k = max(2, min(MAX_SIDES, per_match))
    if team_count <= max(2, k):
        return 1
    floor_per_group = 3 if k == 2 else k  # 一组最少几队才排得满（k=2 时别出现两人组）
    best: tuple[int, int, int] | None = None
    for groups in range(1, team_count + 1):
        if team_count // groups < floor_per_group:  # 最小一组也得凑得出一场
            continue
        largest = -(-team_count // groups)  # 最大一组（向上取整）
        even = team_count % groups == 0
        full = 0 if (k == 2 or largest % k == 0) else 1
        # **组数多者优先**（在「每组都排得满一场」的前提下）→ 排得满 → 分得匀。
        # 为什么组数排第一：人越多越该多分组（小组赛短、出线名额多），这也是组织者
        # 最常手改的一项；而「排得满」是次要的——少一个队轮休完全可以接受。
        score = (-groups, full, 0 if even else 1)
        if best is None or score < best:
            best = score
    return -best[0] if best else 1  # best = (-组数, 排得满, 匀)


def bracket_order(size: int) -> list[int]:
    """标准对阵表种子顺序：保证 1 号与 2 号只可能在决赛相遇。"""
    order = [1]
    while len(order) < size:
        span = len(order) * 2
        nxt: list[int] = []
        for seed in order:
            nxt.append(seed)
            nxt.append(span + 1 - seed)
        order = nxt
    return order


def wb_code(round_no: int, slot: int) -> str:
    return f"WB-{round_no}-{slot}"


def lb_code(round_no: int, slot: int) -> str:
    return f"LB-{round_no}-{slot}"


def wb_match_count(size: int, round_no: int) -> int:
    return size >> round_no


def lb_match_count(size: int, round_no: int) -> int:
    """败者组第 ``round_no`` 轮场次；minor / major 交替使场次每两轮减半。"""
    return size >> ((round_no + 1) // 2 + 1)


def wb_round_title(size: int, round_no: int) -> str:
    rounds = size.bit_length() - 1
    if round_no == rounds:
        return "胜者组决赛" if size >= 4 else "总决赛"
    return ROUND_NAMES.get(size >> (round_no - 1), f"胜者组第 {round_no} 轮")


def side_keys(count: int) -> list[str]:
    """同场 N 队对应的 A/B/C/D 编号。"""
    return [chr(ord("A") + i) for i in range(max(2, min(MAX_SIDES, count)))]


# --------------------------------------------------------------------------- #
# 结果判定：谁赢了 / 名次 / 名次分
#
# 「能填的都填，结果自动判定」：管理员只需录入各轮成绩或直接填本场成绩，
# 胜负、名次与名次分全部由这里推导，不再需要人工判断。
# --------------------------------------------------------------------------- #
def placement_points(side_count: int, rank: int) -> int:
    """名次分：第 1 名得 ``side_count`` 分，依次递减，最低 1 分。

    2 队即 2/1（等价于胜/负），3 队即 3/2/1，4 队即 4/3/2/1。
    """
    if rank <= 0:
        return 0
    return max(1, side_count - rank + 1)


def judge_round(rnd: Round, *, allow_draw: bool = False, scoring: object = metrics.INTEGER) -> str:
    """按录入内容自动判定本场胜负与名次，返回 winner（``""`` = 尚未确定）。

    判定顺序：

    1. 填了轮次（2 队）→ 大比分 = 各轮胜负计数，成绩 = 各轮合计；
       每轮谁赢由**判断标准**决定（数值高胜或数值低胜，见 :mod:`app.metrics`）；
    2. 否则比较 ``score``（本场成绩）；
    3. 仍然相同再比 ``points``（小分 / 累计成绩）；
    4. 还相同：2 队且允许平局 → ``DRAW``；多队并列第一 → 交回管理员指定。

    **没有成绩**的一方（详见 :meth:`app.metrics.Scoring.has_result`）在任何口径下都垫底：
    数值型的 0 是合法读数（0 分照样参与排名），时间型的 0 与数值型的 ``-1`` 才是没成绩——
    否则时间型里 0 秒会被当成最快的人。
    """
    sc = metrics.as_scoring(scoring)
    sides = rnd.sides
    count = len(sides)
    if count == 2 and rnd.sets:
        wins = [0, 0]
        totals = [0, 0]
        for item in rnd.sets:
            # 只把「填了的那一方」计进合计：另一边是 MISSING（没填），不该当负数减进去
            totals[0] += max(0, int(item.a))
            totals[1] += max(0, int(item.b))
            left = sc.sort_key(item.a)
            right = sc.sort_key(item.b)
            if left < right:
                wins[0] += 1
            elif right < left:
                wins[1] += 1
        for i in (0, 1):
            sides[i].score = wins[i]
            sides[i].points = totals[i]

    for side in sides:
        side.rank = 0

    # 弃权方一律垫底，并且不参与名次竞争
    playing = [i for i, side in enumerate(sides) if not side.forfeit]
    if not playing:
        return ""

    # 填了轮次时 score 是「赢的轮数」（计数，多者胜）；否则 score 就是成绩本身
    # （按判断标准比大小）。这个区别见 app/metrics.Scoring.judge_key。
    counted = count == 2 and bool(rnd.sets)
    order = sorted(
        playing,
        key=lambda i: sc.judge_key(sides[i].score, sides[i].points, counted=counted),
    )
    rank = 0
    previous: tuple[int, int] | None = None
    for position, i in enumerate(order, start=1):
        current = (sides[i].score, sides[i].points)
        if current != previous:
            rank = position
            previous = current
        sides[i].rank = rank
    for i in range(count):
        if sides[i].forfeit:
            sides[i].rank = len(playing) + 1
    # 没有成绩的一方**名次钉在最后一位**（垫底），与「有成绩但并列」区分开：
    # 并列是「成绩一样」（按并列那一位算名次分），没成绩是「压根没成绩」——4 队里 3 队
    # 没跑完时，他们不该占到「并列第 2」那份名次分（那和跑完拿了第 2 一样多）。
    for i in playing:
        if not sc.has_result(sides[i].score) and not sides[i].points:
            sides[i].rank = count

    if not any(
        sc.has_total(sides[i].score, sides[i].points, has_rounds=bool(rnd.sets))
        or sides[i].points
        for i in playing
    ):
        # 谁都没填比分：若只剩一方没弃权，直接判其获胜（弃权判罚）
        if len(playing) == 1:
            return chr(ord("A") + playing[0])
        return ""

    best = (sides[order[0]].score, sides[order[0]].points)
    tied = [i for i in order if (sides[i].score, sides[i].points) == best]
    if len(tied) > 1:
        return "DRAW" if (count == 2 and allow_draw) else ""
    return chr(ord("A") + order[0])


def round_is_decided(rnd: Round) -> bool:
    """本场是否已有明确胜者（平局也算已判定）。"""
    return rnd.status == "done" and bool(rnd.winner)


def source_text(ref: str) -> str:
    """把席位引用翻译成展示文案（**兜底**，见下）。

    ``seed:N`` 里 N 是**种子号**，不是「小组赛第 N 名」：首轮是交叉配对
    （见 :func:`bracket_seeds`），种子 3 完全可能是「A 组第 2」。有小组赛时
    真正的出处由 :func:`seed_sources` 给（「A 组第 2」），这里只在拿不到时兜底——
    没有小组赛（队伍太少）时种子号就等于队伍顺序。
    """
    if not ref:
        return ""
    if ref.startswith("seed:"):
        return f"{ref[5:]} 号种子"
    code, _, flag = ref.rpartition(":")
    return f"{code} {'胜者' if flag == 'W' else '败者'}"


def seed_sources(
    teams: list[Team],
    rounds: list[Round],
    scoring: object,
    seeds: list[str],
) -> dict[str, str]:
    """每个种子席位的**出处文案**：``{"seed:3": "A 组第 2", …}``。

    「抽到哪个位置」与「从哪来」是两回事：交叉配对后，种子里装的是各组第几名
    并不固定，所以席位文案要按**队伍真正的出处**写；没有小组赛时回落到
    :func:`source_text` 的「N 号种子」。
    """
    if not seeds:
        return {}
    if not any(r.stage == "group" for r in rounds):
        return {}
    tables = group_tables(teams, rounds, scoring)
    where = {
        str(row["teamId"]): (str(row.get("group") or "A"), int(row.get("rank") or 0))
        for rows in tables.values()
        for row in rows
    }
    out: dict[str, str] = {}
    for pos, team_id in enumerate(seeds, start=1):
        group, rank = where.get(str(team_id), ("", 0))
        if group and rank:
            out[f"seed:{pos}"] = f"{group} 组第 {rank}"
    return out


# --------------------------------------------------------------------------- #
# 组队：随机分配队友，之后固定不变
# --------------------------------------------------------------------------- #
def auto_form_teams(
    players: Iterable[Player],
    team_size: int = 2,
    seed: int | None = None,
    *,
    merge_remainder: bool = False,
) -> tuple[list[Team], list[str]]:
    """把参与选手随机分成固定队伍（队友随机）。返回 ``(队伍, 提示)``。

    ``merge_remainder=True`` 时把凑不满一队的零头**平均并入**已有队伍，
    这样没人会被落下（队伍人数因此可能不相等）；默认仍按整队切分、
    零头进候选池（组队台里可以随时被人顶上）。
    """
    size = max(1, team_size)
    pool = [p for p in players if p.active and p.name]
    rng = random.Random(seed if seed is not None else random.randrange(1_000_000))
    shuffled = list(pool)
    rng.shuffle(shuffled)

    full = len(shuffled) // size
    members_groups: list[list[Player]] = [
        shuffled[i * size : (i + 1) * size] for i in range(full)
    ]
    leftover = shuffled[full * size :]

    warnings: list[str] = []
    if leftover and merge_remainder and members_groups:
        for i, player in enumerate(leftover):
            members_groups[i % len(members_groups)].append(player)
        names = "、".join(p.display_name for p in leftover)
        warnings.append(
            f"{len(leftover)} 人凑不满整队（{names}），已平均并入前排队伍"
            f"（部分队伍人数会多于 {size} 人）。"
        )
        leftover = []
    elif leftover and not members_groups:
        # 连一支队都凑不齐：直接全部组一队，避免「无法组队」
        members_groups = [list(shuffled)]
        leftover = []
        warnings.append(f"参与选手不足 {size} 人，已把全部 {len(shuffled)} 人编为一队。")

    teams = [_make_team(i + 1, members) for i, members in enumerate(members_groups)]

    if not teams:
        raise ValueError(f"参与选手不足 {size} 人，无法组队")
    if leftover:
        names = "、".join(p.display_name for p in leftover)
        warnings.append(f"{len(leftover)} 人凑不满一队（{names}），暂未编入队伍（在组队台的候选池里）。")
    if len(teams) < 2:
        warnings.append("目前只有 1 支队伍，至少需要 2 支队才能进行比赛。")
    log.info(
        "随机组队完成 | 选手=%d | 队伍=%d | 每队=%d 人 | 零头并入=%s",
        len(pool),
        len(teams),
        size,
        merge_remainder,
    )
    return teams, warnings


#: 自动队名的长度上限（**中文字符数**，中文一个算一个）：与前端
#: ``static/js/teams.js`` 的 ``NAME_MAX`` 是同一个数——两边都改才不会出现
#: 「界面让填 8 个字、后端按 6 个字存」。
TEAM_NAME_MAX = 8


def auto_team_name(names: Iterable[str]) -> str:
    """按队员名拼一个队名：**每人最多分到 ``TEAM_NAME_MAX ÷ 人数`` 个字符**。

    人数越多，每人能占的字就越少（不然 4 个人各写满就直接爆掉上限）。取整是**向下**的：
    3 人一队时每人 2 个字符（8 ÷ 3 = 2），拼出来 6 个字——留白比硬塞好，
    队名越挤越不像名字。

    例（上限 8）：

    * 1 人 → 这个人最多 8 个字（一个人也要看得清是谁）；
    * 2 人 → 各 4 个字（合计 8）；
    * 3 人 → 各 2 个字（合计 6）；
    * 4 人 → 各 2 个字（合计 8）。

    以前这里是 ``" & ".join(全名)``：名字一长就把队名撑成一串，界面上只能看见一个字的缩写；
    现在按固定预算截断，长度可控、也还认得出人。
    """
    clean = [str(n or "").strip() for n in names if str(n or "").strip()]
    if not clean:
        return ""
    each = max(1, TEAM_NAME_MAX // len(clean))
    return "".join(n[:each] for n in clean)[:TEAM_NAME_MAX]


def _make_team(index: int, members: list[Player]) -> Team:
    names = [m.display_name for m in members]
    short = "".join(n[0] for n in names if n)[:3] or f"T{index}"
    return Team(
        id=f"t{index:02d}",
        name=auto_team_name(names) or f"T{index}",
        short=short,
        color=PALETTE[(index - 1) % len(PALETTE)],
        player_ids=[m.id for m in members],
    )


def team_of_player(teams: Iterable[Team], player_id: str) -> Team | None:
    for team in teams:
        if player_id in team.player_ids:
            return team
    return None


# --------------------------------------------------------------------------- #
# 小组赛
# --------------------------------------------------------------------------- #
def heat_sizes(team_count: int, per_match: int) -> list[int]:
    """一轮里各场的队数：尽量均分，且每场至少 2 队、不超过 ``per_match``。

    例：6 队 / 同场 4 → ``[3, 3]``（而不是 4 + 2）；5 队 / 同场 3 → ``[2, 3]``；
    5 队 / 同场 2 → ``[2, 2]``（这一轮有 1 队轮休）。
    """
    k = max(2, min(MAX_SIDES, per_match))
    count = max(2, team_count)
    matches = max(1, -(-count // k))            # ceil(count / k)
    while matches > 1 and matches * 2 > count:  # 保证每场至少 2 队
        matches -= 1
    sizes: list[int] = []
    remaining = count
    for index in range(matches):
        left = matches - index
        size = min(k, max(2, remaining // left))
        sizes.append(size)
        remaining -= size
    return sorted(sizes, reverse=True)          # 人数多的场次排在前面


def _pair_rounds(items: list[Any]) -> list[list[list[Any]]]:
    """标准单循环：每两队**恰好相遇一次**（圆桌轮转）。

    两个要点，缺一就会漏场次：

    1. 槽位要**折叠**成对——第 ``i`` 位 vs 倒数第 ``i`` 位（不是相邻两位成对），
       再固定 0 号位把其余顺转一格；
    2. 奇数队要补一个「轮空」占位凑成偶数槽位——圆桌法在奇数个槽位上
       只转 n-1 步就回到原点，不补位会漏掉大半对阵（3 队只排出 2 场：
       B 与 C 永远碰不上，A 连打两轮）。

    补位后每队轮休一次，n 队排满 n(n-1)/2 场。
    """
    slots: list[Any] = list(items)
    if len(slots) % 2:
        slots.append(None)                      # None = 轮空占位，这一轮该队轮休
    count = len(slots)
    half = count // 2
    rounds: list[list[list[Any]]] = []
    for _ in range(count - 1):
        matches: list[list[Any]] = []
        for slot in range(half):
            real = [x for x in (slots[slot], slots[count - 1 - slot]) if x is not None]
            if len(real) >= 2:
                matches.append(real)
        rounds.append(matches)
        slots = [slots[0], slots[-1], *slots[1:-1]]   # 0 号位固定，其余顺转一格
    return rounds


def _multi_rounds(items: list[Any], per_match: int) -> list[list[list[Any]]]:
    """多队同场（3~4 队一场）的轮转排法：每场尽量凑满 ``per_match`` 队。"""
    count = len(items)
    sizes = heat_sizes(count, per_match)
    if sum(sizes) > count:                      # 理论上不会发生，稳妥兜底
        sizes = [count]
    arr: list[Any] = list(items)
    rounds: list[list[list[Any]]] = []
    seen: set[frozenset[frozenset[int]]] = set()
    for _ in range(count - 1):
        matches: list[list[Any]] = []
        cursor = 0
        for size in sizes:
            if cursor >= count:
                break
            chunk = arr[cursor : cursor + size]
            cursor += size
            if len(chunk) >= 2:
                matches.append(chunk)
        key = frozenset(frozenset(id(item) for item in match) for match in matches)
        if matches and key not in seen:         # 奇数队会转回原点，别排第二遍同样的场
            seen.add(key)
            rounds.append(matches)
        arr = [arr[0], arr[-1], *arr[1:-1]]
    return rounds


def _heat_schedule(items: list[Any], per_match: int) -> list[list[list[Any]]]:
    """把 ``items`` 编排成若干轮，每轮由若干场「N 队同场」组成。

    * ``per_match=2`` → 标准单循环（每两队相遇一次，见 :func:`_pair_rounds`）；
    * ``per_match=3/4`` → 每场尽量 3~4 队同场，各队出场次数保持均衡；
    * 队数不是 ``per_match`` 的整数倍时**均分到各场**（6 队 / 同场 4 → 3 + 3），
      仍然排不下的队这一轮轮休。

    返回 ``[[[同场队伍…], …], …]``（外层 = 轮次）。
    """
    if len(items) < 2:
        return []
    if per_match <= 2:
        return _pair_rounds(items)
    return _multi_rounds(items, per_match)


def group_pairings(items: list[Any], per_match: int = 2) -> list[list[Any]]:
    """小组赛的默认排法：按（轮次 → 场次）顺序**拍平**成一维。

    与 :func:`build_group_rounds` 生成顺序一一对应，因此可以按位置写回每一局
    ——「恢复默认对阵」用的就是它（手改过的安排会被覆盖）。
    """
    return [match for rnd in _heat_schedule(list(items), per_match) for match in rnd]


def assign_groups(teams: list[Team], group_count: int) -> list[Team]:
    """按顺序轮转分入 A/B/C… 组（队伍本身已随机，因此分组自然均衡）。"""
    count = max(1, min(group_count, len(teams))) if teams else 1
    letters = [chr(ord("A") + i) for i in range(count)]
    for idx, team in enumerate(teams):
        team.group = letters[idx % len(letters)]
    return teams


def build_group_rounds(
    teams: list[Team], teams_per_match: int = 2, start_index: int = 1
) -> list[Round]:
    """生成小组赛对局（组内轮转；每场 ``teams_per_match`` 队同场）。"""
    grouped: dict[str, list[Team]] = defaultdict(list)
    for team in teams:
        grouped[team.group or "A"].append(team)

    rounds: list[Round] = []
    index = start_index
    for key in sorted(grouped):
        for round_no, matches in enumerate(_heat_schedule(grouped[key], teams_per_match), start=1):
            for slot, group in enumerate(matches, start=1):
                rounds.append(
                    Round(
                        index=index,
                        code=f"G-{key}-{round_no}-{slot}",
                        stage="group",
                        bracket_round=round_no,
                        slot=slot,
                        # 同一轮会有多场，标签必须带场次，否则几张卡看起来一模一样
                        label=f"{key} 组 · 第 {round_no} 轮 · 第 {slot} 场",
                        sides=[
                            Side(
                                team_id=team.id,
                                player_ids=list(team.player_ids),
                                label=team.short or team.label,
                            )
                            for team in group
                        ],
                    )
                )
                index += 1
    return rounds


def table_sort_key(row: dict[str, Any], scoring: object = metrics.INTEGER) -> tuple[Any, ...]:
    """小组赛排序：名次分 → 分项 → 队名（完全确定，无随机）。

    * 数值低胜（时间型最常见）：完成场次 → 总成绩（未完赛的人**不能**因为
      「没跑完所以成绩小」排到前面，所以完成场次必须排在总成绩之前）；
    * 数值高胜：净胜分 → 总成绩（都按降序，分多者靠前）。
    """
    if metrics.as_scoring(scoring).low_wins:
        return (-row["placement"], -row["finished"], row["spent"], row["name"])
    return (-row["placement"], -row["diff"], -row["scored"], row["name"])


def group_tables(
    teams: list[Team], rounds: list[Round], scoring: object = metrics.INTEGER
) -> dict[str, list[dict[str, Any]]]:
    """小组赛积分表（按小组分组，已排序并标注名次）。

    每场按**名次分**结算（2 队 = 2/1，3 队 = 3/2/1，4 队 = 4/3/2/1），
    因此 2 队对阵与多队同场可以放在同一张表里比较；名次分本身与计分口径无关。

    ``finished`` / ``spent``（完成场次 / 成绩合计）只在数值低胜时参与排序，
    数值高胜仍按历史上的净胜分与总得分排。
    """
    sc = metrics.as_scoring(scoring)
    rows: dict[str, dict[str, Any]] = {
        t.id: {
            "teamId": t.id,
            "name": t.label,
            "short": t.short or t.label,
            "color": t.color,
            "group": t.group or "A",
            "played": 0,
            "win": 0,
            "lose": 0,
            "placement": 0,
            "scored": 0,
            "conceded": 0,
            "diff": 0,
            "finished": 0,
            "spent": 0,
            "bestRank": 0,
            "rank": 0,
        }
        for t in teams
    }
    for rnd in rounds:
        if rnd.stage != "group" or rnd.status != "done" or not rnd.winner:
            continue
        parts = [s for s in rnd.sides if s.team_id in rows]
        if len(parts) < 2:
            continue
        count = len(parts)
        for side in parts:
            row = rows[side.team_id]
            # 没有成绩（MISSING / 时间型的 0）按 0 计入累计：负数会把总分越加越小
            mine = max(0, side.score)
            others = sum(max(0, other.score) for other in parts if other is not side)
            row["played"] += 1
            row["scored"] += mine
            row["conceded"] += others
            # 该场的「总成绩」：填了轮次就是各轮合计，否则就是 score（与前端一致）
            total = sc.round_total(side.score, side.points, bool(rnd.sets))
            if sc.has_total(side.score, side.points, has_rounds=bool(rnd.sets)):
                row["finished"] += 1
                row["spent"] += total
            # 平局没有名次，按并列末位参与名次分计算
            place = side.rank or count
            row["placement"] += placement_points(count, place)
            if place == 1:
                row["win"] += 1
            elif rnd.winner != "DRAW":
                row["lose"] += 1
            if place and (not row["bestRank"] or place < row["bestRank"]):
                row["bestRank"] = place

    tables: dict[str, list[dict[str, Any]]] = {}
    for key in sorted({r["group"] for r in rows.values()}):
        bucket = [r for r in rows.values() if r["group"] == key]
        for row in bucket:
            row["diff"] = row["scored"] - row["conceded"]
        bucket.sort(key=lambda row: table_sort_key(row, sc))
        for pos, row in enumerate(bucket, start=1):
            row["rank"] = pos
        tables[key] = bucket
    return tables


def overall_ranking(
    tables: dict[str, list[dict[str, Any]]], scoring: object = metrics.INTEGER
) -> list[str]:
    """小组赛总排名：先所有小组第 1 名（按战绩），再所有第 2 名，依此类推。

    这样「前 B 名晋级」等价于：各组名次靠前者优先，保证各组头名稳进淘汰赛。

    **它是「谁晋级」的依据，不是「怎么配对」的依据**——直接拿它填对阵表会让同组两队
    在首轮相遇，配对要用 :func:`bracket_seeds`。
    """
    sc = metrics.as_scoring(scoring)
    depth = max((len(rows) for rows in tables.values()), default=0)
    ranking: list[str] = []
    for pos in range(depth):
        chunk = [rows[pos] for rows in tables.values() if len(rows) > pos]
        chunk.sort(key=lambda row: table_sort_key(row, sc))
        ranking.extend(row["teamId"] for row in chunk)
    return ranking


def bracket_seeds(
    tables: dict[str, list[dict[str, Any]]], size: int, scoring: object = metrics.INTEGER
) -> list[str]:
    """把出线队排成**淘汰赛种子顺序**（``[0]`` = 1 号种子，长度 = ``size``）。

    「总排名」与「种子顺序」不是一回事：总排名是「先列各组第 1 名、再列第 2 名…」，
    而首轮配对是「1 号对 8 号、3 号对 6 号…」。照总排名直接填表就会**让同组两队在
    首轮相遇**——3 个小组出线 8 队时种子 3 与 6 恰好同为 C 组（C 组第 1 打 C 组第 2）；
    两个小组各出线 4 队时，第 1 名甚至会碰到本组第 3 名。

    这里按通行做法**交叉配对**，并保住「1 号与 2 号只可能在决赛相遇」：

    1. 排名前一半为**强侧**（各组名次靠前者），后一半为**弱侧**；
    2. 强侧按战绩顺序（1 → 4 → 2 → 3，就是 ``bracket_order`` 的强侧位序）去弱侧挑对手，
       **从最弱的开始挑，但优先挑不同组的**；
    3. 实在挑不出不同组的（某一组的出线队超过总数一半，数学上躲不开）才允许同组相遇。

    出线队不是偶数（理论上不会：淘汰赛规模恒为 2 的幂）时按总排名原样返回——
    交叉配对的前提是上下半区一样大。
    """
    count = max(2, int(size))
    ranking = overall_ranking(tables, scoring)[:count]
    if len(ranking) != count or count % 2:
        return ranking
    group_of = {
        str(row["teamId"]): str(row.get("group") or "A")
        for rows in tables.values()
        for row in rows
    }
    half = count // 2
    strong, weak = ranking[:half], ranking[half:]
    pool = list(weak)
    pairs: list[tuple[str, str]] = []
    for slot in bracket_order(half):
        top = strong[slot - 1]
        # 从最弱的开始挑：战绩最好的先挑，挑走的还是弱侧里最弱的那个
        rival = next(
            (cand for cand in reversed(pool) if group_of.get(cand) != group_of.get(top)), ""
        )
        if not rival and pool:
            rival = pool[-1]  # 躲不开同组了（一组出线太多）
        if rival:
            pool.remove(rival)
        pairs.append((top, rival))
    order = bracket_order(count)
    seeds: list[str] = [""] * count
    for i, (top, rival) in enumerate(pairs):
        seeds[order[2 * i] - 1] = top
        if rival:
            seeds[order[2 * i + 1] - 1] = rival
    return [team_id for team_id in seeds if team_id]


def knockout_started(rounds: list[Round], scoring: object = metrics.INTEGER) -> bool:
    """淘汰赛是否**已经开打**：有场次不是「未开始」，或者已经录过成绩。

    这个标记决定要不要重排配对（见 :func:`advance_seeds`）。种子算法改过一版
    （从「总排名直接当种子」改成「交叉配对，同组首轮不相遇」），但**已经打下来的
    届必须保持原样**：对阵是当初生成、并且已经被打过的，拿新算法重排等于把已录的
    成绩作废（对阵一变，按规矩就得重打）。所以只对「还没开打」的届生效。
    """
    sc = metrics.as_scoring(scoring)
    return any(
        rnd.stage in ("wb", "lb", "gf")
        and (rnd.status != "pending" or round_has_result(rnd, sc))
        for rnd in rounds
    )


def group_stage_done(rounds: list[Round]) -> bool:
    group = [r for r in rounds if r.stage == "group"]
    return bool(group) and all(r.status == "done" and r.winner for r in group)


# --------------------------------------------------------------------------- #
# 淘汰赛骨架（标准双败）
# --------------------------------------------------------------------------- #
def build_knockout_rounds(
    size: int, start_index: int = 1, *, loser_bracket: bool = True
) -> list[Round]:
    """生成淘汰赛骨架（只有席位来源，阵容由 resolve 推导）。

    ``loser_bracket=True``（默认）双败淘汰：

    * 胜者组：``size/2 → size/4 → … → 1``；
    * 败者组：共 ``2k-2`` 轮，奇数轮（minor）由败者组内部淘汰，
      偶数轮（major）由败者组幸存者迎战胜者组同轮败者（反向配对，避免刚打完又相遇）；
    * 总决赛：胜者组冠军 vs 败者组冠军。

    ``loser_bracket=False`` 单败淘汰：只有胜者组，最后一轮直接就是决赛
    （输一场即淘汰，没有败者组与总决赛）。
    """
    size = max(2, size)
    if not loser_bracket:
        return _build_single_elim(size, start_index)
    if size == 2:
        return [
            Round(
                index=start_index,
                code="GF",
                stage="gf",
                bracket_round=1,
                slot=1,
                label="总决赛",
                src_a="seed:1",
                src_b="seed:2",
            )
        ]

    rounds_count = size.bit_length() - 1
    total_lb = max(0, 2 * rounds_count - 2)
    order = bracket_order(size)

    rounds: list[Round] = []
    index = start_index

    # ---- 胜者组 ----
    for r in range(1, rounds_count + 1):
        count = wb_match_count(size, r)
        title = wb_round_title(size, r)
        for m in range(1, count + 1):
            if r == 1:
                src_a = f"seed:{order[(m - 1) * 2]}"
                src_b = f"seed:{order[(m - 1) * 2 + 1]}"
            else:
                src_a = f"{wb_code(r - 1, m * 2 - 1)}:W"
                src_b = f"{wb_code(r - 1, m * 2)}:W"
            # 胜者组首轮败者两两配对进败者组；之后每一轮的败者直接进入同序号的败者组 major 轮
            loser_to = lb_code(1, (m + 1) // 2) if r == 1 else lb_code(2 * r - 2, m)
            rounds.append(
                Round(
                    index=index,
                    code=wb_code(r, m),
                    stage="wb",
                    bracket_round=r,
                    slot=m,
                    label=f"{title} · 第 {m} 场",
                    src_a=src_a,
                    src_b=src_b,
                    winner_to=wb_code(r + 1, (m + 1) // 2) if r < rounds_count else "GF",
                    loser_to=loser_to,
                )
            )
            index += 1

    # ---- 败者组 ----
    for i in range(1, total_lb + 1):
        count = lb_match_count(size, i)
        major_wb_round = i // 2 + 1          # 偶数轮（major）对应的胜者组轮次
        title = "败者组决赛" if i == total_lb else f"败者组第 {i} 轮"
        for m in range(1, count + 1):
            if i == 1:
                # 首轮：胜者组第 1 轮的败者两两配对
                src_a = f"{wb_code(1, m * 2 - 1)}:L"
                src_b = f"{wb_code(1, m * 2)}:L"
            elif i % 2 == 0:
                # major：败者组幸存者 vs 胜者组同轮败者（反向配对，避免刚打完又相遇）
                src_a = f"{lb_code(i - 1, m)}:W"
                src_b = f"{wb_code(major_wb_round, count - m + 1)}:L"
            else:
                # minor：败者组内部淘汰
                src_a = f"{lb_code(i - 1, m * 2 - 1)}:W"
                src_b = f"{lb_code(i - 1, m * 2)}:W"
            if i == total_lb:
                winner_to = "GF"
            elif lb_match_count(size, i + 1) == count:
                winner_to = lb_code(i + 1, m)
            else:
                winner_to = lb_code(i + 1, (m + 1) // 2)
            rounds.append(
                Round(
                    index=index,
                    code=lb_code(i, m),
                    stage="lb",
                    bracket_round=i,
                    slot=m,
                    label=f"{title} · 第 {m} 场",
                    src_a=src_a,
                    src_b=src_b,
                    winner_to=winner_to,
                )
            )
            index += 1

    # ---- 总决赛 ----
    rounds.append(
        Round(
            index=index,
            code="GF",
            stage="gf",
            bracket_round=1,
            slot=1,
            label="总决赛",
            src_a=f"{wb_code(rounds_count, 1)}:W",
            src_b=f"{lb_code(total_lb, 1)}:W",
        )
    )
    return rounds


def _build_single_elim(size: int, start_index: int = 1) -> list[Round]:
    """单败淘汰：只有胜者组，最后一轮即决赛（输一场就淘汰）。"""
    rounds_count = size.bit_length() - 1
    order = bracket_order(size)
    rounds: list[Round] = []
    index = start_index
    for r in range(1, rounds_count + 1):
        count = wb_match_count(size, r)
        for m in range(1, count + 1):
            if r == 1:
                src_a = f"seed:{order[(m - 1) * 2]}"
                src_b = f"seed:{order[(m - 1) * 2 + 1]}"
            else:
                src_a = f"{wb_code(r - 1, m * 2 - 1)}:W"
                src_b = f"{wb_code(r - 1, m * 2)}:W"
            is_final = r == rounds_count
            rounds.append(
                Round(
                    index=index,
                    code="GF" if is_final else wb_code(r, m),
                    stage="gf" if is_final else "wb",
                    bracket_round=r,
                    slot=m,
                    label=(
                        "决赛"
                        if is_final
                        else f"{wb_round_title(size, r)} · 第 {m} 场"
                    ),
                    src_a=src_a,
                    src_b=src_b,
                    # 输者直接淘汰：不写 winner_to（末轮）与 loser_to
                    winner_to="" if is_final else wb_code(r + 1, (m + 1) // 2),
                )
            )
            index += 1
    return rounds


def resolve_rounds(
    rounds: list[Round],
    seeds: list[str],
    teams: list[Team],
    *,
    seed_texts: dict[str, str] | None = None,
) -> list[Round]:
    """按已有结果推导淘汰赛双方（纯函数，可反复调用）。

    ``seeds`` 是晋级队伍的 ``team_id`` 顺序（1 号种子在前）。
    ``seed_texts`` 是每个种子席位的出处文案（``{"seed:3": "A 组第 2"}``，见
    :func:`seed_sources`）——给了就用它，没有就退回 :func:`source_text`。
    当前向遍历发现某场双方与已保存的不一致时，该场（以及其后所有对局）
    的比分与状态会被清空——上游结果改了，下游自然重来。
    """
    by_id = {t.id: t for t in teams}
    resolved = [r.model_copy(deep=True) for r in rounds]
    by_code = {r.code: r for r in resolved if r.code}

    def team_id_of(ref: str) -> str:
        if not ref:
            return ""
        if ref.startswith("seed:"):
            try:
                pos = int(ref[5:])
            except ValueError:
                return ""
            return seeds[pos - 1] if 1 <= pos <= len(seeds) else ""
        code, _, flag = ref.rpartition(":")
        source = by_code.get(code)
        if source is None or source.status != "done" or not source.winner:
            return ""
        win_side = source.side_by_key(source.winner)
        if win_side is None:
            return ""
        if flag == "W":
            return win_side.team_id
        # 「败者」只对 2 队对阵有唯一含义；淘汰赛恒为 2 队同场
        others = [side for side in source.sides if side is not win_side]
        return others[0].team_id if len(others) == 1 else ""

    for rnd in resolved:
        if rnd.stage in ("group", "league"):
            continue          # 小组赛由赛制生成、积分制常规局由换人接口维护
        decided = rnd.status == "done" or round_has_result(rnd)
        replaced = False
        for idx, ref in enumerate((rnd.src_a, rnd.src_b)):
            side: Side = rnd.sides[idx]
            tid = team_id_of(ref)
            team = by_id.get(tid) if tid else None
            players = list(team.player_ids) if team else []
            label = (team.short or team.label) if team else ""
            # 换了另一支队伍才算结构性变化；单纯的队员变动（替补）不推翻已打完的比赛
            if side.team_id != tid:
                replaced = True
            side.team_id = tid
            side.label = label
            side.source = (seed_texts or {}).get(ref) or source_text(ref)
            if not decided or replaced:
                side.player_ids = players
        if replaced and decided:
            reset_round_result(rnd)
    return resolved


def round_has_result(rnd: Round, scoring: object = metrics.INTEGER) -> bool:
    """本场是否已有任何录入痕迹（用于判断上游变化后是否需要作废）。

    这里问的是**「动过没有」**，不是「0 分算不算成绩」：数值型的 0 是合法读数，但它
    和旧库里从没打过的 0 无法区分，把它当成痕迹会让一批没开打的对局「看起来已经开打」，
    从而锁住对阵与赛程重建。真正的「已结算」由 ``status`` / ``winner`` / ``rank`` 表达。
    """
    sc = metrics.as_scoring(scoring)
    return bool(
        rnd.winner
        or rnd.status != "pending"
        or rnd.sets
        or any(
            sc.has_entered(side.score) or side.points or side.rank or side.forfeit
            for side in rnd.sides
        )
    )


def reset_round_result(rnd: Round) -> None:
    """清空一场比赛的比分 / 名次 / 弃权 / 用时（保留计划时间与直播设置）。"""
    rnd.status = "pending"
    rnd.winner = ""
    rnd.sets = []
    rnd.duration_minutes = 0
    for side in rnd.sides:
        side.score = 0
        side.points = 0
        side.rank = 0
        side.forfeit = False
    rnd.started_at = ""
    rnd.finished_at = ""
    rnd.locked = False


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build_tournament(
    teams: list[Team], rules: Rules
) -> tuple[list[Round], list[str], dict[str, Any]]:
    """生成完整赛程骨架（小组赛 + 淘汰赛）。返回 ``(对局, 提示, 概要)``。"""
    if len(teams) < 2:
        raise ValueError("至少需要 2 支队伍（即 4 名参与选手）才能生成赛程")

    size = resolve_size(len(teams), rules.knockout_size, strict=False)
    warnings: list[str] = []
    if rules.knockout_size and rules.knockout_size != size:
        warnings.append(
            f"原设定的 {rules.knockout_size} 强超出当前 {len(teams)} 支队伍的可行范围，已改为 {size} 强。"
        )
    rounds: list[Round] = []
    per_match = max(2, min(MAX_SIDES, rules.teams_per_match or 2))
    loser_bracket = bool(rules.loser_bracket)

    if len(teams) > 2:
        # **组队台里手工分好的组照用**：只有「一支都没分组」（自动组队刚出来的）
        # 才按小组数轮转分配。以前这里无条件重排一遍，等于把保存队伍时改的分组
        # 直接丢掉——「改了分组、生成赛程却没按新的来」就是这么来的。
        manual = all((team.group or "").strip() for team in teams)
        if not manual:
            group_count = rules.group_count or group_count_for(len(teams), per_match)
            group_count = max(1, min(group_count, len(teams)))
            assign_groups(teams, group_count)
        else:
            group_count = len({team.group for team in teams})
            log.info("按组队台的分组生成小组赛 | 队伍=%d | 分组=%d", len(teams), group_count)
        rounds = build_group_rounds(teams, per_match, 1)
        shape = "组 vs 组" if per_match == 2 else f"{per_match} 队同场"
        warnings.append(
            f"小组赛：{len(teams)} 支队分 {group_count} 组轮转，每场 {shape}，共 {len(rounds)} 场。"
        )
        small = [
            key
            for key in {t.group or "A" for t in teams}
            if sum(1 for t in teams if (t.group or "A") == key) < per_match
        ]
        if small:
            warnings.append(
                f"{'、'.join(sorted(small))} 组成员不足 {per_match} 支队，这些场次按实际队数进行。"
            )
        eliminated = len(teams) - size
        if eliminated > 0:
            warnings.append(f"小组赛按名次分排名，前 {size} 名晋级淘汰赛，其余 {eliminated} 支队淘汰。")
        else:
            warnings.append(f"全部 {size} 支队晋级淘汰赛，小组赛用于确定种子顺位。")

    knockout = build_knockout_rounds(size, len(rounds) + 1, loser_bracket=loser_bracket)
    rounds.extend(knockout)
    rounds_count = size.bit_length() - 1
    if size >= 4 and loser_bracket:
        warnings.append(
            f"淘汰赛：{size} 强双败，胜者组 {rounds_count} 轮、败者组 {2 * rounds_count - 2} 轮，"
            f"最后由胜者组冠军与败者组冠军争夺总冠军。"
        )
    elif size >= 4:
        warnings.append(
            f"淘汰赛：{size} 强单败（未开启败者组），输一场即淘汰，共 {rounds_count} 轮，"
            f"最后一轮为决赛。"
        )
    elif loser_bracket:
        warnings.append("队伍数不足 4 支：直接进行一场总决赛。")
    else:
        warnings.append("队伍数不足 4 支：直接进行一场决赛（单败）。")

    summary = {
        "size": size,
        "teams": len(teams),
        "teamsPerMatch": per_match,
        "loserBracket": loser_bracket,
        "groupMatches": len([r for r in rounds if r.stage == "group"]),
        "knockoutMatches": len([r for r in rounds if r.stage != "group"]),
        "total": len(rounds),
    }
    log.info(
        "赛程已生成 | 队伍=%d | 淘汰赛规模=%d | 同场=%d | 败者组=%s | 小组赛场次=%d | 淘汰赛场次=%d",
        len(teams),
        size,
        per_match,
        loser_bracket,
        summary["groupMatches"],
        summary["knockoutMatches"],
    )
    return rounds, warnings, summary


def size_from_rounds(rounds: list[Round]) -> int:
    """从已生成的对局反推淘汰赛规模（无需再传赛制参数）。"""
    wb = [r for r in rounds if r.stage == "wb"]
    if not wb:
        return 2 if any(r.code == "GF" for r in rounds) else 0
    first = min(r.bracket_round for r in wb)
    return sum(1 for r in wb if r.bracket_round == first) * 2


def advance_seeds(
    teams: list[Team], rounds: list[Round], scoring: object = metrics.INTEGER
) -> list[str]:
    """当前晋级淘汰赛的**种子顺序**（小组赛未结束时返回空列表）。

    * 还没开打：走 :func:`bracket_seeds`（交叉配对，同组首轮不相遇）；
    * **已经开打**：按小组赛总排名原样排（即当初生成这版对阵时用的老算法）——
      算法升级不许回溯改写已经打下来的比赛，否则一次普通写入就会把已录的淘汰赛成绩
      作废。想换算法就把淘汰赛场次重置（冻结随之解除，见 :func:`knockout_started`）；
    * 没有小组赛（队伍太少，直接淘汰赛）时按队伍顺序排。
    """
    size = size_from_rounds(rounds) or bracket_size(len(teams))
    if not any(r.stage == "group" for r in rounds):
        return [t.id for t in teams][:size]
    if not group_stage_done(rounds):
        return []
    tables = group_tables(teams, rounds, scoring)
    if knockout_started(rounds, scoring):
        return overall_ranking(tables, scoring)[:size]
    return bracket_seeds(tables, size, scoring)


def resolve_tournament(
    teams: list[Team], rounds: list[Round], scoring: object = metrics.INTEGER
) -> list[Round]:
    """按当前进程重算整份赛程（小组赛阵容保持原样，淘汰赛按结果推导）。"""
    seeds = advance_seeds(teams, rounds, scoring)
    # 席位文案跟着**种子顺序**走：交叉配对后「种子 3」不等于「小组赛第 3 名」，
    # 得按球队真正的出处写（A 组第 2 / C 组第 1）
    return resolve_rounds(
        rounds, seeds, teams, seed_texts=seed_sources(teams, rounds, scoring, seeds)
    )


def phase_of(rounds: list[Round]) -> str:
    if not rounds:
        return "idle"
    final = next((r for r in rounds if r.stage == "gf"), None)
    if final is not None and final.status == "done" and final.winner:
        return "finished"
    group = [r for r in rounds if r.stage == "group"]
    if group and not group_stage_done(rounds):
        return "group"
    return "knockout"


def champion_of(teams: list[Team], rounds: list[Round]) -> Team | None:
    final = next((r for r in rounds if r.stage == "gf"), None)
    if final is None or final.status != "done" or not final.winner:
        return None
    side = final.side_by_key(final.winner)
    if side is None:
        return None
    return next((t for t in teams if t.id == side.team_id), None)


def live_or_next(rounds: list[Round]) -> list[Round]:
    """当前正在进行的比赛；没有则给出最靠前的待开始比赛（用于首页「当前对阵」）。"""
    playing = [r for r in rounds if r.status == "live"]
    if playing:
        return sorted(playing, key=lambda r: r.index)
    for stage in ("group", "wb", "lb", "gf"):
        pending = [r for r in rounds if r.stage == stage and r.status == "pending"]
        ready = [r for r in pending if all(s.team_id for s in r.sides)]
        if ready:
            return sorted(ready, key=lambda r: r.index)
        if pending and stage == "group":
            return sorted(pending, key=lambda r: r.index)[:1]
    return []


def stage_progress(rounds: list[Round]) -> list[dict[str, Any]]:
    """各阶段进度，供前端渲染进度条。"""
    out: list[dict[str, Any]] = []
    for stage in ("league", "group", "wb", "lb", "gf"):
        bucket = [r for r in rounds if r.stage == stage]
        if not bucket:
            continue
        done = sum(1 for r in bucket if r.status == "done")
        out.append(
            {
                "stage": stage,
                "name": STAGE_NAMES[stage],
                "total": len(bucket),
                "done": done,
                "live": sum(1 for r in bucket if r.status == "live"),
                "percent": round(done / len(bucket) * 100, 1),
            }
        )
    return out


def bracket_view(teams: list[Team], rounds: list[Round]) -> dict[str, list[dict[str, Any]]]:
    """对阵图数据：按阶段与轮次分组，前端直接渲染。"""
    grouped: dict[str, dict[int, list[Round]]] = {"wb": defaultdict(list), "lb": defaultdict(list), "gf": defaultdict(list)}
    for rnd in rounds:
        if rnd.stage in grouped:
            grouped[rnd.stage][rnd.bracket_round].append(rnd)

    def dump(bucket: dict[int, list[Round]]) -> list[dict[str, Any]]:
        return [
            {
                "round": round_no,
                "title": items[0].label.split(" · ")[0] if items else f"第 {round_no} 轮",
                "matches": [r.model_dump(by_alias=True) for r in sorted(items, key=lambda r: r.slot)],
            }
            for round_no, items in sorted(bucket.items())
        ]

    return {stage: dump(bucket) for stage, bucket in grouped.items()}


def player_progress(teams: list[Team], rounds: list[Round]) -> dict[str, dict[str, Any]]:
    """每位选手的赛况（所属队伍、出场、胜负），供选手页展示，替代原来的积分榜。"""
    out: dict[str, dict[str, Any]] = {}
    for team in teams:
        for pid in team.player_ids:
            out[pid] = {
                "playerId": pid,
                "teamId": team.id,
                "teamName": team.name,
                "group": team.group,
                "played": 0,
                "win": 0,
                "lose": 0,
                "forms": [],
            }
    for rnd in rounds:
        if rnd.status != "done" or not rnd.winner:
            continue
        for side in rnd.sides:
            won = rnd.key_of(side) == rnd.winner
            for pid in side.player_ids:
                row = out.get(pid)
                if row is None:
                    continue
                row["played"] += 1
                row["win" if won else "lose"] += 1
                row["forms"].append("W" if won else "L")
    return out
