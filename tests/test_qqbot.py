"""QQ 机器人推送侧的检查。

两条主线：

1. **出厂默认值里不许出现任何具体域名**——预填别人的地址会让人「看着配好了、
   其实消息（连同 API Key）发到别人的机器人上」。这类问题不报错，只会在某天
   被群里的人问「为什么机器人没反应」时才发现。
2. 推送的失败必须是**一句话说清楚**，而不是 500：地址留空时 httpx 抛的是
   ``UnsupportedProtocol``，它不是 ``HTTPError``，漏接就变成 500。
"""

from __future__ import annotations

import json

from app import qqbot
from app.defaults import default_config
from app.models import StreamConfig


def test_defaults_have_no_personal_hosts():
    """出厂配置（含模型兜底值）里不能出现任何具体域名。"""
    blob = json.dumps(
        {
            "config": default_config(),
            "qqbot": qqbot.DEFAULT_SETTINGS,
            "stream": StreamConfig().dump(),
        },
        ensure_ascii=False,
    )
    assert "shiyora" not in blob
    assert StreamConfig().base_url == ""
    assert StreamConfig().hls_base == ""
    assert qqbot.DEFAULT_SETTINGS["baseUrl"] == ""


async def test_send_text_reports_missing_base_url():
    """没填地址时给一句人话，而不是抛异常（那会变成 500）。"""
    result = await qqbot.send_text(
        "测试",
        settings={
            "enabled": True,
            "apiKey": "abk_x",
            "baseUrl": "",
            "umo": "aiocqhttp:GroupMessage:123456",
        },
    )
    assert result["ok"] is False
    assert "未配置 AstrBot 地址" in result["detail"]


async def test_send_text_refuses_when_disabled_or_without_key():
    """三道前置检查各自给出明确原因（顺序：启用 → Key → 地址 → 目标会话）。"""
    base = {"baseUrl": "https://bot.test", "umo": "aiocqhttp:GroupMessage:1"}
    off = await qqbot.send_text("x", settings={**base, "enabled": False, "apiKey": "k"})
    assert "未启用" in off["detail"]
    no_key = await qqbot.send_text("x", settings={**base, "enabled": True, "apiKey": ""})
    assert "API Key" in no_key["detail"]
    no_target = await qqbot.send_text(
        "x", settings={"enabled": True, "apiKey": "k", "baseUrl": "https://bot.test", "umo": ""}
    )
    assert "目标会话" in no_target["detail"]


def test_plain_text_conversion_is_idempotent():
    """纯文本化可以反复调用（出站前只过一次，但幂等是它的契约）。"""
    source = "**加粗** 和 [文字](https://x.test) 以及 `代码`"
    once = qqbot.to_plain_text(source)
    assert once == qqbot.to_plain_text(once)
    assert "**" not in once


def test_list_message_hint_points_to_a_real_command():
    """列表翻页提示必须让用户发**真实存在**的命令（以前写「发送「下一页」」）。"""
    events = [
        {"id": f"e{i:03d}", "name": f"第 {i} 届", "status": "active", "players": 8, "rounds": 3}
        for i in range(1, 40)
    ]
    text, pages = qqbot.build_events_message(events, page=1, per_page=8)
    assert pages > 1
    assert "发「比赛列表 2」" in text
    assert "发送「下一页」" not in text  # 「下一页」既不是命令也不是别名
