"""机器人沙盒（``/bot``）：缓冲行为 + 只在调试模式下存在的接口。

这个页面的价值是「不用接 QQ 就能看到会发什么」，所以两条底线必须钉住：

* **组装走真实链路**——沙盒里的文本必须与 ``/api/qqbot/preview`` 逐字一致
  （两边共用 ``build_message``，这里用测试把这条约束钉死）；
* **生产环境里不存在**——``NTE_DEBUG`` 没开时接口一律 404，页面也不该泄露任何东西。
"""

from __future__ import annotations

import pytest

from app import botlab, qqbot


@pytest.fixture(autouse=True)
def _clean_buffer():
    """每个用例前后清空缓冲（模块级单例，测试之间不该互相看见消息）。"""
    botlab.clear()
    yield
    botlab.clear()


def test_record_and_recent_order():
    """记录按发生顺序返回（旧 → 新），页面从下往上长。"""
    botlab.record(kind="event", parts=["第一段", "第二段"])
    botlab.record(kind="result", parts=["结果"])
    messages = botlab.recent()
    assert [m["kind"] for m in messages] == ["event", "result"]
    assert messages[0]["parts"] == ["第一段", "第二段"]
    assert messages[0]["id"] < messages[1]["id"]


def test_empty_parts_are_dropped_but_message_is_kept():
    """空段丢掉，但**消息本身要留下**：真的出过「拼出来是空的」，看不见更难查。"""
    msg = botlab.record(kind="event", parts=["", "  ", "有内容"])
    assert msg["parts"] == ["有内容"]
    empty = botlab.record(kind="event", parts=["", ""])
    assert empty["parts"] == []
    assert len(botlab.recent()) == 2


def test_buffer_is_capped():
    """环形缓冲不会无限长（模块级单例，跑久了不能把内存当日志文件用）。"""
    for index in range(botlab.MAX_MESSAGES + 5):
        botlab.record(kind="event", parts=[f"第 {index} 条"])
    messages = botlab.recent()
    assert len(messages) == botlab.MAX_MESSAGES
    assert messages[-1]["parts"] == [f"第 {botlab.MAX_MESSAGES + 4} 条"]


def test_clear_reports_how_many():
    botlab.record(kind="event", parts=["a"])
    botlab.record(kind="event", parts=["b"])
    assert botlab.clear() == 2
    assert botlab.recent() == []


def test_kinds_come_from_the_bot_itself():
    """类型表必须**直接来自** ``qqbot.KIND_META``（别在这里抄一份，会漂移）。"""
    keys = [item["key"] for item in botlab.kinds()]
    assert keys == list(qqbot.KIND_META)
    assert botlab.kinds()[0]["name"] == qqbot.KIND_META[keys[0]][0]


def test_title_defaults_to_the_kind_name():
    msg = botlab.record(kind="result", parts=["x"])
    assert msg["title"] == qqbot.KIND_META["result"][0]


async def test_spa_serves_the_bot_page(client):
    """``/bot`` 是**前端路由**：服务端要能把它当独立页返回，且标题不是届次名。"""
    res = await client.get("/bot")
    assert res.status_code == 200
    assert "view-bot" in res.text
    assert "机器人沙盒" in res.text, "标题 / 分享预览要认出这是独立页，不能当成某个届次"


async def test_api_is_404_when_debug_is_off(admin_client, monkeypatch):
    """生产环境（没开调试）里这个接口不存在——不是 403，是 404。"""
    monkeypatch.delenv("NTE_DEBUG", raising=False)
    # 未登录也一样是 404 或 401 之外的「查无此路由」语义：这里用管理员会话确认是 404
    res = await admin_client.get("/api/debug/bot")
    assert res.status_code == 404


async def test_api_requires_server_admin_when_debug_on(client, monkeypatch):
    """开了调试也有人管：未登录不给看（沙盒里会有届名等信息）。"""
    monkeypatch.setenv("NTE_DEBUG", "1")
    res = await client.get("/api/debug/bot")
    assert res.status_code == 401


async def test_api_state_lists_kinds_and_events(admin_client, monkeypatch):
    monkeypatch.setenv("NTE_DEBUG", "1")
    res = await admin_client.get("/api/debug/bot")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enabled"] is True
    assert [item["key"] for item in body["kinds"]] == list(qqbot.KIND_META)
    assert body["events"], "至少要能选一届"
    assert "imageAvailable" in body


async def test_build_matches_the_preview_endpoint(admin_client, monkeypatch):
    """沙盒组装的文本必须与「预览」逐字一致（两边共用 build_message）。"""
    monkeypatch.setenv("NTE_DEBUG", "1")
    preview = await admin_client.get("/api/qqbot/preview", params={"kind": "event"})
    assert preview.status_code == 200, preview.text
    expected = preview.json()["parts"]

    built = await admin_client.post("/api/debug/bot/build", json={"kind": "event"})
    assert built.status_code == 200, built.text
    message = built.json()["message"]
    assert message["parts"] == expected
    assert message["source"] == "preview"
    # 记进缓冲了
    assert botlab.recent()[-1]["id"] == message["id"]


async def test_text_endpoint_uses_the_real_splitter(admin_client, monkeypatch):
    """自由文本走的是真发出去时那个切分函数（分段结果要与它一致）。"""
    monkeypatch.setenv("NTE_DEBUG", "1")
    text = "第一行\n" * 800  # 远超单段上限，必然被切开
    res = await admin_client.post("/api/debug/bot/text", json={"text": text})
    assert res.status_code == 200, res.text
    body = res.json()
    limit = body["limit"]
    assert body["message"]["parts"] == qqbot.split_message(text, limit)
    assert len(body["message"]["parts"]) > 1, "这么长的文本应当被切成多段"


async def test_text_endpoint_rejects_blank(admin_client, monkeypatch):
    monkeypatch.setenv("NTE_DEBUG", "1")
    res = await admin_client.post("/api/debug/bot/text", json={"text": "   "})
    assert res.status_code == 400


async def test_clear_endpoint(admin_client, monkeypatch):
    monkeypatch.setenv("NTE_DEBUG", "1")
    await admin_client.post("/api/debug/bot/build", json={"kind": "event"})
    res = await admin_client.post("/api/debug/bot/clear", json={})
    assert res.status_code == 200
    assert res.json()["removed"] >= 1
    assert botlab.recent() == []


async def test_push_is_recorded_only_in_debug(admin_client, monkeypatch):
    """真实推送也记一条（页面兼作「最近发出去什么」的记录），但调试关着就什么都不记。"""
    monkeypatch.delenv("NTE_DEBUG", raising=False)
    from app import qqbot_api

    qqbot_api._record_push(kind="event", parts=["x"], card=None, mentions=[], event_id="e001")
    assert botlab.recent() == []

    monkeypatch.setenv("NTE_DEBUG", "1")
    qqbot_api._record_push(
        kind="event",
        parts=["正文"],
        card={"url": "http://x/card.png", "hash": "abc"},
        mentions=["10001"],
        event_id="e001",
    )
    message = botlab.recent()[-1]
    assert message["source"] == "push"
    assert message["parts"] == ["正文"]
    assert message["image"]["hash"] == "abc"
    assert message["mentions"] == ["10001"]

# --------------------------------------------------------------------------- #
# 模拟群：认命令
# --------------------------------------------------------------------------- #
def test_parse_maps_aliases_to_kinds():
    """群友打的「进度 / 结果 / 谁在播」要落到站点侧那几个 kind 上。"""
    assert botlab.parse("比赛") == ("kind", "detail")
    assert botlab.parse("进度") == ("kind", "progress")
    assert botlab.parse("什么情况") == ("kind", "progress")
    assert botlab.parse("结果咋样") == ("kind", "result")
    assert botlab.parse("谁在播") == ("kind", "live")
    assert botlab.parse("都有谁") == ("kind", "roster")
    # 群聊常见前缀与多余参数都不影响
    assert botlab.parse("！比赛") == ("kind", "detail")
    assert botlab.parse("/比赛  e002") == ("kind", "detail")


def test_parse_handles_help_identity_and_unknown():
    assert botlab.parse("帮助")[0] == "help"
    assert botlab.parse("比赛帮助")[0] == "help"
    action, name = botlab.parse("报名")
    assert action == "identity" and name == "报名"
    assert botlab.parse("今天天气不错") == ("unknown", "")
    assert botlab.parse("   ") == ("unknown", "")


def test_alias_table_covers_the_plugin():
    """**别名必须与插件逐条对齐**：插件加了命令、这里没跟，模拟群就会「没听懂」。

    直接从插件源码里抽 ``@filter.command("x", alias={...})``，比人肉对表可靠。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / "integrations" / "astrbot_plugin_nte_match" / "main.py").read_text(encoding="utf-8")
    blocks = re.findall(r"@filter\.command\((.{0,600}?)\)\s*\n\s*async def", source, re.DOTALL)
    assert blocks, "没从插件源码里抽到命令——正则或插件写法变了"

    known = {name.lower() for name in (*botlab.QUERY_COMMANDS, *botlab.IDENTITY_COMMANDS, *botlab.HELP_COMMANDS)}
    missing: list[str] = []
    for block in blocks:
        for raw in re.findall(r'"([^"]+)"', block):
            if raw.lower() not in known:
                missing.append(raw)
    assert not missing, f"模拟群认不出这些插件命令（补进 app/botlab.py）：{sorted(set(missing))}"


def test_help_text_lists_every_command_including_itself():
    """帮助里必须有**完整命令表**——包括「比赛帮助」自己（踩过：只列了查询类）。

    而且列出来的每条命令，模拟群都得认：否则用户照着帮助打，得到「没听懂」。
    """
    from app import helpcard_content as content

    text = botlab.help_text()
    assert "比赛帮助" in text, "帮助里得写清「再发一次比赛帮助」这条路"
    for title, rows in content.SECTIONS:
        assert title in text, f"少了分组：{title}"
        for cmd, _desc in rows:
            head = cmd.split()[0].strip("[]")
            assert head in text, f"帮助里少了命令：{cmd}"
            action, _value = botlab.parse(head)
            assert action != "unknown", f"帮助里列了「{cmd}」，但模拟群认不出来"
    assert "真实群" in text, "要说明「改数据的命令得在真群里用」"


# --------------------------------------------------------------------------- #
# 模拟群：接口
# --------------------------------------------------------------------------- #
@pytest.fixture
def debug_on(monkeypatch):
    monkeypatch.setenv("NTE_DEBUG", "1")


async def test_say_answers_with_the_same_text_as_preview(admin_client, debug_on):
    """模拟群里发「比赛结果」，回的文本要与预览接口**逐字一致**（同一条链路）。"""
    preview = await admin_client.get("/api/qqbot/preview", params={"kind": "result"})
    assert preview.status_code == 200, preview.text
    expected = preview.json()["parts"]

    res = await admin_client.post("/api/debug/bot/say", json={"text": "结果"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["action"] == "kind"
    assert body["user"]["role"] == "user" and body["user"]["parts"] == ["结果"]
    reply = body["replies"][0]
    assert reply["role"] == "bot"
    assert reply["parts"] == expected
    # 双方都进了缓冲（页面刷新后还在）
    assert [m["role"] for m in botlab.recent()][-2:] == ["user", "bot"]


async def test_say_help_and_unknown(admin_client, debug_on):
    helped = await admin_client.post("/api/debug/bot/say", json={"text": "帮助"})
    assert helped.status_code == 200
    assert "比赛" in helped.json()["replies"][0]["parts"][0]

    lost = await admin_client.post("/api/debug/bot/say", json={"text": "今天吃什么"})
    assert lost.json()["action"] == "unknown"
    assert "没听懂" in lost.json()["replies"][0]["parts"][0]


async def test_say_identity_command_explains_itself(admin_client, debug_on):
    """报名这类要真实 QQ 的命令：说清楚「为什么这里不行」，而不是装作成功了。"""
    res = await admin_client.post("/api/debug/bot/say", json={"text": "报名"})
    body = res.json()
    assert body["action"] == "identity"
    assert "真实群" in body["replies"][0]["parts"][0]


async def test_say_rejects_empty(admin_client, debug_on):
    res = await admin_client.post("/api/debug/bot/say", json={"text": "  "})
    assert res.status_code == 400


async def test_say_is_404_without_debug(admin_client, monkeypatch):
    monkeypatch.delenv("NTE_DEBUG", raising=False)
    res = await admin_client.post("/api/debug/bot/say", json={"text": "比赛"})
    assert res.status_code == 404

async def test_every_query_kind_can_be_answered(admin_client, debug_on):
    """**每种查询命令都得有回复**：模拟群里任何一种「看比赛」的命令都不该沉默或报错。"""
    one_per_kind: dict[str, str] = {}
    for alias, kind in botlab.QUERY_COMMANDS.items():
        one_per_kind.setdefault(kind, alias)
    for kind, alias in one_per_kind.items():
        res = await admin_client.post("/api/debug/bot/say", json={"text": alias})
        assert res.status_code == 200, f"{alias} → {kind} 失败：{res.text}"
        body = res.json()
        assert body["action"] == "kind", f"{alias} 没被认出来"
        reply = body["replies"][0]
        assert reply["parts"], f"{alias} → {kind} 回复是空的"

async def test_image_commands_attach_the_card(admin_client, debug_on):
    """能出图的命令，回复里**必须有图**——比赛信息 / 比赛结果 / 比赛（详情）。"""
    for text in ("比赛信息", "比赛结果", "比赛"):
        res = await admin_client.post("/api/debug/bot/say", json={"text": text})
        assert res.status_code == 200, res.text
        reply = res.json()["replies"][0]
        assert reply["image"], f"{text} 应当带卡片图"
        assert "/api/cards/" in reply["image"]["url"], reply["image"]["url"]


async def test_help_sends_the_help_image_when_present(admin_client, debug_on):
    """「比赛帮助」在真实群里是**先发帮助图**：模拟群也要给图（有图时）。"""
    from app import helpcard

    res = await admin_client.post("/api/debug/bot/say", json={"text": "比赛帮助"})
    assert res.status_code == 200
    reply = res.json()["replies"][0]
    assert reply["parts"], "命令表还是要给的"
    if helpcard.default_path().is_file():
        assert reply["image"] and reply["image"]["url"].endswith("/help.jpg")

