"""计分口径：**一场比赛的分数是什么、怎么比、怎么显示** —— 全站只有这一处定义。

三件事各自独立，任何赛制都只是它们的组合：

============  ==========================================  ==============================
计分类型      说明                                        存储单位（一律 int）
============  ==========================================  ==============================
``integer``   自然数（排球 25 分、篮球 98 分…）            原值
``decimal``   小数（评委打分 8.75、体测 12.5…）            **千分之一**（8.75 → 8750）
``time``      时间（跑酷 1:23.456、速通…）                 **毫秒**
============  ==========================================  ==============================

* **计分标签**：纯展示用（得分 / 评分 / 用时 / 自定义），例如类型选「时间」+ 标签选
  「用时」，页面就写「用时：1:23.456」。空标签按类型给默认值。
* **判断标准**：``high`` 数值高胜 / ``low`` 数值低胜。**方向只由它决定**——类型只决定
  怎么解析与怎么显示，不再隐含方向（时间 + 高胜 是合法组合，虽然少见）。

---- 为什么一律存整数 ----

小数与时间都不存浮点：它们要参与求和、并列比较与数据库存取，浮点的相等判断和累计误差
会让「并列」变得不可靠（两个 12.345 相加的结果可能不相等）。小数统一放大成千分之一、
时间统一成毫秒，于是「排序、求和、判并列」全是在整数上做。

**无损映射**（老数据升级）：历史数据只有 ``metric`` 一个字段，``score`` = 自然数 +
得分 + 数值高胜，``time`` = 时间 + 用时 + 数值低胜——数值一个字节都不动，只是把
「含义」拆成了三个字段（见 :func:`resolve`）。

---- 两条关键约定 ----

**① 数值型的 ``0`` 是合法读数，时间型的 ``0`` 才是「没有成绩」。**

评委真的会打 0 分，速通不可能 0 毫秒跑完。所以「没有成绩」不能再用「0」一个值兼职：

* 时间型：``0`` = 没有成绩（合法值必然 > 0，这个位置空着，老数据也正是这么存的）；
* 自然数 / 小数：``0`` = 0 分（合法），**没有成绩写成 ``MISSING``（``-1``）**——
  成绩本来就非负，负数只有哨兵这一个来源，不会和真实数据撞车。

没有成绩的一方在任何口径下都排最后；时间型里如果让 0 参与比较，「0 秒」就会被当成
最快的人——一个静默的、灾难性的错误。

**② 「有没有成绩」（``has_result``）与「有没有录入过」（``has_entered``）是两件事。**

数值型的 0 既是合法读数，又和旧库里「从没打过」的 0 长得一模一样。判定胜负、排名、
完成场次用前者；「这场比赛动过没有」（要不要锁住对阵、要不要作废上游）用后者。

``points``（小分 / 累计成绩 / 罚时）一直把 0 当合法值，没有这条区分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# 计分类型
# --------------------------------------------------------------------------- #
INTEGER = "integer"
DECIMAL = "decimal"
TIME = "time"

VALUE_TYPES = (INTEGER, DECIMAL, TIME)

TYPE_LABELS = {
    INTEGER: "自然数",
    DECIMAL: "小数",
    TIME: "时间",
}

#: 小数放大到千分之一存储（8.75 → 8750），与时间的毫秒同一套思路
DECIMAL_SCALE = 3
DECIMAL_UNIT = 10**DECIMAL_SCALE

#: 「没有成绩」的哨兵：数值型的 0 是合法读数，只好用一个不可能出现的负数来兼职。
#: 时间型不用它——时间型的合法值必然 > 0，``0`` 本身就是「没有成绩」（老数据也这么存）。
MISSING = -1

# --------------------------------------------------------------------------- #
# 判断标准
# --------------------------------------------------------------------------- #
HIGH = "high"      # 数值高胜
LOW = "low"        # 数值低胜

BETTERS = (HIGH, LOW)

BETTER_LABELS = {
    HIGH: "数值高胜",
    LOW: "数值低胜",
}

# --------------------------------------------------------------------------- #
# 计分标签（纯展示；预设这三个，填别的就是自定义）
# --------------------------------------------------------------------------- #
LABEL_PRESETS = ("得分", "评分", "用时")
DEFAULT_LABELS = {
    INTEGER: "得分",
    DECIMAL: "评分",
    TIME: "用时",
}
LABEL_MAX = 12

# --------------------------------------------------------------------------- #
# 旧口径（只用于读老数据）：metric = score / time
# --------------------------------------------------------------------------- #
LEGACY_SCORE = "score"
LEGACY_TIME = "time"
LEGACY_METRICS = (LEGACY_SCORE, LEGACY_TIME)


# --------------------------------------------------------------------------- #
# 规整
# --------------------------------------------------------------------------- #
def norm_type(value: object) -> str:
    """规整计分类型；不认识的一律回落到自然数。"""
    raw = str(value or "").strip().lower()
    return raw if raw in VALUE_TYPES else INTEGER


def norm_better(value: object) -> str:
    """规整判断标准；不认识的回落到数值高胜。"""
    raw = str(value or "").strip().lower()
    return raw if raw in BETTERS else HIGH


def norm_metric(value: object) -> str:
    """规整**旧口径**比法名；不认识的一律回落到计分制。"""
    raw = str(value or "").strip().lower()
    return raw if raw in LEGACY_METRICS else LEGACY_SCORE


def legacy_metric(value_type: object) -> str:
    """计分类型 → 旧口径比法名（写库时保持两边一致，老版本读得回来）。"""
    return LEGACY_TIME if norm_type(value_type) == TIME else LEGACY_SCORE


def clean_label(text: object, value_type: object = INTEGER) -> str:
    """规整计分标签：去空白、限长；空 = 按类型给默认（得分 / 评分 / 用时）。"""
    raw = str(text or "").strip()
    if not raw:
        return DEFAULT_LABELS[norm_type(value_type)]
    return raw[:LABEL_MAX]


@dataclass(frozen=True, slots=True)
class Scoring:
    """一套完整的计分口径：类型 + 标签 + 判断标准。

    全站判定胜负、排名名次、显示与解析**都只经过它**——把这三个字段单独传下去，
    迟早会有某一处忘了跟着改（历史教训：方向曾经同时由「比法」和「局分计数」两条
    路径决定，用时制下 2:1 会被判成输）。
    """

    value_type: str = INTEGER
    label: str = ""
    better: str = HIGH

    # ---- 组合：从任意来源（新字段 / 旧 metric / 空）解析出一套口径 ----
    @classmethod
    def resolve(
        cls,
        *,
        value_type: object = "",
        label: object = "",
        better: object = "",
        metric: object = "",
    ) -> Scoring:
        """把「新三字段」与「旧 metric」合成一套口径。

        三档优先级，**空与垃圾分开处理**：

        * 字段**有值且认识** → 用它；
        * 字段**为空**（老数据只有 ``metric``）→ 按旧 ``metric`` 推：
          ``time`` → 时间 + 数值低胜，其余 → 自然数 + 数值高胜，
          因此老数据升级后数值与判定一字不变；
        * 字段**有值但不认识**（手改 API 的垃圾）→ 回落到类型默认，
          而不是拿旧 ``metric`` 兜底（否则「改成整数」这种请求会被旧值顶回来）。
        """
        raw_kind = str(value_type or "").strip().lower()
        raw_better = str(better or "").strip().lower()
        if raw_kind in VALUE_TYPES:
            kind = raw_kind
        elif raw_kind:
            kind = INTEGER
        else:
            kind = TIME if norm_metric(metric) == LEGACY_TIME else INTEGER
        if raw_better in BETTERS:
            direction = raw_better
        elif raw_better:
            direction = HIGH
        else:
            # 没显式指定方向时跟着类型走：时间天然是「越快越好」
            direction = LOW if kind == TIME else HIGH
        return cls(value_type=kind, label=clean_label(label, kind), better=direction)

    # ---- 描述 ----
    @property
    def type_label(self) -> str:
        """计分类型的中文名（自然数 / 小数 / 时间）。"""
        return TYPE_LABELS[self.value_type]

    @property
    def label_text(self) -> str:
        """计分标签（得分 / 评分 / 用时 / 自定义）。"""
        return clean_label(self.label, self.value_type)

    @property
    def better_label(self) -> str:
        """判断标准的中文名（数值高胜 / 数值低胜）。"""
        return BETTER_LABELS[self.better]

    @property
    def low_wins(self) -> bool:
        """数值越小越好吗。"""
        return self.better == LOW

    @property
    def time_based(self) -> bool:
        """是不是时间型（只看显示与解析，不表示方向）。"""
        return self.value_type == TIME

    @property
    def zero_is_result(self) -> bool:
        """``0`` 算不算一个合法成绩：数值型算，时间型不算（0 毫秒跑不完）。"""
        return self.value_type != TIME

    def missing_value(self) -> int:
        """本口径下「没有成绩」的写法：时间型是 ``0``，其余是 ``MISSING``。"""
        return 0 if self.value_type == TIME else MISSING

    def dump(self) -> dict[str, str]:
        """给前端用的三个字段 + 中文名（一次给全，免得前端再拼）。"""
        return {
            "valueType": self.value_type,
            "valueLabel": self.label_text,
            "better": self.better,
            "typeLabel": self.type_label,
            "betterLabel": self.better_label,
        }

    # ---- 数值 ----
    def has_result(self, value: Any) -> bool:
        """这个成绩算「有效」吗（判定胜负、排名、完成场次都问它）。

        * ``MISSING`` / 负数 / 空 → 没有成绩（未完赛、退赛、未填）；
        * ``0`` → 数值型是合法读数（0 分），时间型是「没有成绩」。
        """
        if value is None:
            return False
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        if number < 0:
            return False
        return number > 0 or self.zero_is_result

    def has_entered(self, value: Any) -> bool:
        """有没有**明确录入过**（不是「没有成绩」，也不是数值型的 0）。

        只用来回答「这场比赛动过没有」：数值型的 0 是合法读数，但它和旧库里
        从没打过的 0 长得一模一样，所以不能算「录入痕迹」——否则一批没开打的
        对局会被当成「已经开打」，白白锁住对阵与赛程重建。
        """
        return self.has_result(value) and int(value or 0) != 0

    def has_total(self, value: Any, points: Any = 0, *, has_rounds: bool = False) -> bool:
        """本场是否留下了可用成绩（「完成场次」与「谁都没填」都问它）。

        填了轮次就看**各轮合计**（轮数是计数，输了个 0:2 不等于没打）；
        没填轮次就看本场成绩本身——数值型的 0 是合法读数。
        """
        if has_rounds:
            return int(points or 0) > 0
        return self.has_result(value)

    def sort_key(self, value: Any) -> tuple[int, int]:
        """主成绩的排序键（**小者优先**）。

        返回 ``(组号, 值)``：没有成绩的一律进第 1 组（永远排在有成绩的后面），
        有成绩的按判断标准排——数值高胜取负数（分高者小），数值低胜原样（小者小）。
        """
        number = int(value or 0)
        if not self.has_result(number):
            return (1, 0)
        return (0, -number if not self.low_wins else number)

    def spare_key(self, points: Any) -> int:
        """次要值的排序键（小者优先）：小分 / 累计成绩 / 罚时。

        与 :meth:`sort_key` 不同，这里 **0 是合法值**——时间型里没有罚时最好，
        数值高胜里没有小分也确实是最后。
        """
        number = int(points or 0)
        return number if self.low_wins else -number

    def order_key(self, value: Any, points: Any = 0) -> tuple[int, int, int]:
        """一套完整的名次排序键（小者在前）。

        自然数 + 数值高胜与历史上的 ``(-score, -points)`` **完全等价**（含并列），
        所以老数据的行为一字不变。
        """
        return (*self.sort_key(value), self.spare_key(points))

    def judge_key(
        self, value: Any, points: Any = 0, *, counted: bool = False
    ) -> tuple[Any, ...]:
        """判定名次用的排序键（小者在前）。

        ``counted=True`` 表示 ``value`` 是**赢的轮数**而不是成绩本身——这只会出现在
        「填了轮次的两人对局」里：轮数是计数，**任何口径下都是多者胜**；
        真正的成绩在 ``points`` 里，仍按判断标准比。

        这个区分是必须的：时间型里如果把「2 : 1」当成时间比，赢家会被判输
        （1 < 2），而且看起来毫无破绽。
        """
        if counted:
            return (-int(value or 0), self.spare_key(points))
        return self.order_key(value, points)

    def round_total(self, value: Any, points: Any, has_rounds: bool) -> int:
        """一场比赛的「总成绩」：填了轮次就是各轮合计（``points``），否则是 ``value``。

        这条约定前后端一致（见录分弹窗的自动推导），小组赛表与积分榜的累计项都按它算
        ——否则「填了轮次」与「直接填成绩」两种录入方式会得到两套不可比的累计值。
        """
        return int(points or 0) if has_rounds else int(value or 0)

    # ---- 文本 ----
    def format(self, value: Any) -> str:
        """按计分类型把存储值显示成人看的样子；没有成绩一律 ``—``。"""
        if not self.has_result(value):
            return "—"
        number = int(value)
        if self.value_type == TIME:
            return format_time(number)
        if self.value_type == DECIMAL:
            return format_decimal(number)
        return str(number)

    def parse(self, text: Any) -> int:
        """按计分类型把用户输入解析成存储值（时间 → 毫秒，小数 → 千分之一）。

        空与「退赛」这类写法一律解析成 :meth:`missing_value`——**不要**当成 0：
        数值型的 0 是一个合法读数，把「没填」记成「0 分」会让一次漏填变成成绩。

        解析不了抛 :class:`ValueError`，由调用方决定怎么提示。
        """
        raw = str(text if text is not None else "").strip()
        if not raw or raw.lower() in DNF_WORDS:
            return self.missing_value()
        if self.value_type == TIME:
            return parse_time(raw)
        if self.value_type == DECIMAL:
            return parse_decimal(raw)
        return parse_integer(raw)

    def format_score(self, value: Any, *, counted: bool) -> str:
        """一方的 ``score`` 怎么显示。

        ``counted=True`` 表示这场比赛**填了轮次**，于是 ``score`` 是「赢的轮数」——
        那是计数，任何类型下都按整数显示（时间型里把 2 轮写成 ``2`` 而不是去格式化）。
        """
        number = int(value or 0)
        if counted:
            return "—" if number < 0 else str(number)
        return self.format(number)


# --------------------------------------------------------------------------- #
# 数值 ↔ 文本
# --------------------------------------------------------------------------- #
# 录入容错：``1:23.456`` / ``1'23"45`` / ``83.456`` / ``83`` 都认。
# 全是毫秒的十进制表示，所以统一按分隔符切开再拼回去。
_TIME_SEPARATORS = (":", "：", "'", "’", "′")
#: 表示「没有成绩」的写法（退赛 / 未完赛）
DNF_WORDS = frozenset({"dnf", "dns", "dnq", "退赛", "未完赛", "未完成", "-", "—", "/", "无"})


def parse_time(text: object) -> int:
    """把用时文本解析成**毫秒**；空 / 退赛 = 没有成绩（``0``，时间型的哨兵就是 0）。"""
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
    seconds = _decimal(parts[-1], raw, "用时")
    total_ms = round(seconds * 1000)
    for index, item in enumerate(reversed(parts[:-1]), start=1):
        total_ms += _whole(item, raw, "用时") * (60**index) * 1000
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


def hours_minutes_seconds(value: object) -> tuple[int, int, float]:
    """毫秒 → ``(时, 分, 秒)``（秒带小数），供「时分秒」三个输入框回填。"""
    ms = max(0, int(value or 0))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return hours, minutes, round(rest / 1000, 3)


def parse_hms(hours: object, minutes: object, seconds: object) -> int:
    """「时 / 分 / 秒」三个输入框 → 毫秒（全空 = 没有成绩；时间型的哨兵就是 0）。"""
    def _num(raw: object, label: str) -> float:
        text = str(raw or "").strip()
        if not text:
            return 0.0
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"「{text}」不是合法的{label}") from exc
        if value < 0:
            raise ValueError(f"{label}不能是负数")
        return value

    total = _num(hours, "小时") * 3600 + _num(minutes, "分钟") * 60 + _num(seconds, "秒")
    return max(0, round(total * 1000))


def parse_decimal(text: object) -> int:
    """小数文本 → **千分之一**；空 / 退赛 = 没有成绩（``MISSING``）。"""
    raw = str(text or "").strip()
    if not raw:
        return MISSING
    if raw.lower() in DNF_WORDS:
        return MISSING
    value = _decimal(raw, raw, "小数")
    if value < 0:
        raise ValueError(f"「{raw}」不能是负数")
    return max(0, round(value * DECIMAL_UNIT))


def format_decimal(value: object) -> str:
    """千分之一 → 文本：末尾的 0 一律去掉（``8.750`` → ``8.75``，``8000`` → ``8``）。

    ``0`` 显示成 ``0``：数值型里「零分」是一个合法读数；负数（``MISSING``）才是
    「没有成绩」，显示 ``—``。
    """
    number = int(value or 0)
    if number < 0:
        return "—"
    if number == 0:
        return "0"
    text = f"{number / DECIMAL_UNIT:.{DECIMAL_SCALE}f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_integer(text: object) -> int:
    """自然数文本 → 整数；空 / 退赛 = 没有成绩（``MISSING``）。"""
    raw = str(text or "").strip()
    if not raw:
        return MISSING
    if raw.lower() in DNF_WORDS:
        return MISSING
    value = _decimal(raw, raw, "分数")
    if value < 0:
        raise ValueError(f"「{raw}」不能是负数")
    return max(0, int(value))


def format_value(value: object, scoring: Scoring | object = INTEGER) -> str:
    """按口径显示一个数值（``scoring`` 可以只传计分类型名，便于调用方少写几行）。"""
    return as_scoring(scoring).format(value)


def parse_value(text: object, scoring: Scoring | object = INTEGER) -> int:
    """按口径解析一个数值（``scoring`` 可以只传计分类型名）。"""
    return as_scoring(scoring).parse(text)


def as_scoring(value: Scoring | object) -> Scoring:
    """把「计分类型名 / Scoring / 空」统一成 :class:`Scoring`。

    允许直接传 ``"time"`` 这类类型名，是为了调用方（尤其测试）不用每次都
    构造一个完整口径；传 :class:`Scoring` 时原样返回。
    """
    if isinstance(value, Scoring):
        return value
    return Scoring.resolve(value_type=value)


def _decimal(text: str, raw: str, what: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"「{raw}」不是合法的{what}") from exc


def _whole(text: str, raw: str, what: str) -> int:
    try:
        return int(float(text))
    except ValueError as exc:
        raise ValueError(f"「{raw}」不是合法的{what}（可写 1:23.456 或 83.45）") from exc
