"""数据模型。

约定：Python 内部字段使用 snake_case，对外 JSON / Web API 统一 camelCase，
由 pydantic 的 alias 生成器自动完成转换（序列化时必须 ``by_alias=True``）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

# 选手对外可见的字段：只有名字与头像等展示信息（UUID / QQ / 推流流名不下发）
PUBLIC_PLAYER_FIELDS = ("id", "name", "avatar", "tag", "substitute", "active", "member_uid")

# 成员权限：成员 < 赛事管理员 < 服务器管理员。
# 服务器管理员全局有且只有一个（程序启动时自检，见 store.ensure_server_admin）。
MemberPermission = Literal["member", "event_admin", "server_admin"]
# 直播封禁范围：global = 服务器管理员签发的全局封禁；event = 赛事管理员签发、仅作用于自己那届
LiveBanScope = Literal["global", "event"]

# 成员对外可见（含访客）字段：展示信息与推流 ID（用于频道展示），不含任何凭据
PUBLIC_MEMBER_FIELDS = (
    "uid",
    "name",
    "avatar",
    "game_uuid",
    "stream_id",
    "room_title",
    "permission",
    "active",
)

# 一场比赛最多 4 支队伍同场（组 vs 组 vs 组 vs 组）
SideKey = Literal["A", "B", "C", "D"]
MAX_SIDES = 4
# 每场同场竞技的队伍数：2 = 组vs组，3 = 组vs组vs组，4 = 四队同场
MIN_TEAMS_PER_MATCH = 2
# 比赛简介的字数上限（按字符计，中英文同规）：界面用 maxlength 挡，服务端再截一次兜底
MAX_BRIEF_CHARS = 30
RoundStatus = Literal["pending", "live", "done"]
WinnerCode = Literal["", "A", "B", "DRAW"]
# 观众的观看线路：webrtc = 8889（UDP，延迟最低）；hls = 8888（TCP，抗抖动）
StreamMode = Literal["auto", "webrtc", "hls"]
# league=积分制常规局；group=小组赛；wb=胜者组；lb=败者组；gf=总决赛（胜者组冠军 vs 败者组冠军）
Stage = Literal["league", "group", "wb", "lb", "gf"]
# 赛事赛制：league=积分制（均分排名）；tournament=锦标赛制（固定队伍 + 双败淘汰）
MatchFormat = Literal["league", "tournament"]


class NTEModel(BaseModel):
    """所有模型基类：camelCase 别名 + 忽略未知字段。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


# --------------------------------------------------------------------------- #
# 选手 / 队伍
# --------------------------------------------------------------------------- #
class Player(NTEModel):
    """参赛选手。``qq`` 用于拉取头像，``substitute`` 标记替补。"""

    id: str = ""
    name: str = ""
    uuid: str = ""            # 游戏内 UUID（手动录入，用于检索；不自动生成）
    qq: str = ""
    avatar: str = ""          # 非空则覆盖 QQ 头像（可为本地上传地址）
    tag: str = ""             # 选手编号，如 NTE-07
    stream_key: str = ""      # 推流流名（如 stream / live），用于自动派生推流与播放地址
    note: str = ""
    substitute: bool = False
    active: bool = True
    member_uid: str = ""      # 关联的全局成员（选手就是成员）；空 = 独立选手

    @field_validator("qq")
    @classmethod
    def _clean_qq(cls, value: str) -> str:
        return "".join(ch for ch in value if ch.isdigit())

    @field_validator("stream_key")
    @classmethod
    def _clean_stream_key(cls, value: str) -> str:
        """只保留 URL 路径安全字符，避免拼接出越界地址。"""
        return "".join(ch for ch in value.strip().strip("/") if ch.isalnum() or ch in "-_")

    @property
    def display_name(self) -> str:
        return self.name or self.tag or self.id

    @property
    def has_avatar_source(self) -> bool:
        """是否有可用的头像来源（自定义地址，或合法的 QQ 号）。"""
        if self.avatar:
            return True
        return self.qq.isdigit() and 4 <= len(self.qq) <= 12

    def public(self) -> dict[str, Any]:
        """对外**脱敏**结构：只有名字、头像与必要的展示标记。

        UUID / QQ / 推流流名属于隐私与推流凭据，绝不下发用户端；
        管理端需要时通过 ``GET /api/private``（需登录）单独获取。
        头像改用 ``/api/avatar/p/<选手 ID>`` 取，客户端因此看不到 QQ 号。
        """
        data: dict[str, Any] = self.model_dump(by_alias=True, include=list(PUBLIC_PLAYER_FIELDS))
        data["hasStream"] = bool(self.stream_key)
        data["hasAvatar"] = self.has_avatar_source
        return data


class Team(NTEModel):
    """固定队伍：组队后人数固定、全程不换人。"""

    id: str
    name: str = ""            # 队名，默认由成员名拼成「甲 & 乙」
    short: str = ""           # 缩写，赛程与对阵图里使用
    color: str = ""
    player_ids: list[str] = Field(default_factory=list)
    group: str = ""           # 小组赛分组（A/B/C/D…），空表示尚未分组

    @property
    def label(self) -> str:
        return self.name or self.short or self.id


class Channel(NTEModel):
    """成员直播间（日常 / 非比赛）。

    与 :class:`Player` 的区别：**跟赛事届次无关**，是常驻的「群友自播」位，
    存在同一个库里但独立于任何一届赛事；没有比赛时也能一直开着播。

    推流标识同样是他自己的唯一流名（``stream_key`` / ``streamKey``），
    观众看到的是这个流名的播放地址；推流地址只在管理端出现。
    """

    id: str = ""
    name: str = ""            # 主播名 / 频道名
    qq: str = ""              # 可选，仅用于取头像
    avatar: str = ""          # 非空则覆盖 QQ 头像（可为本地上传地址）
    stream_key: str = ""      # 推流流名（全局唯一，与选手流名也不得重复）
    title: str = ""           # 直播间标题（一句话）
    server: str = ""          # 游戏区服（自由填写，如「国服 / 国际服」）
    role: str = ""            # 常驻角色 / 称号（自由填写，展示用）
    description: str = ""     # 简介 / 内容说明
    tags: list[str] = Field(default_factory=list)
    link: str = ""            # 外部跳转（个人主页 / 其它平台）
    color: str = ""
    sort: int = 0             # 排序（小的在前）
    active: bool = True       # 停用后不出现在用户端
    featured: bool = False    # 置顶推荐

    @field_validator("qq")
    @classmethod
    def _clean_qq(cls, value: str) -> str:
        return "".join(ch for ch in value if ch.isdigit())

    @field_validator("stream_key")
    @classmethod
    def _clean_stream_key(cls, value: str) -> str:
        """只保留 URL 路径安全字符，避免拼接出越界地址。"""
        return "".join(ch for ch in value.strip().strip("/") if ch.isalnum() or ch in "-_")

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value or []:
            clean = str(item or "").strip()
            if clean and clean not in out:
                out.append(clean)
        return out[:8]

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def has_avatar_source(self) -> bool:
        """是否有可用的头像来源（自定义地址，或合法的 QQ 号）。"""
        if self.avatar:
            return True
        return self.qq.isdigit() and 4 <= len(self.qq) <= 12


# --------------------------------------------------------------------------- #
# 成员（全局）与直播封禁（全局）
# --------------------------------------------------------------------------- #
class Member(NTEModel):
    """服务器成员（全局，跨届共享）。

    成员是本站的「账号」：``uid`` 是网站用户 UUID（全局唯一，创建时自动生成）。

    凭据一律**加盐**存储，明文永不落库、也永不回传（只在生成 / 轮换那一次显示）：

    * ``key_hash`` / ``bearer_hash``：加盐哈希（``hmac_sha256$盐$摘要``，见 ``auth.hash_secret``）；
    * ``key_sha256`` / ``bearer_sha256``：**历史无盐格式**，只为老库能继续校验而保留，
      轮换一次即自动升级（接口会把「还有几条待轮换」报给管理端）。

    ``stream_id`` 是这位成员的推流 ID（也是他在媒体服务器上的推流路径），
    与令牌一起构成推流凭据；只有两者同时正确才允许推流（见 ``live`` 模块）。
    """

    uid: str = ""             # 网站用户 UUID（全局唯一，自动生成；不可修改）
    name: str = ""
    qq: str = ""
    avatar: str = ""
    game_uuid: str = ""       # 游戏内 UUID
    stream_id: str = ""       # 推流 ID（全局唯一）
    room_title: str = ""      # 直播间名字（成员可自行修改）
    note: str = ""
    permission: MemberPermission = "member"
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    key_hash: str = ""         # 加盐哈希（新格式，推荐）
    bearer_hash: str = ""      # 加盐哈希（新格式，推荐）
    key_sha256: str = ""       # 历史无盐格式（兼容旧库）
    bearer_sha256: str = ""    # 历史无盐格式（兼容旧库）

    @property
    def key_stored(self) -> str:
        """登录密钥的存储值（优先加盐的新格式）。"""
        return self.key_hash or self.key_sha256

    @property
    def bearer_stored(self) -> str:
        """Bearer 令牌的存储值（优先加盐的新格式）。"""
        return self.bearer_hash or self.bearer_sha256

    @property
    def legacy_credentials(self) -> list[str]:
        """还在用**历史无盐格式**的凭据名（管理端据此提示轮换）。"""
        names = []
        if self.key_sha256 and not self.key_hash:
            names.append("key")
        if self.bearer_sha256 and not self.bearer_hash:
            names.append("bearer")
        return names

    @field_validator("qq")
    @classmethod
    def _clean_qq(cls, value: str) -> str:
        return "".join(ch for ch in value if ch.isdigit())

    @field_validator("stream_id")
    @classmethod
    def _clean_stream_id(cls, value: str) -> str:
        """只保留 URL 路径安全字符，避免拼出越界路径。"""
        return "".join(ch for ch in value.strip().strip("/") if ch.isalnum() or ch in "-_")

    @property
    def display_name(self) -> str:
        return self.name or self.uid

    @property
    def has_avatar_source(self) -> bool:
        if self.avatar:
            return True
        return self.qq.isdigit() and 4 <= len(self.qq) <= 12

    def public(self) -> dict[str, Any]:
        """对外**脱敏**结构：展示信息 + 推流 ID；密钥 / 令牌绝不下发。

        字段名统一走 camelCase 别名（与全站 JSON 约定一致）。
        """
        data: dict[str, Any] = self.model_dump(by_alias=True, include=list(PUBLIC_MEMBER_FIELDS))
        data["hasKey"] = bool(self.key_stored)
        data["hasBearer"] = bool(self.bearer_stored)
        data["hasAvatar"] = self.has_avatar_source
        # 注：「凭据是不是历史无盐格式」属于内部细节，只在管理端视图里补（见 members._member_public），
        # 不放进这里的公开结构，免得访客也能看出谁的凭据是旧的。
        return data

    def private(self) -> dict[str, Any]:
        """管理端结构：在公开字段基础上补上隐私字段（仍**不含**密钥 / 令牌明文）。"""
        data = self.public()
        data["qq"] = self.qq
        data["note"] = self.note
        data["createdAt"] = self.created_at
        data["updatedAt"] = self.updated_at
        return data


class LiveBan(NTEModel):
    """直播间封禁记录。

    * ``scope="global"``：服务器管理员签发，任何届次均生效，且只有服务器管理员能解除；
    * ``scope="event"``：赛事管理员对自己那届的直播间签发，仅在该届生效。

    ``until`` 为空 = 永久封禁；否则到期自动失效（判定见 ``logic.ban_active``）。
    """

    id: str = ""
    scope: LiveBanScope = "global"
    member_uid: str = ""      # 被禁成员（可空：仅按流名封禁）
    stream_id: str = ""       # 被禁的推流 ID（= 成员 stream_id / 频道流名）
    name: str = ""            # 展示名缓存（成员改名后仍能显示当时的名字）
    reason: str = ""
    until: str = ""           # 解禁时间 ISO；空 = 永久
    event_id: str = ""        # scope=event 时所属届
    created_at: str = ""
    created_by: str = ""      # 执行者展示名 / 权限


# --------------------------------------------------------------------------- #
# 对局
# --------------------------------------------------------------------------- #
class Side(NTEModel):
    """一场比赛中的一方（一方 = 一支固定队伍）。

    ``score`` 与 ``points`` 的含义随同场队伍数变化：

    * 2 队（组 vs 组）：``score`` = 局分 / 大比分，``points`` = 总小分（可选）
    * 3~4 队同场：``score`` = 该场得分（排名依据），``points`` = 细则分（可选）

    ``rank`` 为该场名次（1 起），由录入内容**自动推导**，用于小组赛名次分计算。
    """

    player_ids: list[str] = Field(default_factory=list)
    team_id: str = ""
    label: str = ""
    score: int = 0
    points: int = 0
    rank: int = 0
    forfeit: bool = False     # 弃权（长期没人 / 人数不足）：名次垫底，对方自动晋级
    source: str = ""          # 席位来源说明，如「A 组第 1」「WB-1-2 败者」

    @field_validator("player_ids")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for pid in value:
            if pid and pid not in seen:
                seen.append(pid)
        return seen


class SetScore(NTEModel):
    """一局的小分（仅 2 队对阵有意义）。"""

    a: int = 0
    b: int = 0


class Round(NTEModel):
    """一场比赛：2~4 支队伍同场（小组赛可为多队，淘汰赛恒为 2 队）。

    小组赛与淘汰赛共用本模型，``stage`` 区分阶段。
    淘汰赛对阵的双方由 ``src_a`` / ``src_b`` 引用上游结果推导，
    因此「败者组成员随比赛进程自动生成」只是一次纯函数重算。

    ``sides`` 是权威字段（A/B/C/D 顺序即出场顺序）；
    ``side_a`` / ``side_b`` 是兼容访问器，供只关心 2 队的逻辑使用。
    """

    index: int = 1
    code: str = ""            # 全局唯一编号：G-A-1 / WB-1-2 / LB-3-1 / GF
    stage: Stage = "group"
    bracket_round: int = 0    # 阶段内轮次序号（从 1 开始）
    slot: int = 0             # 阶段内第几场（从 1 开始）
    label: str = ""
    status: RoundStatus = "pending"
    sides: list[Side] = Field(default_factory=lambda: [Side(), Side()])
    winner: WinnerCode = ""
    note: str = ""
    # 各局小分（2 队时用于自动推导局分与总得分）
    sets: list[SetScore] = Field(default_factory=list)
    duration_minutes: int = 0  # 用时（分钟），0 = 未登记（回退到起止时间差）
    # 本场是否安排直播：直播开关 + 直播选手提示
    live: bool = False
    live_note: str = ""
    scheduled_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    locked: bool = False
    # 淘汰赛席位来源与去向（引用其它对局的 code）
    src_a: str = ""           # seed:3 / WB-1-1:W / WB-1-1:L / LB-2-1:W
    src_b: str = ""
    winner_to: str = ""       # 胜者去向（对局 code，GF 为空）
    loser_to: str = ""        # 败者去向（对局 code，败者组入口）

    @model_validator(mode="before")
    @classmethod
    def _legacy_sides(cls, data: Any) -> Any:
        """兼容旧数据里的 ``sideA`` / ``sideB`` 写法。"""
        if not isinstance(data, dict):
            return data
        keys = {k.lower().replace("_", "") for k in data}
        if "sides" in keys:
            return data
        legacy = [data.get("sideA") or data.get("side_a"), data.get("sideB") or data.get("side_b")]
        if any(item is not None for item in legacy):
            merged = dict(data)
            merged["sides"] = [item for item in legacy if item is not None]
            return merged
        return data

    @field_validator("sides")
    @classmethod
    def _normalize_sides(cls, value: list[Side]) -> list[Side]:
        """至少两方（不足补空位），最多 4 方。"""
        out = list(value or [])
        while len(out) < 2:
            out.append(Side())
        return out[:MAX_SIDES]

    def side_at(self, index: int) -> Side:
        """第 ``index`` 方（0 起）；越界时抛出 ``IndexError``。"""
        return self.sides[index]

    @property
    def side_a(self) -> Side:
        return self.sides[0]

    @side_a.setter
    def side_a(self, value: Side) -> None:
        self.sides[0] = value

    @property
    def side_b(self) -> Side:
        return self.sides[1]

    @side_b.setter
    def side_b(self, value: Side) -> None:
        self.sides[1] = value

    @property
    def side_map(self) -> dict[SideKey, Side]:
        """按 A/B/C/D 取方（只包含实际存在的方）。"""
        return {chr(ord("A") + i): side for i, side in enumerate(self.sides)}

    def key_of(self, side: Side) -> str:
        """某一方对应的 A/B/C/D 编号。"""
        for i, item in enumerate(self.sides):
            if item is side:
                return chr(ord("A") + i)
        return ""

    def side_by_key(self, key: str) -> Side | None:
        idx = ord((key or "").upper()[:1] or "A") - ord("A")
        return self.sides[idx] if 0 <= idx < len(self.sides) else None

    @property
    def ranked_sides(self) -> list[Side]:
        """按本场名次排序的各方（未判定名次时按出场顺序）。"""
        if any(s.rank for s in self.sides):
            return sorted(self.sides, key=lambda s: (s.rank or 99))
        return list(self.sides)


# --------------------------------------------------------------------------- #
# 赛事配置
# --------------------------------------------------------------------------- #
class EventInfo(NTEModel):
    name: str = ""            # 届名（多届赛事标识，如「2026 国庆赛」）
    status: Literal["draft", "active", "closed"] = "active"
    # 归属：由哪位成员创建（赛事管理员只能管理 / 删除自己创建的届；空 = 仅服务器管理员可管）
    owner_uid: str = ""
    hidden: bool = False      # 隐藏：不出现在公开的届次列表里（服务器管理员仍可见）
    # 比赛类型：预设 key（volleyball / racing / photography / generic）或自定义字符串。
    # 只影响界面文案（参赛者 / 成绩 / 场次的称呼），数据模型不变。
    sport: str = "volleyball"
    # 排名开关：关 = 娱乐记录模式（只记录场次与分数，不排名、不晋级、不判冠军）
    ranked: bool = True
    # 比赛简介：由赛事创办者撰写，**允许留空**；留空时主界面与往届列表都不显示这一项。
    # 长度上限 30 个字（按字符计，中英文同规）。
    brief: str = ""
    title: str = "NTE 比赛"
    subtitle: str = "NEVERNESS TO EVERNESS · MATCH"
    venue: str = ""
    organizer: str = ""
    start_time: str = ""      # 开赛时间（本地时间，YYYY-MM-DDTHH:MM:SS）
    end_time: str = ""        # 结束时间；留空表示「尚未结束 / 待定」
    # 比赛已开始：赛制与参赛名单冻结（管理员二次确认后置位）。
    # 直播开关、替补换人、录分与时间登记**不受**锁定影响。
    locked: bool = False
    locked_at: str = ""       # 开赛（锁定）时刻
    rules_text: str = ""
    logo_text: str = "NTE"

    @field_validator("sport")
    @classmethod
    def _clean_sport(cls, value: str) -> str:
        """类型 key 只保留安全字符，避免拼进 HTML / 路径。"""
        clean = "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in "-_")
        return clean[:32] or "volleyball"

    @field_validator("brief")
    @classmethod
    def _clean_brief(cls, value: str) -> str:
        """比赛简介：合并空白并**硬性截到 30 字**（界面用 maxlength 挡在前面）。

        这里刻意「截断」而不是「报错」：简介是随时可改的展示文案，
        没必要因为多打一个字就让整个赛事信息存不进去。
        """
        return " ".join(str(value or "").split())[:MAX_BRIEF_CHARS]


class Rules(NTEModel):
    """赛制参数。``format`` 决定使用哪一套规则，两套赛制共用一份模型。"""

    format: MatchFormat = "tournament"

    # ---- 通用 ----
    team_size: int = 2           # 每队上场人数（默认 2；可按队伍分别调整）
    teams_per_match: int = 2     # 每场同场竞技的队伍数：2 / 3 / 4（小组赛生效）
    target_score: int = 0        # 单局目标分，0 表示不限制
    allow_draw: bool = False     # 是否允许平局（仅小组赛生效）

    # ---- 积分制（league）----
    points_win: int = 3          # 胜方积分
    points_lose: int = 0         # 负方积分
    points_draw: int = 1         # 平局积分
    total_rounds: int = 5        # 总轮次
    include_substitutes: bool = True
    fair_rotation: bool = True   # 均衡出场、避免连续轮空与重复搭档
    min_rank_played: int = 5     # 参与排名的最少场次

    # ---- 锦标赛制（tournament）----
    group_count: int = 0         # 小组赛组数，0 = 按队伍数自动推算
    knockout_size: int = 0       # 淘汰赛规模（2 的幂），0 = 自动取最大可行值
    loser_bracket: bool = True   # 败者组开关：开 = 双败淘汰，关 = 输一场即淘汰

    @field_validator("teams_per_match")
    @classmethod
    def _clamp_teams_per_match(cls, value: int) -> int:
        return max(MIN_TEAMS_PER_MATCH, min(MAX_SIDES, int(value or MIN_TEAMS_PER_MATCH)))

    @field_validator("team_size")
    @classmethod
    def _clamp_team_size(cls, value: int) -> int:
        return max(1, min(6, int(value or 1)))


class StreamConfig(NTEModel):
    """MediaMTX 直播配置：**推流只有 WHIP，观看只有 WebRTC 与 HLS**。

    | 用途 | 地址 |
    | --- | --- |
    | 推流（WHIP，8889） | `https://…:8889/<流名>/whip` |
    | 观看（WebRTC，8889） | `https://…:8889/<流名>` |
    | 观看（HLS，8888） | `https://…:8888/<流名>` |

    端口：8889 = WebRTC（WHIP 推 / WebRTC 看），8888 = HLS。
    地址只在端口上按流名区分，没有 ``/whep`` 之类的子路由。
    推流地址带凭据，只出现在管理端，绝不进公开状态。

    地址是**源站地址**（不做反代，前端/播放器直连媒体服务器）。

    两条观看线路都是 HTTP 系，**默认走 HTTPS**：本站上 CDN 后是 HTTPS 页面，
    用 ``http://`` 会被浏览器按混合内容拦掉。对应 MediaMTX 侧要开
    ``webrtcEncryption: yes`` / ``hlsEncryption: yes`` 并配置证书；
    ``verify_tls`` 只影响本服务的**源站探测**（``/api/live/health``），
    自签名证书时关掉即可——观众侧仍需要浏览器信任的证书。
    """

    enabled: bool = True
    provider: str = "mediamtx"
    base_url: str = "https://live.shiyora.net:8889"      # WebRTC 端口（WHIP 推 / WebRTC 看）
    api_base: str = "http://live.shiyora.net:9997"       # MediaMTX 控制 API：读「谁真的在推流」
    # 控制 API 的 Basic 认证：mediamtx.yml 里配了 authInternalUsers 时必填
    # （``curl -u 用户名:密码``）。属于凭据：只在管理端下发，绝不进公开状态。
    api_user: str = ""
    api_pass: str = ""
    hls_base: str = "https://live.shiyora.net:8888"     # HLS 根地址，用于按流名派生
    stream_key: str = "stream"
    mode: StreamMode = "auto"
    verify_tls: bool = True     # 校验上游 HTTPS 证书；用自签名证书时关掉
    whip_push: str = "https://live.shiyora.net:8889/stream/whip"
    poster: str = ""
    title: str = "赛事直播"
    note: str = ""

    @field_validator("mode", mode="before")
    @classmethod
    def _clean_mode(cls, value: Any) -> str:
        """历史数据里可能有 flv / embed（已不再支持），一律回落到 auto。"""
        candidate = str(value or "").strip().lower()
        return candidate if candidate in ("auto", "webrtc", "hls") else "auto"


class UiConfig(NTEModel):
    accent: str = "cyan"
    show_qq: bool = True
    show_avatar: bool = True
    reveal_results: bool = True
    ticker: str = ""


class AdminConfig(NTEModel):
    """服务器主管理 KEY。

    ``key_hash`` 是**加盐 PBKDF2**（``pbkdf2_sha256$迭代$盐$摘要``，新格式，推荐）；
    ``key_sha256`` / ``key`` 只为历史数据兼容而保留（无盐哈希 / 明文），
    一旦重新设置 KEY 就会切换成 ``key_hash``。
    """

    key: str = ""
    key_sha256: str = ""
    key_hash: str = ""


class Config(NTEModel):
    version: int = 1
    revision: int = 0
    updated_at: str = ""
    event: EventInfo = Field(default_factory=EventInfo)
    rules: Rules = Field(default_factory=Rules)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    teams: list[Team] = Field(default_factory=list)
    players: list[Player] = Field(default_factory=list)
    rounds: list[Round] = Field(default_factory=list)
    # 本届实际参与的选手 ID；手填的「参与名单」。
    # 为空表示未指定，视为报名池中全部启用选手参与（兼容旧数据）。
    participants: list[str] = Field(default_factory=list)

    @field_validator("participants")
    @classmethod
    def _unique_participants(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for pid in value:
            clean = (pid or "").strip()
            if clean and clean not in seen:
                seen.append(clean)
        return seen
