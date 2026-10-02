"""新建赛事届时使用的默认赛事配置。

仅在数据库中没有任何届次（或首次迁移旧配置）时使用，
之后完全以 ``config/nte.sqlite3`` 为准。

**不带任何示例数据**：选手、队伍、赛程与比分一律为空，
届状态是「筹备中」且没有开赛时间，全部等管理员自己录入。
"""

from __future__ import annotations

import copy
from typing import Any

# 出厂管理 KEY：仅在数据库里还没有任何届次时写入一次。
# 只要它没被改过，启动时就会在控制台提示；改过之后不再显示。
DEFAULT_ADMIN_KEY = "NTE-ADMIN"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "revision": 0,
    "updatedAt": "",
    "event": {
        "name": "首届赛事",
        # 新届没有任何选手与比赛，状态从「筹备中」开始；点「开始比赛」后自动转为进行中
        "status": "draft",
        "title": "NTE 比赛",
        "subtitle": "NEVERNESS TO EVERNESS · MATCH",
        "venue": "",
        "organizer": "",
        # 留空 = 尚未登记开赛时间（界面显示「时间待定」），开始比赛时自动补上
        "startTime": "",
        "endTime": "",
        "locked": False,
        "lockedAt": "",
        "rulesText": (
            "固定队伍：确定参与名单后随机分配队友并全程固定（每个组的人数可调，默认 2 人）。"
            "小组赛每场可为组 vs 组 / 三队同场 / 四队同场，按名次分排名，"
            "取总排名前 N 名进入淘汰赛（十六强 / 八强 / 半决赛 / 决赛）。"
            "淘汰赛默认双败：落败者进入败者组，最终由胜者组冠军与败者组冠军争夺总冠军；"
            "关闭败者组即为单败淘汰，输一场直接淘汰。"
        ),
        "logoText": "NTE",
    },
    "rules": {
        "teamSize": 2,             # 固定队伍人数（2 即 2v2）
        "targetScore": 0,          # 单局目标分，0 表示不限制
        "groupCount": 0,           # 小组赛组数，0 = 按队伍数自动推算
        "allowDraw": False,        # 仅小组赛允许平局
    },
    "stream": {
        "enabled": True,
        "provider": "mediamtx",
        # HTTP 系（WebRTC / HLS）默认 HTTPS：本站上 CDN 后是 HTTPS 页面，
        # http:// 会被浏览器当混合内容拦掉（含内嵌播放页与 WHEP 拉流）。
        "baseUrl": "https://live.shiyora.net:8889",
        # MediaMTX 的控制 API（默认 :9997）：用来查「谁真的在推流」，
        # 只有媒体服务器上报 ready 的机位才会显示「直播中」
        "apiBase": "https://live.shiyora.net:9997",
        # 控制 API 的 Basic 认证（mediamtx.yml 配了 authInternalUsers 才需要）
        "apiUser": "",
        "apiPass": "",
        "rtmpBase": "rtmp://live.shiyora.net:1935",
        "rtspBase": "rtsp://live.shiyora.net:8554",
        "hlsBase": "https://live.shiyora.net:8888",
        "streamKey": "stream",
        "mode": "auto",
        # 只影响本服务的源站探测（/api/live/health）；自签名证书时关掉
        "verifyTls": True,
        "rtmpPush": "rtmp://live.shiyora.net:1935/stream",
        "whipPush": "https://live.shiyora.net:8889/stream/whip",
        "rtspUrl": "rtsp://live.shiyora.net:8554/stream",
        "hlsUrl": "https://live.shiyora.net:8888/stream/index.m3u8",
        "flvUrl": "",
        "poster": "",
        "title": "赛事直播",
        "note": (
            "支持 WebRTC(WHEP) 与 HLS 观看（均为 HTTPS），推流可用 OBS 的 RTMP 或 WHIP 方式；"
            "媒体服务器需开启 webrtcEncryption / hlsEncryption 并配置证书。"
        ),
    },
    "ui": {
        "accent": "cyan",
        "showQq": True,
        "showAvatar": True,
        "revealResults": True,
        "ticker": "BO1 一局定胜负 · 多局累计积分 · 输一局不淘汰 · NEVERNESS TO EVERNESS",
    },
    "admin": {"key": DEFAULT_ADMIN_KEY, "keySha256": ""},
    # 出厂不带任何示例数据：没有队伍、没有选手、没有比赛
    "teams": [],
    "players": [],
    "rounds": [],
    "participants": [],
}


def default_config() -> dict[str, Any]:
    """返回一份默认配置的深拷贝，避免调用方意外污染模板。"""
    return copy.deepcopy(DEFAULT_CONFIG)
