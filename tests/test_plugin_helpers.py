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
    """帮助里必须写清「要先 @ 机器人或带唤醒前缀」——关掉大模型后这是唯一的说明。"""
    text = plugin_module.HELP_TEXT
    assert "@ 机器人" in text
    assert "唤醒前缀" in text
    assert "比赛直播" in text  # 新命令别从帮助里掉出去


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
    """帮助里要有「怎么参加」——群里最常问的就是这个，答不上就没人接着问了。"""
    text = plugin_module.HELP_TEXT
    assert "怎么参加" in text
    assert "参赛不用自己注册" in text


def test_help_doc_lists_every_command(plugin_module):
    """`HELP.md`（做图底稿）与 `HELP_TEXT`（群里回的文本）都要提到每一条命令。

    这两处最容易漂移：加了命令却忘了同步，群里回的文字和帮助图就对不上。
    """
    source = PLUGIN.read_text(encoding="utf-8")
    commands = re.findall(r'@filter\.command\(\s*"([^"]+)"', source)
    assert len(commands) == 16, f"命令数变了（现在 {len(commands)} 条）：请同步 HELP.md 与 README"
    doc = (PLUGIN.parent / "HELP.md").read_text(encoding="utf-8")
    for name in commands:
        assert name in plugin_module.HELP_TEXT, f"HELP_TEXT 里缺命令：{name}"
        assert name in doc, f"HELP.md 里缺命令：{name}"


def test_plugin_registers_no_llm_tools(plugin_module):
    """插件**不注册任何 ``llm_tool``**：机器人不经过大模型，答案稳定可复现。

    有人「顺手」加回一个 LLM 工具时，这条会红——那正是要提醒的时候。
    """
    source = PLUGIN.read_text(encoding="utf-8")
    assert "llm_tool(" not in source, "插件里又注册了 llm_tool：本项目不用大模型（见 README）"
    assert not [name for name in dir(plugin_module.NTEMatchPlugin) if "llm" in name.lower()]


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
