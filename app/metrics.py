"""比法（metric）：**哪种数值更好** —— 全站只有这一处定义。

赛事项目千差万别，但结算方式只有两类：

* ``score``（**计分制**）：分数高者胜。排球、篮球、卡牌、电竞、评委打分……
* ``time``（**用时制**）：用时短者胜。赛车、跑酷、速通、越野……

把「方向」这一个差别抽出来之后，胜负判定、名次、名次分、淘汰晋级、
积分榜排序全都**不需要**分两套：它们本来就是「谁的名次靠前」，
而不是「谁的分数大」。

能不能比出一个名次，才是赛制真正关心的东西。

---- 数字怎么存 ----

用时制下 ``score`` / ``points`` / ``sets`` 里的数字一律是**毫秒**（整数）。
不用浮点是因为它们要参与求和、并列比较与数据库存取——浮点的相等判断
和累计误差会让「并列」变得不可靠（两个 12.345 秒相加的结果可能不相等）。
界面只负责 ``1:23.456`` ↔ 毫秒 的互转（见 :func:`parse_time` / :func:`format_time`）。

---- 一条关键约定 ----

``score <= 0`` 视为「**没有成绩**」（未完赛 / 退赛 / 未填 / DNF），在**两种比法下
都排最后**。否则用时制里「0 秒」会被当成最快的人——一个静默的、灾难性的错误。

``points``（计分制的小分 / 用时制的罚时）没有这条约定：0 是合法值
（用时制里「没有罚时」恰恰是最好的）。
"""

from __future__ import annotations

from typing import Any

SCORE = "score"
TIME = "time"

METRICS = (SCORE, TIME)

#: 比法的中文名（界面与规则文案共用，免得两处各写一遍）
LABELS = {
    SCORE: "计分制",
    TIME: "用时制",
}

#: 一句话说清方向（给管理员看的）
DESCRIPTIONS = {
    SCORE: "分数高的一方获胜",
    TIME: "用时短的一方获胜",
}


def norm(value: object) -> str:
    """规整比法取值；不认识的一律回落到计分制（老数据没有这个字段）。"""
    raw = str(value or "").strip().lower()
    return raw if raw in METRICS else SCORE


def lower_is_better(metric: object) -> bool:
    """该比法下「数值越小越好」吗。"""
    return norm(metric) == TIME


def has_result(score: int) -> bool:
    """这个成绩算「有效」吗（``<= 0`` = 没成绩 / 未完赛）。"""
    return int(score) > 0


def value_key(score: int, metric: object) -> tuple[int, int]:
    """主成绩的排序键（**小者优先**）。

    返回 ``(组号, 值)``：没有成绩的一律进第 1 组（永远排在有成绩的后面），
    有成绩的按该比法的方向排——计分制取负数（分高者小），用时制原样（快者小）。
    """
    score = int(score)
    if not has_result(score):
        return (1, 0)
    return (0, -score if not lower_is_better(metric) else score)


def spare_key(points: int, metric: object) -> int:
    """次要值的排序键（小者优先）：计分制的小分、用时制的罚时。

    与 :func:`value_key` 不同，这里 **0 是合法值**——用时制里没有罚时最好，
    计分制里没有小分也确实是最后。
    """
    points = int(points)
    return points if lower_is_better(metric) else -points


def order_key(score: int, points: int, metric: object) -> tuple[int, int, int]:
    """一套完整的名次排序键（小者在前）。

    计分制下与历史上的 ``(-score, -points)`` **完全等价**（含并列），
    所以老数据的行为一字不变。
    """
    return (*value_key(score, metric), spare_key(points, metric))


def judge_key(score: int, points: int, metric: object, *, counted: bool = False) -> tuple[Any, ...]:
    """判定名次用的排序键（小者在前）。

    ``counted=True`` 表示 ``score`` 是**局分**（赢的局数）而不是成绩本身——
    这只会出现在「填了各局小分的双人对局」里：局分是计数，**任何比法下都是多者胜**；
    真正的成绩在 ``points`` 里，仍按比法比。

    这个区分是必须的：用时制里如果把局分「2 : 1」当成时间比，赢家会被判输
    （1 < 2），而且看起来毫无破绽。
    """
    if counted:
        return (-int(score), spare_key(points, metric))
    return order_key(score, points, metric)


def round_total(score: int, points: int, has_sets: bool) -> int:
    """一场比赛的「总成绩」：填了各局就是各局合计（``points``），否则是 ``score``。

    这条约定前后端一致（见录分弹窗的自动推导），小组赛表与积分榜的
    累计项都按它算——否则「填了各局」与「直接填成绩」两种录入方式
    会得到两套不可比的累计值。
    """
    return int(points) if has_sets else int(score)


# --------------------------------------------------------------------------- #
# 用时制的数值 ↔ 文本
# --------------------------------------------------------------------------- #
# 录入容错：``1:23.456`` / ``1'23"45`` / ``83.456`` / ``83`` 都认。
# 全是毫秒的十进制表示，所以统一按分隔符切开再拼回去。
_TIME_SEPARATORS = (":", "：", "'", "’", "′")
#: 表示「没有成绩」的写法（退赛 / 未完赛）
DNF_WORDS = frozenset({"dnf", "dns", "dnq", "退赛", "未完赛", "未完成", "-", "—", "/", "无"})


def parse_time(text: object) -> int:
    """把用时文本解析成**毫秒**；空 = 没有成绩（``0``）。

    不能解析时抛 :class:`ValueError`，由调用方决定怎么提示——
    静默当成 0 会让人以为「记上了」，实际记成了未完赛。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0
    if raw.lower() in DNF_WORDS:
        return 0

    # 去掉秒与毫秒之间可能出现的引号（``1'23"456``）
    body = raw.replace('"', ".").replace("”", ".").replace("″", ".")
    for sep in _TIME_SEPARATORS:
        body = body.replace(sep, ":")
    parts = [item for item in body.split(":") if item != ""]
    if not parts:
        return 0

    # 最后一段是「秒.毫秒」，前面的段是 时 / 分（从右往左进位）
    seconds = _decimal(parts[-1], raw)
    total_ms = round(seconds * 1000)
    for index, item in enumerate(reversed(parts[:-1]), start=1):
        total_ms += _whole(item, raw) * (60**index) * 1000
    return max(0, total_ms)


def format_time(value: object) -> str:
    """毫秒 → 文本：不满一分钟是 ``83.45``，超过则 ``1:23.456``。

    小数位「够用就少」：整十毫秒显示两位（``1:23.45``），否则三位（``1:23.456``）。
    这样用 0.01 秒精度的项目看着干净，用 0.001 秒的也不会被砍掉。
    """
    ms = int(value or 0)
    if ms <= 0:
        return "—"
    dec = 2 if ms % 10 == 0 else 3
    unit = 1000
    hours, rest = divmod(ms, 3600 * unit)
    minutes, rest = divmod(rest, 60 * unit)
    secs, millis = divmod(rest, unit)
    fraction = f"{millis:03d}"[:dec]
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{fraction}"
    if minutes:
        return f"{minutes}:{secs:02d}.{fraction}"
    return f"{secs}.{fraction}"


def format_value(value: object, metric: object) -> str:
    """按比法把数字显示成人看的样子（计分制就是数字，用时制是时间）。"""
    return format_time(value) if lower_is_better(metric) else str(int(value or 0))


def parse_value(text: object, metric: object) -> int:
    """按比法把用户输入的文本解析成存储值（用时制 → 毫秒）。"""
    if lower_is_better(metric):
        return parse_time(text)
    raw = str(text or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError as exc:
        raise ValueError(f"「{raw}」不是合法的分数") from exc


def _decimal(text: str, raw: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"「{raw}」不是合法的用时（可写 1:23.456 或 83.45）") from exc


def _whole(text: str, raw: str) -> int:
    try:
        return int(float(text))
    except ValueError as exc:
        raise ValueError(f"「{raw}」不是合法的用时（可写 1:23.456 或 83.45）") from exc
