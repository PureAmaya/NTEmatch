"""新建赛事届时使用的默认赛事配置。

仅在数据库中没有任何届次（或首次迁移旧配置）时使用，
之后完全以 ``config/nte.sqlite3`` 为准。

**不带任何示例数据**：选手、队伍、赛程与比分一律为空，
届状态是「筹备中」且没有开赛时间，全部等管理员自己录入。
"""

from __future__ import annotations

import copy
from typing import Any

#: 界面的**固定用词**。
#:
#: 「比赛类型」（排球 / 赛车 / 摄影…）这一层已经退休：它只干一件事——把「选手」
#: 换成「车手」、「对局」换成「赛段」，而组织者真正要的是**规则跟着赛制走**。
#: 多一层可选择的东西，就多一处会跟实际赛制对不上的地方（也是「规则里出现了
#: 另一种比赛的用词」的来源）。保留键名不变，取词的代码不用改结构。
SPORT_WORDS: dict[str, str] = {
    "key": "default",
    "label": "比赛",
    "participant": "选手",
    "participants": "选手",
    "score": "比分",
    "round": "对局",
    "venue": "场地",
}

# 兼容壳：老数据里还留着 ``sport`` 字段，代码里也可能还有零星调用点。
# 无论传什么 key 都回同一套用词（**不再按类型换称呼**）。
SPORT_PRESETS: dict[str, dict[str, str]] = {
    "volleyball": {
        "label": "排球 / 对抗赛",
        "participant": "选手",
        "participants": "选手",
        "score": "比分",
        "round": "对局",
        "venue": "场地",
        # 计分口径的**建议值**（见 app/metrics.py）：只用来在管理端提示，
        # 不会自动改组织者选定的赛制——赛制是他的事，工具不该替他做主。
        "valueType": "integer",
        "valueLabel": "得分",
        "better": "high",
    },
    "racing": {
        "label": "赛车",
        "participant": "车手",
        "participants": "车手",
        "score": "成绩",
        "round": "赛段",
        "venue": "赛道",
        "valueType": "time",
        "valueLabel": "用时",
        "better": "low",
    },
    "photography": {
        "label": "摄影",
        "participant": "作者",
        "participants": "作者",
        "score": "得分",
        "round": "作品",
        "venue": "赛区",
        "valueType": "decimal",
        "valueLabel": "评分",
        "better": "high",
    },
    "generic": {
        "label": "通用 / 其它",
        "participant": "参赛者",
        "participants": "参赛者",
        "score": "得分",
        "round": "场次",
        "venue": "场地",
        "valueType": "integer",
        "valueLabel": "得分",
        "better": "high",
    },
}

DEFAULT_SPORT = "volleyball"


def sport_meta(key: str = "") -> dict[str, str]:
    """**已退休**：比赛类型不再影响界面用词，一律回 :data:`SPORT_WORDS`。

    留着这个函数只为一件事：老库里仍有 ``sport`` 字段、代码里也有零星调用点，
    让它们照常拿到一套用词，而不是报错。
    """
    del key  # 传什么都不再影响结果
    return dict(SPORT_WORDS)


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "revision": 0,
    "updatedAt": "",
    "event": {
        "name": "首届赛事",
        # 新届没有任何选手与比赛，状态从「筹备中」开始；点「开始比赛」后自动转为进行中
        "status": "draft",
        # 比赛类型（预设 key 或自定义）与排名开关（关 = 娱乐记录模式，不排名 / 不晋级）
        "sport": DEFAULT_SPORT,
        "ranked": True,
        "title": "NTE 比赛",
        "subtitle": "NEVERNESS TO EVERNESS · MATCH",
        # 比赛简介（≤30 字，可留空；留空则主界面与往届列表都不显示）
        "brief": "",
        "venue": "",
        "organizer": "",
        # 留空 = 尚未登记开赛时间（界面显示「时间待定」），开始比赛时自动补上
        "startTime": "",
        "endTime": "",
        "locked": False,
        "lockedAt": "",
        # 赛事信息（可写 Markdown 的「参赛须知」）：**出厂留空**。
        # 规则由「比赛规则」面板按当前赛制自动生成（见 logic.rulebook），
        # 这里只说组织者想补充的话——预填一段通则既会与自动规则重复，
        # 又会在改了赛制之后变成一句假话。
        "rulesText": "",
        "logoText": "NTE",
    },
    "rules": {
        "teamSize": 2,             # 固定队伍人数（2 即 2v2）
        "targetScore": 0,          # 单轮目标分，0 表示不限制
        "groupCount": 0,           # 小组赛组数，0 = 按队伍数自动推算
        "allowDraw": False,        # 仅小组赛允许平局
        # 计分口径（见 app/metrics.py）：类型决定怎么解析与显示，判断标准决定谁赢
        "valueType": "integer",    # integer 自然数 / decimal 小数 / time 时间
        "valueLabel": "得分",      # 展示标签：得分 / 评分 / 用时 / 自定义
        "better": "high",          # high 数值高胜 / low 数值低胜
    },
    "stream": {
        "provider": "mediamtx",
        # 出厂**不预填任何地址**：每一套部署的媒体服务器都不一样，预填别人的域名
        # 会让新装的人「看起来配好了、其实连的是别人的服务器」。留空时各处都会
        # 明确提示「未配置」（见 live.py 的守卫），不会静默失败。
        # 两条观看线路都是 HTTP 系，要填 HTTPS：本站上 CDN 后是 HTTPS 页面，
        # http:// 会被浏览器当混合内容拦掉（WebRTC 与 HLS 观看都会失败）。
        "baseUrl": "",
        # MediaMTX 的控制 API（默认 :9997）：用来查「谁真的在推流」，
        # 只有媒体服务器上报 ready 的机位才会显示「直播中」
        "apiBase": "",
        # 控制 API 的 Basic 认证（mediamtx.yml 配了 authInternalUsers 才需要）
        "apiUser": "",
        "apiPass": "",
        "hlsBase": "",
        "streamKey": "stream",
        # 主直播间 / 遗留频道的推流令牌：留空 = 流名登记过就放行（旧行为）；
        # 填了则推这些流名也必须带令牌（成员机位本来就要求「推流 ID + 成员令牌」）
        "pushToken": "",
        "mode": "auto",
        # 只影响本服务的源站探测（/api/live/health）；自签名证书时关掉
        "verifyTls": True,
        "whipPush": "",
        "poster": "",
        "title": "赛事直播",
        # 备注**默认留空**：以前这里预填了一段「推流用 WHIP、8889/8888、要开
        # webrtcEncryption…」的实现说明，而它会出现在直播页上给观众看——
        # 观众不需要读技术文档（那些内容归 README）。组织者想说什么自己填。
        "note": "",
    },
    "ui": {
        "accent": "cyan",
        "showQq": True,
        "showAvatar": True,
        "revealResults": True,
        "ticker": "一场可记多轮 · 赢的轮数就是大比分 · 逐局累计积分 · NEVERNESS TO EVERNESS",
    },
    # 出厂不带任何示例数据：没有队伍、没有选手、没有比赛
    "teams": [],
    "players": [],
    "rounds": [],
    "participants": [],
}


def default_config() -> dict[str, Any]:
    """返回一份默认配置的深拷贝，避免调用方意外污染模板。"""
    return copy.deepcopy(DEFAULT_CONFIG)
