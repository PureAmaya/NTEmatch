"""AstrBot 插件里那几个纯函数与「框架交互约定」的测试。

插件模块依赖 ``astrbot`` 包，而它在站点环境里没有安装——所以这里**先打桩**再按路径
加载插件模块。打桩只覆盖插件用到的那几个符号，够用且不引入真实依赖。

这些测试盯的是**已经踩过的两个坑**，都属于「改回去也不会报错、但线上会失效」的类型：

1. 命令参数不能声明成 ``int``：AstrBot 的命令过滤器在参数有 int 默认值时会对实参做
   ``int(...)``，转不动会**抛异常**（不是忽略匹配），整条命令失效；
2. 配置不能在 ``__init__`` 里读死：不同版本注入配置的时机不一样，
   读死了会一直用默认值，所有命令都报「令牌不正确」。
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import types
from pathlib import Path

import httpx
import pytest

PLUGIN = (
    Path(__file__).resolve().parent.parent
    / "integrations"
    / "astrbot_plugin_nte_match"
    / "main.py"
)


def _install_astrbot_stub() -> None:
    """往 ``sys.modules`` 里塞一个最小的 astrbot 桩。"""
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _Star:
        def __init__(self, context=None, config=None):
            self.context = context
            if config is not None:
                self.config = config

    class _Filter:
        """装饰器只把函数原样返回（AstrBot 也是这么做的）。"""

        def __getattr__(self, _name):
            def register(*_args, **_kwargs):
                def wrap(func):
                    return func

                return wrap

            return register

    class _Event:
        pass

    class _MessageChain:
        """AstrBot 的消息链：这里只要求能把组件列表装进去（投递要断言的就是那份列表）。"""

        def __init__(self, chain=None):
            self.chain = list(chain or [])

    def _component(name):
        def factory(*args, **kwargs):
            return {"type": name, "args": args, "kwargs": kwargs}

        return factory

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.star = types.ModuleType("astrbot.api.star")
    api.star.Star = _Star
    api.star.Context = object
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = _Event
    event.filter = _Filter()
    event.MessageChain = _MessageChain
    comps = types.ModuleType("astrbot.api.message_components")
    comps.At = _component("at")
    comps.Plain = _component("plain")

    root = types.ModuleType("astrbot")
    root.api = api
    sys.modules.update(
        {
            "astrbot": root,
            "astrbot.api": api,
            "astrbot.api.star": api.star,
            "astrbot.api.event": event,
            "astrbot.api.message_components": comps,
        }
    )


@pytest.fixture(scope="module")
def plugin_module():
    _install_astrbot_stub()
    spec = importlib.util.spec_from_file_location("nte_match_plugin_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 1),
        ("1", 1),
        ("2", 2),
        ("第2页", 2),
        ("2 页", 2),
        ("第二页", 1),  # 中文数字解析不了：回第 1 页，而不是报错
        ("abc", 1),
        ("-3", 3),  # 只取数字：负数页码不存在
        ("999999", 999),  # 上限兜底
    ],
)
def test_page_number_is_lenient(plugin_module, raw, expected):
    assert plugin_module._page_number(raw) == expected


def test_list_page_param_must_not_be_int(plugin_module):
    """``比赛列表`` 的页码参数不能是 int：否则非数字参数会让命令抛异常。"""
    default = inspect.signature(plugin_module.NTEMatchPlugin.cmd_list).parameters["page"].default
    assert not isinstance(default, int), "页码参数又变回 int 了——非数字参数会让命令失效"
    assert isinstance(default, str)


def test_config_is_read_lazily(plugin_module):
    """实例化时没有配置也不该崩；框架后注入配置要能立刻生效。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    assert plugin.token == ""
    assert plugin.base_url == "http://127.0.0.1:8000"

    plugin.config = {"api_token": "nte_abc", "base_url": "https://match.test/"}
    assert plugin.token == "nte_abc"
    assert plugin.base_url == "https://match.test"  # 尾部斜杠被去掉


def test_config_shapes_are_tolerated(plugin_module):
    """AstrBot 不同版本会把配置值包成 ``{"value": ...}``，两种形态都得认。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"api_token": {"value": "nte_wrapped"}, "timeout": {"value": 3}}
    assert plugin.token == "nte_wrapped"
    assert plugin.timeout == 3


def test_help_text_explains_how_to_trigger(plugin_module):
    """帮助里必须写清「要先 @ 机器人或带唤醒前缀」——这是**唯一**的说明来源。"""
    text = plugin_module.HELP_TEXT
    assert "@ 机器人" in text
    assert "唤醒前缀" in text
    assert "比赛直播" in text  # 新命令别从帮助里掉出去


class _FakeContext:
    """只带真 @ 投递用到的那一个方法：发消息。"""

    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


async def test_delivery_builds_a_real_at_chain(plugin_module):
    """投递一条召集：链里是 ``At`` 组件（**真 @**）+ 正文，正文首行换行靠零宽空格保住。

    为什么盯这个换行：aiocqhttp 会把 ``Plain`` 的首尾空白去掉，直接拼 ``"\\n"`` 会被
    吃掉，@ 和正文就挤在同一行里（群里看着像一条没排版的乱句子）。
    """
    plugin = plugin_module.NTEMatchPlugin(context=None)
    ctx = _FakeContext()
    plugin.context = ctx

    ok, detail = await plugin._deliver(
        {
            "umo": "aiocqhttp:GroupMessage:123",
            "mentions": ["10001", "10002"],
            "body": "【NTE 比赛】集合啦！",
        }
    )
    assert ok is True and detail == ""
    umo, chain = ctx.sent[0]
    assert umo == "aiocqhttp:GroupMessage:123"
    comps = getattr(chain, "chain", chain)
    assert comps[0] == {"type": "at", "args": (), "kwargs": {"qq": "10001"}}
    assert comps[1]["kwargs"]["qq"] == "10002"
    assert comps[2]["type"] == "plain"
    assert comps[2]["kwargs"]["text"].startswith("\u200b\n")
    assert "集合啦！" in comps[2]["kwargs"]["text"]


async def test_delivery_without_a_session_reports_why(plugin_module):
    """没有目标会话：回报失败（站点会据此退回文本写法），绝不静默丢掉。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.context = _FakeContext()
    ok, detail = await plugin._deliver({"mentions": ["10001"], "body": "集合啦！"})
    assert ok is False and "目标会话" in detail


async def test_delivery_failure_is_reported_to_the_site(plugin_module):
    """``send_message`` 抛异常：回报失败——站点据此**立刻**退回文本写法重发。"""

    class _Boom(_FakeContext):
        async def send_message(self, umo, chain):
            raise RuntimeError("这个会话发不出去")

    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.context = _Boom()
    ok, detail = await plugin._deliver(
        {"umo": "aiocqhttp:GroupMessage:123", "mentions": ["10001"], "body": "x"}
    )
    assert ok is False and "发不出去" in detail


async def test_outbox_once_acks_every_item(plugin_module):
    """取一轮件：逐条发 + **逐条回执**（回执里带上成没成、为什么）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.context = _FakeContext()
    acks: list[dict] = []

    async def fake_get(path, **params):
        assert path == "outbox"
        return {
            "ok": True,
            "items": [
                {"id": "n1", "umo": "g:1", "mentions": ["10001"], "body": "第一条"},
                {"id": "n2", "umo": "", "mentions": ["10002"], "body": "没有会话"},
            ],
        }

    async def fake_post(path, payload):
        acks.append({"path": path, **payload})
        return {"ok": True}

    plugin._get = fake_get
    plugin._post = fake_post

    assert await plugin._outbox_once() == 1
    assert [ack["id"] for ack in acks] == ["n1", "n2"]
    assert [ack["ok"] for ack in acks] == [True, False]
    assert {ack["path"] for ack in acks} == {"outbox/ack"}


async def test_outbox_loop_can_be_switched_off(plugin_module):
    """配置里关掉投递：加载时不起取件任务（站点那边就只能把 @ 写进文本）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"outbox_enabled": False}

    async def fake_get(path, **params):
        return {"ok": True, "currentEvent": {"name": "x"}, "eventCount": 1}

    plugin._get = fake_get
    await plugin.initialize()
    assert plugin._outbox_task is None
    await plugin.terminate()


class _FakeEvent:
    """只带插件真正用到的那几个方法的假事件。"""

    def __init__(self, sender="", group="", message_obj=None):
        self._sender = sender
        self._group = group
        self.message_obj = message_obj

    def get_sender_id(self):
        if not self._sender:
            raise AttributeError("老版本没有这个方法")
        return self._sender

    def get_group_id(self):
        return self._group

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def image_result(self, image):
        return {"type": "image", "image": image}


def test_sender_id_falls_back_to_message_obj(plugin_module):
    """取不到 get_sender_id 时回落到 message_obj.sender.user_id（老版本兼容）。"""

    class _Sender:
        user_id = "20002"

    class _Obj:
        sender = _Sender()

    assert plugin_module._sender_id(_FakeEvent(sender="10001")) == "10001"
    assert plugin_module._sender_id(_FakeEvent(message_obj=_Obj())) == "20002"
    assert plugin_module._sender_id(_FakeEvent()) == ""


class _At:
    """消息链里的 At 片段（`_at_targets` 只读它的 ``qq``）。"""

    def __init__(self, qq):
        self.qq = qq


class _Msg:
    def __init__(self, *segs):
        self.message = list(segs)


def test_stream_key_arg_ignores_qq_like_args(plugin_module):
    """@ 人时混进来的纯数字不是流名；其余原样交给站点校验（错字要当场报错）。"""
    parse = plugin_module._stream_key_arg
    assert parse("tom") == "tom"
    assert parse("", "") == ""
    assert parse("10001") == "", "纯数字是 QQ（@ 某人时平台会把它当参数传进来）"
    assert parse("10001", "tom") == "tom"
    assert parse("tom-1_2") == "tom-1_2"
    assert parse("中文流名") == "中文流名", "不在这里挑字符——让站点给一句清楚的报错"


async def test_stream_setup_points_to_private_chat(plugin_module):
    """「比赛直播注册」：把流名与 @ 目标交给站点，群里只说「去私聊查收」。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    calls: list[tuple] = []

    async def fake_post(path, payload=None):
        calls.append((path, payload))
        return {
            "ok": True,
            "sent": True,
            "streamId": "tom",
            "created": ["推流码 tom", "一把新的直播令牌"],
            "note": "直播注册完成：推流码 tom、一把新的直播令牌",
        }

    plugin._post = fake_post
    out = await _collect(
        plugin.cmd_stream_setup(_FakeEvent(sender="10001", group="g1"), "tom", "")
    )
    assert calls == [("stream-setup", {"qq": "10001", "targetQq": "", "streamKey": "tom"})]
    assert "私聊" in out[0]["text"]


async def test_stream_setup_admin_mentions_the_target(plugin_module):
    """管理员 @ 某人代办：群里点名说「发给他本人了」，绝不带任何明文。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    seen: dict = {}

    async def fake_post(path, payload=None):
        seen.update(payload)
        return {"ok": True, "sent": True, "forOther": True, "name": "张三", "note": "直播注册完成"}

    plugin._post = fake_post
    event = _FakeEvent(sender="10001", group="g1", message_obj=_Msg(_At("10002")))
    out = await _collect(plugin.cmd_stream_setup(event, "", ""))
    assert seen["targetQq"] == "10002"
    assert seen["streamKey"] == ""
    assert "TA" in out[0]["text"]


async def test_uid_answers_in_the_group(plugin_module):
    """游戏 UUID：群内直接回（与命令说明、推流地址不同，它不用私聊）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    seen: dict = {}

    async def fake_get(path, **params):
        seen["path"] = path
        seen.update(params)
        return {"ok": True, "parts": ["【NTE 比赛】张三 的游戏 UUID\nUUID-1"]}

    plugin._get = fake_get
    event = _FakeEvent(sender="10001", group="g1", message_obj=_Msg(_At("10002")))
    out = await _collect(plugin.cmd_uid(event))
    assert seen["path"] == "uid" and seen["qq"] == "10001" and seen["targetQq"] == "10002"
    assert out[0]["type"] == "plain" and "UUID-1" in out[0]["text"]


async def test_profile_view_goes_private_to_the_right_person(plugin_module):
    """资料查看：私聊发给**站点指认的那个人**（管理员代办时不是自己）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    private: list[tuple] = []

    async def fake_get(path, **params):
        return {"ok": True, "parts": ["资料全文"], "toQq": "10002", "forOther": True}

    async def fake_notify(qq, text):
        private.append((qq, text))
        return {"ok": True}

    plugin._get = fake_get
    plugin._notify = fake_notify
    event = _FakeEvent(sender="10001", group="g1", message_obj=_Msg(_At("10002")))
    out = await _collect(plugin.cmd_profile(event))
    assert private == [("10002", "资料全文")]
    assert "TA" in out[0]["text"]
    assert "资料全文" not in out[0]["text"], "群里只留一句指引"


async def test_profile_edit_sends_field_and_value(plugin_module):
    """改资料：字段与值原样交给站点（容错表在站点那一侧，插件不自己认）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    seen: dict = {}

    async def fake_post(path, payload=None):
        seen["path"] = path
        seen.update(payload or {})
        return {"ok": True, "parts": ["新的资料"], "toQq": "10001", "changed": ["名字 → 新名字"]}

    async def fake_notify(qq, text):
        return {"ok": True}

    plugin._post = fake_post
    plugin._notify = fake_notify
    out = await _collect(plugin.cmd_profile(_FakeEvent(sender="10001", group="g1"), "名字", "新名字"))
    assert seen["path"] == "profile"
    assert seen["field"] == "名字" and seen["value"] == "新名字"
    assert "已改" in out[0]["text"]


async def test_profile_falls_back_to_the_group_for_yourself(plugin_module):
    """私聊发不出去时：**自己**的资料可以回群里（里面没有敏感内容），别人的不行。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_get(path, **params):
        return {"ok": True, "parts": ["我的资料全文"], "toQq": "10001"}

    async def fail_notify(qq, text):
        return {"ok": False, "error": "未加机器人好友"}

    plugin._get = fake_get
    plugin._notify = fail_notify
    out = await _collect(plugin.cmd_profile(_FakeEvent(sender="10001", group="g1")))
    assert "我的资料全文" in out[0]["text"]
    assert "私聊没发出去" in out[0]["text"]


async def test_credential_commands_send_you_to_private_chat(plugin_module):
    """三条自助命令都只调站点的 ``/credential``，群里只说「去私聊查收」。

    **新值不在插件手里**：站点把密钥 / 令牌直接私聊给本人，响应里本来就没有明文
    （见 ``app/bot_api.py``），所以插件这一侧连「不小心打进群」的机会都没有。
    """
    plugin = plugin_module.NTEMatchPlugin(context=None)
    calls: list[tuple] = []

    async def fake_post(path, payload=None):
        calls.append((path, payload))
        return {"ok": True, "kind": "key", "sent": True, "note": "登录密钥已重置"}

    plugin._post = fake_post
    out = [item async for item in plugin.cmd_rotate_key(_FakeEvent(sender="10001", group="g1"))]
    assert calls == [
        ("credential", {"qq": "10001", "targetQq": "", "what": "key", "value": ""})
    ]
    assert out[0]["type"] == "plain" and "私聊" in out[0]["text"]

    await _collect(plugin.cmd_rotate_token(_FakeEvent(sender="10001")))
    assert calls[-1][1]["what"] == "token"

    await _collect(plugin.cmd_set_stream_key(_FakeEvent(sender="10001"), "tom"))
    assert calls[-1][1] == {"qq": "10001", "targetQq": "", "what": "streamId", "value": "tom"}


async def _collect(gen):
    """把异步生成器跑完，返回它 yield 出来的结果。"""
    return [item async for item in gen]


async def test_stream_key_command_explains_usage_without_value(plugin_module):
    """没给流名时直接说用法（不白跑一趟站点）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_post(path, payload=None):
        raise AssertionError("没给流名时不该调站点")

    plugin._post = fake_post
    out = await _collect(plugin.cmd_set_stream_key(_FakeEvent(sender="10001")))
    assert "用法" in out[0]["text"] and "改推流码" in out[0]["text"]


async def test_credential_failure_keeps_the_change_honest(plugin_module):
    """私聊没发出去时要讲清「已经换好了、只是你没收到」——否则他会反复重试，每试一次作废一把。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_post(path, payload=None):
        return {"ok": True, "sent": False, "detail": "Bot not found", "note": "直播令牌已重置"}

    plugin._post = fake_post
    out = await _collect(plugin.cmd_rotate_token(_FakeEvent(sender="10001")))
    text = out[0]["text"]
    assert "已重置" in text and "私聊" in text and "再发一次" in text


async def test_grant_points_to_self_service_when_key_cannot_be_sent(plugin_module):
    """站点没能把新成员密钥私聊出去时，群里给出「他自己重置换新」的办法。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_post(path, payload=None):
        return {"ok": True, "created": True, "name": "张三", "keySent": False, "detail": "Bot not found"}

    plugin._post = fake_post
    event = _FakeEvent(sender="10001", group="g1", message_obj=_Msg(_At("10002")))
    out = await plugin._grant(event, "member")  # _grant 返回单条结果（不是生成器）
    assert "比赛重置密钥" in out["text"]
    assert "张三" in out["text"]


async def test_grant_confirms_private_delivery(plugin_module):
    """站点把密钥私聊出去之后，群里只说「已私聊发给 TA」——不带任何明文。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_post(path, payload=None):
        return {"ok": True, "created": True, "name": "张三", "keySent": True}

    plugin._post = fake_post
    event = _FakeEvent(sender="10001", group="g1", message_obj=_Msg(_At("10002")))
    out = await plugin._grant(event, "event_admin")
    assert "赛事管理员" in out["text"] and "私聊" in out["text"]


def test_call_cooldown_blocks_repeat(plugin_module):
    """召集：同一会话立刻再发要被挡；换个会话不受影响。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"call_cooldown": 60, "call_per_hour": 5}

    ok, reason = plugin._call_allowed(_FakeEvent(group="g1"))
    assert ok is True and reason == ""

    ok, reason = plugin._call_allowed(_FakeEvent(group="g1"))
    assert ok is False
    assert "秒后再试" in reason

    ok, _ = plugin._call_allowed(_FakeEvent(group="g2"))  # 另一个群各自计
    assert ok is True


def test_call_hourly_cap(plugin_module):
    """冷却设为 0 时，仍受每小时上限约束。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"call_cooldown": 0, "call_per_hour": 2}

    assert plugin._call_allowed(_FakeEvent(group="g1"))[0] is True
    assert plugin._call_allowed(_FakeEvent(group="g1"))[0] is True
    ok, reason = plugin._call_allowed(_FakeEvent(group="g1"))
    assert ok is False
    assert "一小时内" in reason


def test_call_gate_can_be_disabled(plugin_module):
    """两个上限都设 0 = 关掉闸门（自建小群想随便召集也留了口子）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"call_cooldown": 0, "call_per_hour": 0}
    for _ in range(5):
        assert plugin._call_allowed(_FakeEvent(group="g1"))[0] is True


def test_call_denied_text_names_the_creator_without_qq(plugin_module):
    """名单为空时要说清「创建者没登记 QQ」，而不是含糊地说「你没权限」。

    这是最容易卡死的那一种：届是某位赛事管理员建的，但他没在站点「我的」页填 QQ，
    于是**谁都召集不了**（连他自己也不知道该去补 QQ）——旧文案只说「需要赛事管理员身份」，
    他明明是赛事管理员，看完只会更糊涂。
    """
    text = plugin_module._call_denied_text(
        {"qqs": [], "owner": {"name": "小队长", "hasQq": False}, "note": ""}
    )
    assert "小队长" in text
    assert "QQ" in text


def test_call_denied_text_points_at_my_own_events(plugin_module):
    """被拒但自己建过届：直接给出届次与可用的命令（他多半只是写错了届次）。

    规则是「谁创建的届谁可以召集」，所以赛事管理员能召集的是**自己创建的那些届**——
    提示里必须把这句话写出来，否则他会以为站点不认他的赛事管理员身份。
    """
    text = plugin_module._call_denied_text(
        {
            "qqs": ["20002"],
            "owner": {"name": "服管", "hasQq": True},
            "mine": [{"id": "e005", "name": "小队长杯"}],
        }
    )
    assert "e005" in text and "比赛召集" in text
    assert "**" not in text, "发到群里的是纯文本，别夹 Markdown 记号"


def test_call_denied_text_guides_when_i_created_nothing(plugin_module):
    """自己一届都没建过：告诉他「建一届就能召集」以及可能是 QQ 没登记。"""
    text = plugin_module._call_denied_text(
        {"qqs": ["20002"], "owner": {"name": "服管", "hasQq": True}, "mine": []}
    )
    assert "QQ" in text
    assert "**" not in text


def test_ids_args_parse_scope_and_page(plugin_module):
    """「比赛届次」的参数：认「我的」与页码，顺序随意，认不出的词一律忽略。"""
    parse = plugin_module._ids_args
    assert parse() == ("all", 1)
    assert parse("我的") == ("mine", 1)
    assert parse("我的", "2") == ("mine", 2)
    assert parse("2", "我的") == ("mine", 2)  # 顺序随意，别逼用户记顺序
    assert parse("第2页") == ("all", 2)
    assert parse("全部", "3") == ("all", 3)
    assert parse("随便写的") == ("all", 1)  # 认不出就当没写，别把人挡在命令外面


async def test_ids_go_private_when_multiple_pages(plugin_module):
    """一页装不下 → 私聊发本人，群里只留一句指引（明细不进群，免得刷屏）。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    seen: dict = {}

    async def fake_query(kind, **kwargs):
        seen["kind"] = kind
        seen.update(kwargs)
        return {"ok": True, "page": 1, "pages": 3, "parts": ["第 1 页明细"]}

    async def fake_notify(qq, text):
        seen["private"] = (qq, text)
        return {"ok": True}

    plugin._query = fake_query
    plugin._notify = fake_notify
    out = [r async for r in plugin.cmd_ids(_FakeEvent(sender="10001", group="g1"), "我的", "2")]

    assert seen["kind"] == "ids"
    assert seen["scope"] == "mine" and seen["page"] == 2
    assert seen["qq"] == "10001"  # 「我的」靠这个 QQ 认人，站点侧过滤
    assert seen["private"][0] == "10001" and "第 1 页明细" in seen["private"][1]
    assert len(out) == 1
    assert "私聊" in out[0]["text"]
    assert "比赛届次 我的 2" in out[0]["text"]  # 下一页怎么写，要写真实可用的
    assert "第 1 页明细" not in out[0]["text"]


async def test_ids_stay_in_group_when_single_page(plugin_module):
    """一页装得下（≤10 届）→ 照旧在群里回，不折腾私聊。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_query(kind, **kwargs):
        return {"ok": True, "page": 1, "pages": 1, "parts": ["共 3 届（第 1 / 1 页）"]}

    async def fake_notify(qq, text):
        raise AssertionError("只有一页时不该走私聊")

    plugin._query = fake_query
    plugin._notify = fake_notify
    out = [r async for r in plugin.cmd_ids(_FakeEvent(sender="10001", group="g1"))]
    assert [r["text"] for r in out] == ["共 3 届（第 1 / 1 页）"]


async def test_event_commands_require_an_explicit_event(plugin_module):
    """不写届次 → 直接提示「写明哪一届」，**不再默默套用服务器那个指针**。

    站点内部确实有「当前届」（谁最近打开 / 新建过就是谁），但它只是实现细节，
    不该当用户可见的默认值：否则同一句话今天问是这届、明天问变成那届。
    """
    plugin = plugin_module.NTEMatchPlugin(context=None)
    for raw in ("", "   "):
        target, error = await plugin._resolve_event(raw)
        assert target == ""
        assert error, "不写届次时必须给提示，不能静默回退"
        assert "哪一届" in error
        assert "比赛届次" in error  # 提示里要告诉用户「怎么知道有哪些届」


def test_help_text_explains_how_to_join(plugin_module):
    """帮助里要有「怎么参加」——群里最常问的就是这个，答不上就没人接着问了。

    现在这一节的两条路都得写清：**自己报名**（筹备中发「比赛报名」）与
    **让管理员排名单**（白名单群里的新人也会被顺手建成成员）。
    """
    text = plugin_module.HELP_TEXT
    assert "怎么参加" in text
    assert "比赛报名" in text and "比赛取消报名" in text
    assert "筹备中" in text, "报名只在筹备中的届开放，这一条得写进帮助里"
    assert "不组队不定赛制" in text, "「报名只定名单」这件事最容易误会，必须写清"


def test_help_doc_lists_every_command(plugin_module):
    """`HELP.md`（做图底稿）与 `HELP_TEXT`（群里回的文本）都要提到每一条命令。

    这两处最容易漂移：加了命令却忘了同步，群里回的文字和帮助图就对不上。
    """
    source = PLUGIN.read_text(encoding="utf-8")
    commands = re.findall(r'@filter\.command\(\s*"([^"]+)"', source)
    assert len(commands) == 25, f"命令数变了（现在 {len(commands)} 条）：请同步 HELP.md 与 README"
    doc = (PLUGIN.parent / "HELP.md").read_text(encoding="utf-8")
    for name in commands:
        assert name in plugin_module.HELP_TEXT, f"HELP_TEXT 里缺命令：{name}"
        assert name in doc, f"HELP.md 里缺命令：{name}"


def _load_help_card_content():
    """读帮助图的内容模块（在 ``app/`` 里：**服务启动时会用它自动出图**）。"""
    from app import helpcard_content

    return helpcard_content


def test_help_card_content_has_no_markdown():
    """图只会画字：文案里出现 ``**加粗**`` 就会原样印出星号（踩过这个坑）。"""
    assert _load_help_card_content().markdown_leaks() == []


def test_help_card_artifact_matches_when_present():
    """帮助图**不入库**（服务启动时自动重画）；本地若有一份，它必须与文案同步。

    图上的命令群友会照着打（精确匹配，错一个字就是**毫无反应**），所以「改了文案忘了
    重画」必须有人喊出来——出图时把源文件指纹写进 ``static/help.jpg.src.sha256``，
    这里比对（``tools/check_assets.py`` 查的是同一条）。干净检出（没有图）是**正常状态**。
    """
    card = _load_help_card_content()
    root = Path(__file__).resolve().parent.parent
    art = root / "static" / "help.jpg"
    if not art.exists():
        pytest.skip("帮助图不入库：服务启动时会自动生成一份")
    stamp = art.with_name(art.name + ".src.sha256")
    assert stamp.exists(), "图在、指纹不在：重启一次服务，或跑 tools/make_help_card.py 重画"
    assert stamp.read_text(encoding="utf-8").strip() == card.source_digest(), (
        "帮助图比文案旧：重启一次服务会自动重画，或跑 tools/make_help_card.py"
    )


def test_help_card_renderer_builds_a_real_jpeg():
    """出图这条路径本身要能跑——图不再入库之后，它是产物的**唯一**来源。"""
    import io

    from PIL import Image

    from app import helpcard

    if not helpcard.available():
        pytest.skip("没装 Pillow（可选依赖）：站点会退回文字说明，不影响功能")
    data = helpcard.build()
    assert data[:2] == b"\xff\xd8", "JPEG 的 SOI 记号（画出来得是一张真图）"
    with Image.open(io.BytesIO(data)) as img:
        assert img.width == helpcard.W
        assert 1200 < img.height < helpcard.CANVAS_H


def test_help_card_text_never_overlaps_or_overflows(monkeypatch):
    """图上的字**不许互相压**，也不许冲出安全边距。

    补这条的原因：以前只查「有没有画到图片外」，而卡片的说明被画在了**排版循环残留**的
    横坐标上——同一行带里两笔字压在一起，右边缘一点没越界：检查全绿，眼睛一眼看得出来。
    """
    from PIL import ImageDraw

    from app import helpcard

    if not helpcard.available():
        pytest.skip("没装 Pillow：帮助图本身就不生成")

    draws: list[tuple[float, float, float, float, str, tuple]] = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        font = kwargs.get("font")
        width = float(font.getlength(text)) if font is not None else 0.0
        size = float(getattr(font, "size", 0) or 0)
        fill = kwargs.get("fill") or ()
        draws.append((float(xy[0]), float(xy[1]), width, size, str(text), tuple(fill)))
        return real(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    helpcard.render()

    # ① 不许冲出安全边距（右边刚好留出 PAD）
    limit = helpcard.W - helpcard.PAD + 1
    over = [text for x, _y, w, _s, text, _f in draws if x + w > limit]
    assert not over, f"有文字冲出安全边距：{over[:3]}"

    # ② 同一行带里，说明必须**从命令右边**起画（不是压在命令上）
    accent, dim = helpcard.ACCENT, helpcard.DIM
    problems: list[str] = []
    inline_rows = 0
    for x, y, _w, _size, text, fill in draws:
        if not text.startswith("——") or fill[:3] != dim:
            continue
        for cx, cy, cw, _cs, ctext, cfill in draws:
            if cfill[:3] != accent or abs(cy - y) > 18:
                continue  # 换行到下一行起画的说明：这一行带里没有它的命令，正常
            inline_rows += 1
            if x < cx + cw:
                problems.append(f"「{ctext}」的说明压在命令上（x={x:.0f} < {cx + cw:.0f}）：{text[:20]}")
    assert not problems, "；".join(problems[:3])
    assert inline_rows >= 5, "一条「同行说明」都没量到：检查本身失效了（排版改了要同步这里）"


def test_help_card_lists_exactly_the_same_commands(plugin_module):
    """帮助图上的命令必须与 `HELP_TEXT` **完全一致**。

    图是代码渲染的（`app/helpcard.py`，文字在 `app/helpcard_content.py`），
    所以两边能对得上；这条用例盯住「插件加了命令忘了画」或「图上写错一条」——
    图上的命令错一个字，群友照着打就是**毫无反应**（命令是精确匹配的，没有兜底）。
    """
    card = _load_help_card_content()
    on_card = {cmd.split()[0] for _title, rows in card.SECTIONS for cmd, _desc in rows}
    in_text = {
        line.strip()[2:].split()[0]
        for line in plugin_module.HELP_TEXT.splitlines()
        if line.strip().startswith("· ")
    }
    assert on_card == in_text, (
        f"图上少了 {sorted(in_text - on_card)}；图上多了 {sorted(on_card - in_text)}"
    )


# --------------------------------------------------------------------------- #
# LLM 工具（可选）：让大模型也能查这些数据
#
# 这一节盯的是「工具会不会反过来影响人格」。最容易踩的三件事：
# 1. 工具自己往群里发消息 —— 那等于绕开人格发言，还会与模型的话重复一遍；
# 2. 工具去改系统提示词 / 人格设定 —— 用户配的人格被悄悄换掉；
# 3. 参数的 docstring 不合 AstrBot 的规矩：轻则参数被静默丢掉（模型传了也没用），
#    重则**注册时**抛异常、插件整个加载不了（连命令一起没）。
# 2 与 3 在本地都不会报错，只能靠这几条测试拦。
# --------------------------------------------------------------------------- #
_LLM_TOOL_TYPES = {"string", "number", "boolean", "object", "array"}

#: 允许暴露给大模型的工具（**只有这四个**）：查询、报名 / 取消报名、自己的资料、帮助。
#: 凭据类（推流地址 / 重置密钥 / 重置令牌 / 改推流码 / 直播注册）与「召集」永远不给工具：
#: 工具结果是回给大模型的，而它会照着重述到群里。
_LLM_TOOL_NAMES = {"nte_query", "nte_signup", "nte_me", "nte_help"}


def _llm_tools_in_source() -> dict:
    """从源码里找出所有 ``@_llm_tool("…")`` 的方法（返回 ``{工具名: 函数节点}``）。

    读源码而不是读加载后的类：测试用的 astrbot 桩把装饰器做成了「原样返回」，
    运行时根本看不出哪些方法是工具。
    """
    import ast

    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    out: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Name)
                and deco.func.id == "_llm_tool"
            ):
                continue
            if deco.args and isinstance(deco.args[0], ast.Constant):
                out[str(deco.args[0].value)] = node
    return out


def _parse_args_section(doc: str) -> dict:
    """按 AstrBot 的规矩读 docstring 的 ``Args:`` 段（``参数名(类型): 说明``）。

    AstrBot 用 ``docstring_parser`` 解析这一段、且**只看这段**（不看类型注解）：类型不在
    白名单里、或者压根没写类型，注册时就会抛异常。缺类型的参数这里读成空串，
    测试据此报错——比等插件加载失败再回来查快得多。
    """
    lines = inspect.cleandoc(doc or "").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Args:")
    except StopIteration:
        return {}
    out: dict = {}
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):  # 缩进回到顶格：Args 段结束了
            break
        hit = re.match(r"\s*([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*:", line)
        if hit:
            out[hit.group(1)] = (hit.group(2) or "").strip()
    return out


def test_llm_tool_set_is_deliberate(plugin_module):
    """只暴露这四个工具：多一个都得先改这条测试（凭据与召集永远不给模型）。"""
    tools = _llm_tools_in_source()
    assert set(tools) == _LLM_TOOL_NAMES, (
        f"工具集合变了（现在 {sorted(tools)}）：确认它不会把凭据或「召集」交给大模型，再改这里"
    )


def test_llm_tool_docstrings_match_astrbot_rules(plugin_module):
    """每个工具的 docstring 都得让 AstrBot 解析出**正确的参数**。

    AstrBot 的工具参数**只**来自 docstring：参数名对不上 → 模型传的参数进不了函数；
    类型没写 / 不在白名单 → 注册时直接抛异常（插件加载失败，命令一起没）。
    另外每个参数必须有默认值：模型少传一个参数时不能把调用变成 TypeError。
    """
    for name, node in _llm_tools_in_source().items():
        doc = _docstring_of(node)
        assert doc.strip(), f"{name} 没有 docstring：工具描述为空的话，模型不知道它干什么"
        args = _parse_args_section(doc)
        params = [a.arg for a in node.args.args if a.arg not in ("self", "event")]
        assert set(args) == set(params), (
            f"{name} 的 docstring 参数与函数签名对不上：docstring={sorted(args)} 签名={params}"
        )
        assert "event" not in args and "self" not in args, f"{name} 不该把 event/self 写进 Args"
        for param, type_name in args.items():
            assert type_name in _LLM_TOOL_TYPES, (
                f"{name}.{param} 的类型「{type_name}」AstrBot 不认"
                f"（只能是 {' / '.join(sorted(_LLM_TOOL_TYPES))}）；写漏类型会更惨：注册时就抛异常"
            )
        defaults = node.args.defaults + node.args.kw_defaults
        assert len(defaults) == len(params), f"{name} 的参数都要有默认值：模型可能少传"


def _docstring_of(node) -> str:
    """函数节点的 docstring（第一个语句是字符串常量的话）。"""
    import ast

    first = node.body[0] if node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return str(first.value.value or "")
    return ""


def test_llm_tools_never_speak_or_touch_the_persona(plugin_module):
    """工具**只回文本**：不许自己发消息，也不许碰系统提示词 / 人格设定。

    工具的结果是作为「工具返回」交给大模型的，最终那句话由模型按**当前人格**说出来。
    工具一旦自己发言（yield / plain_result / 私聊），就会出现「人格之外的第二张嘴」，
    用户看到的是两句重复的话；去改 prompt / persona 就更严重：人格被悄悄换掉。
    """
    import ast

    banned_calls = {"plain_result", "chain_result", "image_result", "_notify", "stop_event", "send"}
    for name, node in _llm_tools_in_source().items():
        assert not any(isinstance(inner, ast.Yield) for inner in ast.walk(node)), (
            f"{name} 里有 yield：工具的产出会被当成「发给用户的消息」，绕开人格"
        )
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                assert inner.func.attr not in banned_calls, f"{name} 调了 {inner.func.attr}：工具不许自己发言"
        annotation = ast.unparse(node.returns) if node.returns is not None else ""
        assert annotation == "str", f"{name} 的返回类型要标成 str（工具结果回给大模型，不发群）"

    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    for inner in ast.walk(tree):
        if not isinstance(inner, ast.Attribute):
            continue
        assert "persona" not in inner.attr.lower(), f"插件动了人格相关的东西：{inner.attr}"
        assert inner.attr != "on_llm_request", "插件改了 LLM 请求：人格 / 系统提示词不在插件的管辖内"


async def test_llm_tools_return_plain_text(plugin_module):
    """跑一遍四个工具：回的是**纯文本**，而且不带回复前缀（那是给群里看的装饰）。"""
    calls: list = []
    plugin = plugin_module.NTEMatchPlugin(context=None, config={"reply_prefix": "[NTE]"})

    async def fake_resolve(token):
        return ("e001", "") if token else ("", "要写明是「哪一届」")

    async def fake_query(kind, event_id="", ref="", page=1, at=True, scope="", qq=""):
        calls.append(("query", kind, event_id, ref, page, at, scope, qq))
        return {"ok": True, "parts": ["甲 3:1 乙"], "text": "甲 3:1 乙"}

    async def fake_get(path, **params):
        calls.append(("get", path, params))
        return {"ok": True, "parts": ["资料：名字 甲"], "text": "资料：名字 甲"}

    async def fake_post(path, payload):
        calls.append(("post", path, payload))
        if path == "profile":
            return {"ok": True, "parts": ["资料已更新"], "text": "资料已更新", "changed": ["名字"]}
        return {"ok": True, "text": "已报名：甲届（e001）"}

    prompt_used = []

    async def fake_notify(*_a, **_kw):  # 工具调用它 = 自己发言，直接判失败
        prompt_used.append(1)
        raise AssertionError("工具不许私聊发消息")

    plugin._resolve_event = fake_resolve
    plugin._query = fake_query
    plugin._get = fake_get
    plugin._post = fake_post
    plugin._notify = fake_notify
    event = _FakeEvent(sender="10001", group="900001")

    got = await plugin.tool_query(event, "progress", "甲届")
    assert got == "甲 3:1 乙", f"工具回的应当是站点原文、不带回复前缀：{got!r}"
    assert calls[-1] == ("query", "progress", "e001", "", 1, False, "all", ""), (
        "工具查询要带 at=False（群里那套 @ 片段对模型没用）"
    )

    assert "kind 只能是" in await plugin.tool_query(event, "帮我编个比分")
    assert calls[-1][0] == "query", "非法 kind 不该再去打扰站点"

    await plugin.tool_query(event, "ids", "", "", 2, True)
    assert calls[-1] == ("query", "ids", "", "", 2, False, "mine", "10001"), (
        "「只看我创建的届」要带上发消息那个人的 QQ（站点按它过滤）"
    )

    assert await plugin.tool_signup(event, "甲届", "join") == "已报名：甲届（e001）"
    assert calls[-1] == (
        "post",
        "signup",
        {"qq": "10001", "action": "join", "event": "e001", "group": "900001", "name": ""},
    ), "报名工具与命令走同一条接口、同一份载荷（含群号：白名单按它判）"
    assert "action 只能是" in await plugin.tool_signup(event, "甲届", "带我飞")

    assert (await plugin.tool_me(event, "名字", "新名字")).startswith("已改：名字")
    assert calls[-1] == (
        "post",
        "profile",
        {"qq": "10001", "targetQq": "", "field": "名字", "value": "新名字"},
    )
    assert await plugin.tool_me(event) == "资料：名字 甲"
    assert calls[-1] == ("get", "profile", {"qq": "10001", "targetQq": ""})

    assert await plugin.tool_help(event) == plugin_module.HELP_TEXT
    assert not prompt_used


def test_llm_tool_kinds_follow_the_site(plugin_module):
    """工具能查的类型 = 站点 ``KIND_META`` **减去 call**。

    站点加了新查询类型而插件没跟上 → 工具会天天回「kind 只能是 …」；
    反过来，多给了模型一个类型也要在这里说明理由（默认全给是不行的，
    ``call`` 会 @ 一大片人，只能人下命令）。
    """
    from app import qqbot

    kinds = set(qqbot.KIND_META)
    allowed = set(plugin_module._AGENT_KINDS)
    assert kinds - allowed == {"call"}, (
        f"工具的查询类型与站点对不上：站点多出来 {sorted(kinds - allowed)}、"
        f"插件多出来 {sorted(allowed - kinds)}（见 README「LLM 工具」）"
    )
    doc = _docstring_of(_llm_tools_in_source()["nte_query"])
    for kind in allowed:
        assert kind in doc, f"工具描述里没写 kind={kind}：模型不会用没介绍过的类型"


def test_llm_tool_decorator_degrades_on_old_astrbot(monkeypatch, plugin_module):
    """老版本 AstrBot 没有 ``filter.llm_tool`` 时**不注册工具**，但插件要能正常加载。

    直接写 ``@filter.llm_tool(...)`` 的话，老版本上装饰器求值就 AttributeError，
    插件整个加载不了——连 25 条命令一起没。所以这里必须能退化成「原样返回函数」。
    """
    monkeypatch.setattr(plugin_module.filter, "llm_tool", None, raising=False)

    def sample():
        return "ok"

    assert plugin_module._llm_tool("nte_x")(sample) is sample


class _OldEvent(_FakeEvent):
    """老版本 AstrBot 的 event：没有 ``image_result`` 方法。"""

    image_result = None


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """只实现 ``head`` 的假 HTTP 客户端：记下探测过哪些地址，回一个固定状态码。"""

    def __init__(self, status_code):
        self.status_code = status_code
        self.calls: list[str] = []

    async def head(self, url, timeout=None):
        self.calls.append(url)
        return _FakeResponse(self.status_code)


async def _help_output(plugin, event):
    """跑一次 ``比赛帮助``，把 yield 出来的结果收集起来。"""
    return [item async for item in plugin.cmd_help(event)]


async def test_help_sends_the_image_when_configured(plugin_module):
    """配了帮助图就**当场发图**：发在当前会话（图是给人看和转发的），也不经过站点。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"help_image": "https://example.com/nte-help.png"}
    out = await _help_output(plugin, _FakeEvent(sender="10001", group="g1"))
    assert out == [{"type": "image", "image": "https://example.com/nte-help.png"}]


async def test_help_falls_back_to_cq_image_on_old_framework(plugin_module):
    """框架没有 ``image_result`` 时退化成 CQ 码文本——最坏也只是「带链接的消息」。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"help_image": "/srv/nte/help.png"}
    out = await _help_output(plugin, _OldEvent(sender="10001", group="g1"))
    assert out[0]["type"] == "plain"
    assert "[CQ:image,file=/srv/nte/help.png]" in out[0]["text"]


async def test_help_configured_image_skips_the_probe(plugin_module):
    """显式配了 `help_image` 就照发，**不再去探站点**——自定义地址不替你把关。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"help_image": "https://cdn.test/nte-help.png"}
    client = _FakeClient(200)
    plugin._http = lambda: client
    out = await _help_output(plugin, _FakeEvent(sender="10001", group="g1"))
    assert out == [{"type": "image", "image": "https://cdn.test/nte-help.png"}]
    assert client.calls == []  # 一次都没探测


async def test_help_defaults_to_the_site_image(plugin_module):
    """没配 `help_image` 时默认用**站点内置那张**：`{站点地址}/help.jpg`，配都不用配。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"base_url": "https://match.test"}
    client = _FakeClient(200)
    plugin._http = lambda: client
    out = await _help_output(plugin, _FakeEvent(sender="10001", group="g1"))
    assert out == [{"type": "image", "image": "https://match.test/help.jpg"}]
    assert client.calls == ["https://match.test/help.jpg"]


async def test_help_falls_back_to_text_when_site_has_no_image(plugin_module):
    """站点还没放图（探测回 404）：回文字说明——绝不能发一张加载失败的破图。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {"base_url": "https://match.test"}
    plugin._http = lambda: _FakeClient(404)
    out = await _help_output(plugin, _FakeEvent())  # 取不到 sender → 直接群内回文本
    assert out == [{"type": "plain", "text": plugin_module.HELP_TEXT}]


async def test_help_survives_site_probe_failure(plugin_module):
    """探测站点都失败（站点没起 / 网络不通）也按「没有图」处理，不抛异常把命令打挂。"""

    class _Boom:
        async def head(self, url, timeout=None):
            raise httpx.ConnectError("connection refused")

    plugin = plugin_module.NTEMatchPlugin(context=None)
    plugin.config = {}
    plugin._http = _Boom
    out = await _help_output(plugin, _FakeEvent())
    assert out == [{"type": "plain", "text": plugin_module.HELP_TEXT}]


# --------------------------------------------------------------------------- #
# 自助报名（比赛报名 / 比赛取消报名）
# --------------------------------------------------------------------------- #
def _signup_plugin(plugin_module, calls: list[tuple], resolved: str = "e001"):
    """造一个「届次已解析、站点调用被记下来」的插件实例。"""
    plugin = plugin_module.NTEMatchPlugin(context=None)

    async def fake_resolve(token):
        assert token == "甲届", "届次要原样交给解析（用户可能写编号、也可能写名称片段）"
        return resolved, ""

    async def fake_post(path, payload=None):
        calls.append((path, payload))
        if resolved == "boom":
            return {"ok": False, "error": "这一届现在是「进行中」：报名只对「筹备中」的比赛开放。"}
        return {"ok": True, "text": "已报名：甲届（e001）"}

    plugin._resolve_event = fake_resolve
    plugin._post = fake_post
    return plugin


async def test_signup_reports_the_group_for_the_whitelist(plugin_module):
    """报名要把**这次会话的群号**一起报给站点：白名单就是按它判的。

    少带这一个字段，白名单群里的新人会被站点按「非成员」挡掉——功能看着是好的，
    只是永远用不上，最难查。私聊没有群号（报空串），站点按「只认成员」处理。
    """
    calls: list[tuple] = []
    plugin = _signup_plugin(plugin_module, calls)
    out = await _collect(plugin.cmd_signup(_FakeEvent(sender="10001", group="900001"), "甲届"))
    path, payload = calls[0]
    assert path == "signup"
    assert payload == {
        "qq": "10001",
        "action": "join",
        "event": "e001",
        "group": "900001",
        "name": "",
    }
    assert "已报名" in out[0]["text"], "站点回的那句话原样发群（闸门理由只在站点那一侧）"


async def test_cancel_signup_is_the_same_endpoint_with_cancel(plugin_module):
    """取消报名：同一条接口、`action=cancel`——两条命令只差这一个字。"""
    calls: list[tuple] = []
    plugin = _signup_plugin(plugin_module, calls)
    out = await _collect(plugin.cmd_cancel_signup(_FakeEvent(sender="10001"), "甲届"))
    assert calls[0][0] == "signup" and calls[0][1]["action"] == "cancel"
    assert calls[0][1]["group"] == "", "私聊没有群号：站点按「只认成员」处理"
    assert out[0]["text"]


async def test_signup_shows_the_sites_reason_verbatim(plugin_module):
    """被站点拒绝时，把站点的人话原样发群：插件不自己编理由（判两遍就会漂移）。"""
    calls: list[tuple] = []
    plugin = _signup_plugin(plugin_module, calls, resolved="boom")
    out = await _collect(plugin.cmd_signup(_FakeEvent(sender="10001", group="g1"), "甲届"))
    assert out[0]["text"] == "这一届现在是「进行中」：报名只对「筹备中」的比赛开放。"
