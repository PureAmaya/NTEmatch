"""NTE 比赛 × AstrBot 插件。

把「NTE 比赛」平台的赛事数据接进群聊，两种用法：

* **群命令**（``@filter.command``，25 条）：命中哪条命令、回什么，全由字符串匹配与站点
  数据决定，同一个问题问两次结果一样——**关掉大模型 / 不配 provider 也照常工作**；
* **LLM 工具**（``@filter.llm_tool``，可选，见 :meth:`NTEMatchPlugin.tool_query`）：
  大模型被唤醒时，把同样这些数据挂成函数工具，让它能答「自由问法」（例如「我们下把打谁」）。
  工具**只查数据、只回文本**：不自己往群里发消息、不碰系统提示词与人格——最终那句话
  由大模型按**当前人格**说出来（工具结果会作为「工具返回」回给模型，见 README「LLM 工具」）。

唯一「不查数据」的是一条快捷键：**问帮助**可以直接回一张帮助图——默认就发**站点内置的
那张**（把图放成站点里的 ``static/help.jpg`` 即可，地址 ``/help.jpg``；没有图就回文字说明，
见 README 与 ``HELP.md``），方便贴群公告。

插件本身**不做业务计算**，只调站点的只读查询 API（``/api/bot/*``），
所以文案、赛制、分页逻辑全在站点那一侧，改一处两边同步。

唯一的例外是**真 @ 投递**（见 :meth:`NTEMatchPlugin._outbox_loop`）：AstrBot 的
OpenAPI **没有 at 段**，站点从外面发不出真 @，所以「要 @ 人」的消息（召集 / 赛前提醒）
由站点排队、插件每隔几秒取走，**在 AstrBot 进程内**用 ``At`` 组件发出去再回执。

安装：把本目录整个复制到 AstrBot 的 ``data/plugins/`` 下，然后在 AstrBot 的
插件配置里填 **站点地址** 与 **查询 API 令牌**（站点「服务器 → QQ 机器人」生成）。

源码：https://github.com/PureAmaya/NTEmatch （AGPL-3.0，© 早八时睡觉的你）
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain

try:  # MessageChain 的位置各版本不一：先按文档，再按内核路径，都没有就退化成列表
    from astrbot.api.event import MessageChain
except Exception:  # noqa: BLE001  (老版本没有这个导出)
    try:
        from astrbot.core.message.message_event_result import MessageChain
    except Exception:  # noqa: BLE001  (实在没有就用列表，有的版本也收)
        MessageChain = None  # type: ignore[assignment]

__all__ = ["NTEMatchPlugin"]


def _conf(config, key: str, default):
    """读插件配置：兼容 dict 与 AstrBot 的配置对象（值可能被包成 ``{"value": ...}``）。"""
    raw = None
    try:
        raw = config.get(key)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001  (配置形态随版本而变，读不到就用默认值)
        raw = None
    if isinstance(raw, dict) and "value" in raw:
        raw = raw.get("value")
    if raw is None or raw == "":
        return default
    return raw


#: 大模型可以查的查询类型：名字与站点 ``app/qqbot.py`` 的 ``KIND_META`` **一一对应**，
#: 只少一个 ``call``（召集）。为什么单列一份、而不是「站点有什么就给什么」：
#:
#: * ``call``（召集）是往群里 @ 一大片人 —— 「什么时候喊人」该由人决定，不该由模型决定；
#: * **凭据类能力一律不给工具**（推流地址 / 重置密钥 / 重置令牌 / 改推流码 / 直播注册）：
#:   工具的结果是**回给大模型的**，模型会照着重述到群里 —— 密钥与令牌泄一次就等于白送；
#: * 差集有测试盯着（``test_llm_tool_kinds_follow_the_site``）：
#:   站点加了新类型而这里没跟上，会红，逼着人做一次「给不给模型」的决定。
_AGENT_KINDS = (
    "event",
    "progress",
    "next",
    "result",
    "champion",
    "roster",
    "detail",
    "live",
    "list",
    "ids",
    "uuids",
)

#: 这几个是**全局**类型（不挑届次）：与命令一侧 ``比赛直播`` / ``比赛列表`` / ``比赛届次`` 一致，
#: 工具也不该逼着模型去要一个届次编号。
_AGENT_GLOBAL_KINDS = ("live", "list", "ids")


def _llm_tool(name: str):
    """``@filter.llm_tool(name)`` 的安全包装：拿不到它就退回「不注册这个工具」。

    **不能**直接写 ``@filter.llm_tool(...)``：装饰器在**类定义时**求值，老版本 AstrBot 上
    会 AttributeError —— 插件整个加载不了，连命令一起没了。这里拿不到就当普通函数，
    命令照旧可用，只是大模型那侧看不见这些工具。

    ``name`` 与函数 docstring 里的 ``Args:`` 是 AstrBot 生成工具描述的唯一来源（它**不看**
    类型注解）：docstring 少写类型、或参数名与签名对不上，轻则参数被静默丢掉、重则**装饰时
    直接抛异常**（插件加载失败）。所以有三条测试专门盯着这些 docstring。
    """
    factory = getattr(filter, "llm_tool", None)
    if callable(factory):
        try:
            return factory(name=name)
        except TypeError:  # 极老的版本：不吃 name 关键字
            return factory()
    return lambda func: func


def _sender_id(event) -> str:
    """发消息者的 QQ。不同 AstrBot 版本字段位置不同，逐个试，取不到就回空串。"""
    try:
        value = event.get_sender_id()
        if value:
            return str(value)
    except Exception:  # noqa: BLE001  (老版本没有这个方法)
        pass
    obj = getattr(event, "message_obj", None)
    sender = getattr(obj, "sender", None) if obj is not None else None
    return str(getattr(sender, "user_id", "") or "")


def _group_id(event) -> str:
    """这次会话的群号；**私聊回空串**。

    自助报名要把它一起报给站点：站点按它判「报名白名单」（白名单群里的非成员也能报上）。
    私聊没有群号 —— 那不是「哪个群放宽」的场合，站点会按「只认成员」处理。
    """
    try:
        value = event.get_group_id()
    except Exception:  # noqa: BLE001  (老版本没有这个方法)
        value = ""
    return str(value or "")


def _sender_name(event) -> str:
    """发消息者的昵称（只在站点顺手建成员时当名字用；取不到就回空串）。

    取不到不是异常：站点会退回「群友 <QQ>」，本人之后能用「比赛资料 名字 xxx」改。
    """
    try:
        value = event.get_sender_name()
    except Exception:  # noqa: BLE001  (老版本没有这个方法)
        value = ""
    if value:
        return str(value)
    obj = getattr(event, "message_obj", None)
    sender = getattr(obj, "sender", None) if obj is not None else None
    return str(getattr(sender, "nickname", "") or "")


def _chat_key(event) -> str:
    """这次会话的标识（群号优先，其次私聊对象），用于按会话计冷却。"""
    for getter in ("get_group_id", "get_sender_id"):
        try:
            value = getattr(event, getter)()
        except Exception:  # noqa: BLE001  (同上)
            value = ""
        if value:
            return str(value)
    return "global"


def _self_id(event) -> str:
    """机器人自己的 QQ（用来把「@机器人」从被 @ 名单里剔掉）。取不到就回空串。"""
    for getter in ("get_self_id", "get_bot_id"):
        try:
            value = getattr(event, getter)()
        except Exception:  # noqa: BLE001  (老版本没有这个方法)
            value = ""
        if value:
            return str(value)
    obj = getattr(event, "message_obj", None)
    return str(getattr(obj, "self_id", "") or "")


def _stream_key_arg(*raw: str) -> str:
    """从命令参数里挑出「流名」，只剔掉**纯数字**。

    为什么要剔：``比赛直播注册`` 既可能带流名（``比赛直播注册 tom``），也可能只是 @ 了个人
    （``比赛直播注册 @某人``）——平台有时会把被 @ 的 QQ 当成一个参数传进来，
    而「一串纯数字」当流名没有任何意义（还会把别人的号段占成流名）。

    其余参数**原样交给站点**去校验：错字要当场报错，不能静默换一个自动流名
    （那会让人以为「我起的名字生效了」）。
    """
    for chunk in raw:
        text = str(chunk or "").strip()
        if text and not text.isdigit():
            return text
    return ""


def _at_targets(event) -> list[str]:
    """消息里**被 @ 的人**的 QQ（按顺序、去重）。

    为什么从消息链里取、而不是让用户手打 QQ：手打的号码最容易抄错一位，而授权是
    写操作——抄错就等于给陌生人开了权限。@ 由平台保证是真实存在的账号。

    会剔掉机器人自己（唤醒时必然 @ 了它）与发消息者本人。
    """
    out: list[str] = []
    obj = getattr(event, "message_obj", None)
    chain = getattr(obj, "message", None) or []
    skip = {_self_id(event), _sender_id(event)}
    for seg in chain:
        qq = getattr(seg, "qq", None)
        if qq is None and isinstance(seg, dict):
            qq = seg.get("qq")
        clean = "".join(ch for ch in str(qq or "") if ch.isdigit())
        if clean and clean not in skip and clean not in out:
            out.append(clean)
    return out


def _at_names(event) -> dict[str, str]:
    """被 @ 的人显示名（At 组件里的 ``name``，没有就空）——用作新成员的名字。"""
    out: dict[str, str] = {}
    obj = getattr(event, "message_obj", None)
    chain = getattr(obj, "message", None) or []
    for seg in chain:
        qq = getattr(seg, "qq", None)
        if qq is None and isinstance(seg, dict):
            qq = seg.get("qq")
        name = getattr(seg, "name", None)
        if name is None and isinstance(seg, dict):
            name = seg.get("name")
        clean = "".join(ch for ch in str(qq or "") if ch.isdigit())
        if clean and name:
            out.setdefault(clean, str(name))
    return out


def _page_number(raw) -> int:
    """把「2」「第2页」「2 页」这类写法解析成页码；解析不出就回第 1 页。

    **参数为什么不用 int 注解**：AstrBot 的命令过滤器在参数带 int 默认值时，会对实参做
    ``int(...)`` 强转，转不动会**直接抛异常**（而不是忽略这次匹配），整条命令失效——
    可用户随手写「第2页」再正常不过。所以这里收成字符串、自己宽松解析。
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    return min(999, max(1, int(digits))) if digits else 1


#: 「比赛届次」里表示「只看自己创建的」的词（写「我的」/「我」/「mine」都算）。
_MINE_WORDS = frozenset({"我的", "我", "自己", "自己创建", "我创建", "mine"})

#: 明确要「全部」的词（默认也是全部，写出来只是让人安心）。
_ALL_WORDS = frozenset({"全部", "所有", "全部届次", "all"})


def _ids_args(*raw: str) -> tuple[str, int]:
    """解析「比赛届次」的参数，返回 ``(scope, page)``；``scope`` 取 ``all`` / ``mine``。

    认三类词：``我的``（只看自己创建的）、``全部``（默认）、页码（``2`` / ``第2页``）。
    顺序随意——「比赛届次 2 我的」与「比赛届次 我的 2」都算数；认不出的词**直接忽略**
    （宁可回默认视图，也别为一个错别字把人挡在命令外面）。

    参数为什么收成字符串：见 :func:`_page_number`——带 int 注解时 AstrBot 会做强转，
    非数字参数会让整条命令失效。
    """
    scope = "all"
    page = 1
    for chunk in raw:
        for word in re.split(r"[\s,，、]+", str(chunk or "").strip()):
            low = word.lower()
            if not low:
                continue
            if low in _MINE_WORDS:
                scope = "mine"
            elif low in _ALL_WORDS:
                scope = "all"
            elif re.fullmatch(r"第?\s*\d+\s*页?", word):
                page = _page_number(word)
    return scope, page


def _call_denied_text(data: dict) -> str:
    """召集被拒时说清原因与**下一步动作**（纯函数，方便单测）。

    规则只有一条：**谁创建的届，谁可以召集**（服务器管理员的全局权限另算，
    见站点 ``/api/bot/managers``）。所以被拒只有两种可能：

    1. 这一届**不是你创建的** —— 那就告诉他：你自己哪几届能召集、该敲哪条命令；
    2. **群里认不出你**（你没在站点「我的」页登记 QQ），或本届创建者没登记 QQ
       —— 名单为空时谁都召集不了，得先把 QQ 补上。

    以前这里只回一句「召集需要赛事管理员身份：只有本届举办者或服务器管理员能召集」，
    对一位**正是赛事管理员**的人说，等于什么都没说（他甚至会以为站点把他的角色弄丢了）。
    现在先给结论、再给能立刻做的动作（全程纯文本，不带 Markdown）。
    """
    owner = data.get("owner") or {}
    owner_name = str(owner.get("name") or "").strip()
    if not (data.get("qqs") or []):
        # 名单为空 ≠ 「你没权限」，而是这一届此刻没有能召集的人
        if owner_name and not owner.get("hasQq"):
            return (
                f"这一届是「{owner_name}」创建的，但他还没在站点登记 QQ，所以现在谁都召集不了。\n"
                "让他到站点「我的」页填上自己的 QQ（群里只能靠 QQ 认人），"
                "或让服务器管理员来召集。"
            )
        return (
            f"{data.get('note') or '这一届现在没有可召集的人'}。\n"
            "召集权限＝这一届的创建者或服务器管理员；在站点「我的」页登记 QQ 才能对上号。"
        )
    lines = ["召集要「本届创建者」或服务器管理员——赛事管理员能召集的是他自己创建的那些届。"]
    mine = [m for m in (data.get("mine") or []) if m.get("id")]
    if mine:
        listed = "、".join(f"{m.get('name') or m['id']}（{m['id']}）" for m in mine[:5])
        lines.append(f"你自己创建的届：{listed}。用「比赛召集 <届次>」召集你那届的人。")
    else:
        lines.append(
            "没查到你创建的届：在站点新建一届后就能召集它的人；"
            "如果你建过，先到「我的」页登记 QQ（群里只能靠 QQ 认人）。"
        )
    return "\n".join(lines)


# 帮助文本：**唯一的说明来源**——群里回的是它，``HELP.md``（做帮助图用）也照着它写。
# 改命令时顺手改这两处，别让它们各说一套。
#
# 分几块：命令清单 → 届次怎么写 → 怎么参加 → 怎么触发 → 什么会私聊发你。
HELP_TEXT = (
    "【NTE 比赛】可用命令：\n"
    "· 比赛 [届次] —— 该届的信息 + 进度\n"
    "· 比赛直播 —— 现在谁在直播（主直播间 + 选手 / 成员机位的观看地址）\n"
    "· 比赛列表 [页码] —— 全部赛事\n"
    "· 比赛信息 [届次] —— 时间 / 赛制 / 人数 / 简介 / 比赛规则（发一张卡片图）\n"
    "· 比赛进度 [届次] —— 已赛多少、正在打谁 vs 谁\n"
    "· 比赛下一场 [届次] —— 接下来看哪场（含计划时间）\n"
    "· 比赛结果 [届次] —— 先发结果图：逐场比分 + 淘汰赛树状图\n"
    "· 比赛详情 [届次] [场次] —— 综合信息；给场次编号就细说那一场\n"
    "· 比赛名单 [届次] —— 参赛名单（选手 / 队伍 / 替补）\n"
    "· 比赛冠军 [届次] —— 冠军（或积分制榜首前三）\n"
    "· 比赛UID [@某人] —— 游戏 UUID（群里直接回；不 @ 就是你自己）\n"
    "· 比赛UUID [届次] —— 参赛选手的 UUID 清单（每行「名字 UUID」，可整段复制）\n"
    "· 比赛届次 [我的] [页码] —— 届次编号与名称（填参数前先发它；写「我的」只看自己创建的，\n"
    "  一页装不下时私聊发你）\n"
    "· 比赛召集 [届次] —— @ 参赛者到场（本届创建者＝举办者 / 服务器管理员，带冷却）\n"
    "· 比赛报名 [届次] —— 报名参赛（只对筹备中的届开放；只定名单，不组队不定赛制）\n"
    "· 比赛取消报名 [届次] —— 取消报名（只取消这一届的参赛资格，成员身份留着）\n"
    "· 比赛我的 —— 你的推流地址 + 直播间地址（私聊发你）\n"
    "· 比赛直播注册 [流名] [@某人] —— 开播要用的东西一次给全：缺推流码 / 令牌就补上，\n"
    "  连推流地址与注意事项一起私聊发本人\n"
    "· 比赛资料 [字段 新值] [@某人] —— 资料 + 每项怎么改（私聊发你），\n"
    "  字段：名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ；写「清空」就清掉那一项\n"
    "· 比赛重置密钥 [@某人] —— 换登录密钥（旧密钥立即失效；私聊发本人）\n"
    "· 比赛重置令牌 [@某人] —— 换直播令牌（要先有推流码；私聊发本人）\n"
    "· 比赛改推流码 <流名> [@某人] —— 改推流码（等同「比赛资料 推流码 <流名>」）\n"
    "· 比赛授权 @某人 —— 把群友设为赛事管理员（仅服务器管理员；不是成员会自动建号）\n"
    "· 比赛添加 @某人 —— 把群友添加为普通成员（仅服务器管理员；密钥私聊发给本人）\n"
    "· 比赛帮助 —— 就是本条（私聊发你）\n"
    "上面带 [届次] 的命令**必须写明哪一届**：写 e001 / 1 / 第2届 / 名称里的几个字都行\n"
    "（不知道有哪些届就发「比赛届次」）；不带 [届次] 的命令不用填。\n"
    "比赛直播与比赛列表是全局信息，不用填届次。\n"
    "—— 怎么参加 ——\n"
    "自己报名：本届还没开赛（筹备中）时发「比赛报名 <届次>」——只定名单，不组队不定赛制，\n"
    "组队与赛程都等报名结束由管理员在网站上安排；要退出就发「比赛取消报名 <届次>」。\n"
    "开赛 / 结束后名单就冻住了，报名会被拒绝（会告诉你卡在哪一条）。\n"
    "报名白名单群里的新人也能直接报名：站点顺手把你加成成员，登录密钥私聊发你；\n"
    "别的群请先让管理员把你加为成员（服务器管理员发：比赛添加 @你）。\n"
    "想用「比赛我的」查自己的推流地址，得先成为成员（服务器管理员发：比赛添加 @你）。\n"
    "—— 你自己的东西 ——\n"
    "不知道从哪下手就发「比赛资料」：它会把你现在有什么、每一项怎么改都列出来。\n"
    "要开播（推流）发「比赛直播注册」：缺推流码就给你一个、缺令牌就发一把，\n"
    "推流地址与 OBS 注意事项一起私聊发你；只顾着查地址就发「比赛我的」。\n"
    "游戏 UUID 发「比赛UID」（群里就能看，@ 某人可以看别人的）。\n"
    "「比赛我的 / 比赛资料 / 比赛直播注册 / 比赛重置密钥 / 比赛重置令牌 / 比赛改推流码」\n"
    "不 @ 人就是**只动自己那份**（按你发命令的 QQ 认人）；带上 @某人 才是替 TA 做，\n"
    "那要服务器管理员，结果也只发 TA 本人。\n"
    "—— 怎么触发 ——\n"
    "要先 @ 机器人 再说命令，或按 AstrBot 里设的唤醒前缀发（例如「/比赛进度」）。\n"
    "光打「比赛进度」不会触发：这是 AstrBot 的命令过滤规则（必须被 @ 或命中唤醒前缀），\n"
    "不是本插件能改的——不 @ 就不会有任何回复。\n"
    "—— 会私聊发给你的东西 ——\n"
    "命令说明、你自己的推流地址、资料、届次很多时的届次列表、新成员的登录密钥都走私聊\n"
    "（只该你看到）。\n"
    "私聊发不出去（没加机器人好友）时，资料与说明会退回群里；\n"
    "但**登录密钥与直播令牌不会**——它们只显示一次，泄在群里等于白送一个账号。"
)


def _wrap_chain(components: list) -> object:
    """把组件列表包成 ``MessageChain``（各版本签名不一，最后退回裸列表）。

    ``context.send_message(umo, chain)`` 在有些版本里只认 ``MessageChain`` 对象，
    另一些版本拿列表也照发——所以**拿不到类就用列表**，至少有的版本能发出去。
    """
    if MessageChain is None:
        return components
    try:
        return MessageChain(chain=components)
    except Exception:  # noqa: BLE001  (老版本签名不同：先建空的再塞)
        chain = MessageChain()
        chain.chain = components  # type: ignore[attr-defined]
        return chain


def _image_result(event, image: str):
    """发一张图：优先用框架的 ``image_result``，取不到就退化成 OneBot 的 CQ 码。

    留这个兜底是为了跨版本：AstrBot 各版本的 event 接口不完全一样，而 CQ 码是 OneBot
    的通用写法（站点推流端发 @ 用的也是同一套惯例）。最坏情况下它显示成一条带链接的
    消息，而不是「什么都不发」。
    """
    maker = getattr(event, "image_result", None)
    if callable(maker):
        return maker(image)
    return event.plain_result(f"[CQ:image,file={image}]")


class NTEMatchPlugin(star.Star):
    """赛事查询：25 条群命令（不经过大模型）+ 4 个可选的 LLM 工具（只回文本、不碰人格）。"""

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        # 配置**不在构造时读死**：不同 AstrBot 版本注入配置的时机不一样（有的是实例化
        # 之后由框架赋值），读死了会一直用默认值、命令通通报「令牌不正确」。
        # 这里只保证「别把框架已经放好的 config 覆盖掉」。
        if config is not None:
            self.config = config
        elif not hasattr(self, "config"):
            self.config = {}
        self._client: httpx.AsyncClient | None = None
        # 「召集」的冷却记录：{会话: [时间戳, ...]}（只留一小时内）
        self._call_times: dict[str, list[float]] = {}
        # 真 @ 投递的后台任务（站点排队、这里取走发；见 _outbox_loop）
        self._outbox_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 配置（每次读取，见上面关于注入时机的说明）
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return str(_conf(self.config, "base_url", "http://127.0.0.1:8000")).rstrip("/")

    @property
    def token(self) -> str:
        return str(_conf(self.config, "api_token", ""))

    @property
    def timeout(self) -> float:
        return float(_conf(self.config, "timeout", 10))

    @property
    def reply_prefix(self) -> str:
        return str(_conf(self.config, "reply_prefix", ""))

    # ------------------------------------------------------------------ #
    # 与站点通信
    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _http(self) -> httpx.AsyncClient:
        """复用一个连接池：每条命令都新建 client，等于每次都重新握手。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _get(self, path: str, **params) -> dict:
        """调一次站点接口；失败时返回 ``{"ok": False, "error": "..."}``（不抛异常）。"""
        url = f"{self.base_url}/api/bot/{path}"
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            resp = await self._http().get(url, headers=self._headers, params=clean, timeout=self.timeout)
        except httpx.HTTPError as exc:
            logger.warning("[NTE 比赛] 请求失败 %s：%s", url, exc)
            return {"ok": False, "error": f"连不上比赛平台（{exc}）"}
        return self._handle(resp)

    async def _post(self, path: str, payload: dict) -> dict:
        """调一次站点的**写**接口（授权 / 添加成员 / 私聊投递）。

        与 :meth:`_get` 共用错误处理，差别只在 403：写接口的 403 是「你（这个 QQ）
        没有权限」，站点那边已经写好了人话提示（例如「只有服务器管理员能…」），直接透传；
        读接口的 403 基本只有一个原因：查询 API 没启用。
        """
        url = f"{self.base_url}/api/bot/{path}"
        try:
            resp = await self._http().post(
                url, headers=self._headers, json=payload, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            logger.warning("[NTE 比赛] 请求失败 %s：%s", url, exc)
            return {"ok": False, "error": f"连不上比赛平台（{exc}）"}
        return self._handle(resp, write=True)

    def _handle(self, resp: httpx.Response, *, write: bool = False) -> dict:
        if resp.status_code == 401:
            return {"ok": False, "error": "查询令牌不正确，请到站点重新生成后在插件配置里更新"}
        if resp.status_code == 403 and not write:
            return {"ok": False, "error": "查询接口未启用：请先在站点「服务器 → QQ 机器人」生成令牌"}
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = str(body.get("error") or body.get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:120]
            if write:
                return {"ok": False, "error": detail or f"平台返回 HTTP {resp.status_code}"}
            return {"ok": False, "error": f"平台返回 HTTP {resp.status_code}：{detail}"}
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "error": "平台返回的不是 JSON"}

    async def _notify(self, qq: str, text: str) -> dict:
        """让**站点**把一条消息私聊给某个 QQ。

        为什么不自己发：站点已经握着 AstrBot 的 API Key 与统一发送通道（群推送走的就是
        它），插件不必再去猜某个 AstrBot 版本的私聊接口。私聊只用来发**只该本人看到**的
        东西：登录密钥、推流地址、命令说明。
        """
        if not qq:
            return {"ok": False, "error": "没有可用的 QQ"}
        return await self._post("notify", {"qq": qq, "text": text})

    async def _query(
        self,
        kind: str,
        event_id: str = "",
        ref: str = "",
        page: int = 1,
        at: bool = True,
        scope: str = "",
        qq: str = "",
    ) -> dict:
        return await self._get(
            "query",
            kind=kind,
            eventId=event_id,
            ref=ref,
            page=page,
            at="" if at else "0",
            scope=scope,
            qq=qq,
        )

    def _texts(self, data: dict, *, decorated: bool = True) -> list[str]:
        """把接口回的分段拼上前缀；出错时只回一行错误说明。

        ``decorated=False`` 给 **LLM 工具**用：``reply_prefix`` 是给群里的人认机器人用的，
        回给大模型的纯文本不需要它（模型照着重述时反而会多带一句前缀）。
        """
        if not data.get("ok"):
            return [f"查询失败：{data.get('error') or '未知原因'}"]
        parts = data.get("parts") or []
        if not parts:
            return ["（没有可显示的内容）"]
        prefix = self.reply_prefix if decorated else ""
        return [f"{prefix}{part}" if prefix else part for part in parts]

    async def _agent_query(
        self, kind: str, event_id: str, ref: str, page: int, scope: str, qq: str
    ) -> str:
        """LLM 工具用的一次查询：**只回纯文本**。

        与命令那条路（:meth:`_run`）的差别只在「往哪儿去」：命令要把结果发到群里，
        所以带回复前缀、还可能带 @ 片段；工具的文本是**回给大模型**的（由它按当前人格
        转述），所以不带前缀、也不带 @ 片段——`at=False` 同时让站点省掉那些 @ 片段。
        """
        target = ""
        if kind not in _AGENT_GLOBAL_KINDS:
            target, error = await self._resolve_event(event_id)
            if error:
                return error
        data = await self._query(kind, target, ref, page, at=False, scope=scope, qq=qq)
        return "\n".join(self._texts(data, decorated=False))

    # ------------------------------------------------------------------ #
    # 届次解析：让用户能用「e001 / 1 / 第2届 / 名称片段」随便写
    # ------------------------------------------------------------------ #
    async def _events(self) -> tuple[list[dict], str]:
        """届次列表；失败时返回 ``([], 原因)``——**别把鉴权失败说成「没有赛事」**。"""
        data = await self._get("events")
        if not data.get("ok"):
            return [], str(data.get("error") or "查询失败")
        return list(data.get("events") or []), ""

    async def _resolve_event(self, token: str) -> tuple[str, str]:
        """把用户写的「届次」解析成编号，返回 ``(编号, 错误提示)``。

        **届次必须写明**：``e001`` / ``1`` / ``第2届`` / 名称里的几个字（不写就提示补上，见下）。
        找不到时会**列出可用编号**——用户自己就能改对。

        为什么不给「不填 = 某一届」的默认值：站点内部确实有个「当前届」指针
        （``store.current_id``，谁最近打开 / 新建过就是谁），但它是**实现细节**，
        前台早就不对外提这个概念了。拿它当默认值，群友看到的会是「碰运气」的数据——
        同一句话今天问是这届、明天问变成那届。
        """
        raw = (token or "").strip()
        if not raw:
            # 先回提示、再谈查数据：连届次都没写时不必去问站点
            return "", (
                "要写明是「哪一届」：命令后面带上届的名称或编号，例如「… 春节」或「… e001」。\n"
                "发「比赛届次」可以看到全部届的编号与名称。"
            )
        rows, error = await self._events()
        if error:
            return "", error
        if not rows:
            return "", "平台上还没有赛事。"
        for item in rows:
            if str(item.get("id", "")).lower() == raw.lower():
                return str(item["id"]), ""
        if re.fullmatch(r"第?\s*\d+\s*届?", raw):
            num = int(re.sub(r"\D", "", raw) or 0)
            for item in rows:
                digits = re.sub(r"\D", "", str(item.get("id", "")))
                if digits and int(digits) == num:
                    return str(item["id"]), ""
            ordered = sorted(rows, key=lambda x: str(x.get("id", "")))
            if 1 <= num <= len(ordered):
                return str(ordered[num - 1]["id"]), ""
        hit = [x for x in rows if raw.lower() in str(x.get("name") or "").lower()]
        if len(hit) == 1:
            return str(hit[0]["id"]), ""
        if len(hit) > 1:
            listed = "、".join(f"{x['id']} {x.get('name')}" for x in hit[:6])
            return "", f"「{raw}」匹配到多届：{listed}。请用编号再试一次。"
        listed = "、".join(f"{x['id']} {x.get('name')}" for x in rows[:8])
        return "", f"没找到「{raw}」这一届。可用编号：{listed}\n（发「比赛届次」看全部）"

    async def _run(self, kind: str, event_id: str = "", ref: str = "", page: int = 1):
        """公共流程：解析届次 → 查询 → 逐块产出结果。

        块有两种：``("image", 地址)``（服务端生成的比赛卡片：信息 + 自动生成的规则）
        与 ``("text", 内容)``。**顺序就是服务端给的顺序**（先图后文），别在这里重排——
        「比赛信息」有卡片时，文字部分只省下一行说明（内容都在图里）。

        站点没装 Pillow 时 ``card`` 为 null、文本里也已经带了规则摘要，
        所以这里**不需要**分支：有图就发图，没图就发文本。
        """
        target, error = await self._resolve_event(event_id)
        if error:
            yield ("text", error)
            return
        data = await self._query(kind, target, ref, page)
        card = data.get("card") or {}
        if card.get("url"):
            yield ("image", str(card["url"]))
        for text in self._texts(data):
            yield ("text", text)

    def _emit(self, event, block):
        """把一块结果变成 AstrBot 的返回：图走 ``image_result``，文本走 ``plain_result``。"""
        kind, value = block
        if kind == "image":
            return _image_result(event, value)
        return event.plain_result(value)

    def _call_allowed(self, event) -> tuple[bool, str]:
        """「召集」的冷却闸：同一会话的最小间隔 + 每小时上限。

        为什么单独做、不复用站点那套推送限流：站点限流只作用于网页「推送到群」的
        HTTP 路径；插件是**直接往群里发**的，根本不经过它。而召集天生会 @ 一大片人，
        没有闸门就等于让任何人反复刷群。
        """
        chat = _chat_key(event)
        now = time.time()
        cooldown = float(_conf(self.config, "call_cooldown", 60) or 0)
        per_hour = int(_conf(self.config, "call_per_hour", 6) or 0)
        hits = [t for t in self._call_times.get(chat, []) if now - t < 3600]
        if hits and cooldown > 0 and now - hits[-1] < cooldown:
            return False, f"刚召集过，请 {int(cooldown - (now - hits[-1])) + 1} 秒后再试。"
        if per_hour > 0 and len(hits) >= per_hour:
            return False, f"这个会话一小时内已经召集过 {len(hits)} 次了，先等等吧。"
        hits.append(now)
        self._call_times[chat] = hits
        return True, ""

    # ------------------------------------------------------------------ #
    # 群命令
    # ------------------------------------------------------------------ #
    @filter.command("比赛帮助", alias={"赛事帮助", "比赛命令", "比赛功能", "ntehelp", "比赛help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """把帮助发给你：**发图优先**（图比一屏文字好读，也方便群友转发 / 贴公告），
        没有图才把命令说明**私聊**发给你（群里只留一句提示，免得刷屏）。

        私聊发不出去（未加好友 / 没配推送）时退回群里回——总比让人干等强。
        """
        image = await self._help_image()
        if image:
            # 帮助图有意发在**当前会话**（谁问发哪），不像文字那样走私聊：
            # 它就是拿来给人看的，藏着反而没用。
            yield _image_result(event, image)
            return
        who = _sender_id(event)
        if who:
            sent = await self._notify(who, HELP_TEXT)
            if sent.get("ok"):
                yield event.plain_result("命令说明已私聊发给你，按那个用就行。")
                return
            logger.info("[NTE 比赛] 私聊帮助失败，改为群内回复：%s", sent.get("error"))
        yield event.plain_result(HELP_TEXT)

    async def _help_image(self) -> str:
        """这次该发哪张帮助图；回空串 = 「没有图，走文字说明」。

        * 配了 ``help_image`` 就用它——**不替你把关**：你写的地址你自己清楚；
        * 没配则默认用**站点内置那张**（把图放成 ``static/help.jpg``，地址就是 ``/help.jpg``），
          但要先探一下在不在：默认值得开箱即用，可也不能对着一个还没放图的站点
          发一张破图（群友看到的会是加载失败的占位）。探不到就老实回文字说明。
        """
        configured = str(_conf(self.config, "help_image", "")).strip()
        if configured:
            return configured
        url = f"{self.base_url}/help.jpg"
        try:
            resp = await self._http().head(url, timeout=min(5.0, self.timeout))
        except httpx.HTTPError as exc:
            # 站点没起来 / 网络不通：按「没有图」处理，回文字说明（此时推送多半也不通）
            logger.debug("[NTE 比赛] 探测内置帮助图失败：%s", exc)
            return ""
        return url if resp.status_code == 200 else ""

    # ------------------------------------------------------------------ #
    # 写操作：授权 / 添加群友（只有服务器管理员，权限由**站点**按 QQ 判定）
    # ------------------------------------------------------------------ #
    async def _grant(self, event, permission: str):
        """公共流程：解析被 @ 的人 → 调站点 → 密码私聊给本人 → 群里只报结果。

        **登录密钥永远不进群**：即使私聊失败，也只说「没能私聊发出去」，让本人自己
        找服务器管理员重置——密钥只出现一次，泄在群里就等于白送一个账号。
        """
        targets = _at_targets(event)
        if not targets:
            return event.plain_result(
                "没看到你 @ 谁。用法：@机器人 比赛授权 @某人\n"
                "（先 @ 对方，再发命令；手打 QQ 容易抄错，所以只认 @）"
            )
        target = targets[0]
        data = await self._post(
            "members",
            {
                "actorQq": _sender_id(event),
                "targetQq": target,
                "name": _at_names(event).get(target, ""),
                "permission": permission,
            },
        )
        if not data.get("ok"):
            return event.plain_result(str(data.get("error") or "操作失败"))
        label = "赛事管理员" if permission == "event_admin" else "普通成员"
        who = str(data.get("name") or target)
        if data.get("created"):
            # 密钥由**站点**直接私聊给本人（这里的响应里没有明文，插件也无从泄露）。
            if data.get("keySent"):
                return event.plain_result(f"已把 {who} 添加为{label}，登录密钥已私聊发给 TA。")
            return event.plain_result(
                f"已把 {who} 添加为{label}，但登录密钥没能私聊发给 TA"
                f"（{data.get('detail') or '未知原因'}）。\n"
                "密钥只显示这一次、站里也取不回：让 TA 加机器人好友后自己发一次"
                "「比赛重置密钥」，就能拿到一把新的（旧的那把同时作废）。"
            )
        if data.get("changed"):
            return event.plain_result(f"{who} 已改为{label}（密钥与令牌不变）。")
        return event.plain_result(f"{who} 已经是{label}，无需改动。")

    @filter.command("比赛授权", alias={"授权赛事管理员", "授予赛事管理员", "设为赛事管理员"})
    async def cmd_grant(self, event: AstrMessageEvent):
        """把 @ 到的群友设为**赛事管理员**（还不是成员就自动建号）。只有服务器管理员能用。"""
        yield await self._grant(event, "event_admin")

    @filter.command("比赛添加", alias={"添加成员", "添加群友", "比赛添加成员", "设为成员"})
    async def cmd_add(self, event: AstrMessageEvent):
        """把 @ 到的群友添加为**普通成员**。只有服务器管理员能用。"""
        yield await self._grant(event, "member")

    # ------------------------------------------------------------------ #
    # 自助报名 / 取消报名（只改名单，不组队不定赛制；闸门与白名单都在站点侧判）
    # ------------------------------------------------------------------ #
    async def _signup_text(self, event, action: str, token: str) -> str:
        """报名 / 取消报名的**纯文本结果**（命令与 LLM 工具共用这一份）。

        两条命令、一个工具，都只差「谁来送」这一段文本：判断与文案只有这里一处
        （两处各写一遍，迟早会出现「命令里说不行、工具里说行」这种自相矛盾）。

        **这里不发任何消息**：命令拿到它自己发（:meth:`_signup`），工具直接把它回给大模型。

        **群号要一起报上去**：站点用它判「报名白名单」——白名单群里的非成员也能报上
        （站点顺手把他建成成员，登录密钥私聊给本人）。私聊没有群号，站点按「只认成员」处理。

        插件这一侧**一条闸门都不自己判**（届次是不是筹备中、能不能取消、白名单），
        全交给站点：判两遍迟早漂移，而「能不能报名」这件事只该有一个答案。
        """
        target, error = await self._resolve_event(token)
        if error:
            return error
        data = await self._post(
            "signup",
            {
                "qq": _sender_id(event),
                "action": action,
                "event": target,
                "group": _group_id(event),
                "name": _sender_name(event),
            },
        )
        if not data.get("ok"):
            return str(data.get("error") or "操作失败")
        return str(data.get("text") or "操作完成")

    async def _signup(self, event, action: str, token: str):
        """命令用：把 :meth:`_signup_text` 的结果原样回群。"""
        return event.plain_result(await self._signup_text(event, action, token))

    @filter.command("比赛报名", alias={"我要报名", "报名", "比赛我要报名"})
    async def cmd_signup(self, event: AstrMessageEvent, event_id: str = ""):
        """报名成为某一届的参赛选手（**只对筹备中的届开放**）。

        只把你加进这一届的参赛名单：**不组队、不定赛制**——那些等报名结束由管理员在
        网站上安排。开赛 / 结束之后名单冻住，命令会被站点拒绝并说清原因。

        白名单群里的新朋友也能直接报名：站点顺手建成员，登录密钥私聊发本人。
        """
        yield await self._signup(event, "join", event_id)

    @filter.command("比赛取消报名", alias={"取消报名", "退赛", "我不打了"})
    async def cmd_cancel_signup(self, event: AstrMessageEvent, event_id: str = ""):
        """取消报名：只取消你在**这一届**的参赛资格。

        成员身份与选手资料都留着（名字 / 头像 / 游戏 UUID 不是报名的一部分），
        队伍与赛程一个字都不动——想回来再发一次「比赛报名」即可。
        """
        yield await self._signup(event, "cancel", event_id)

    @filter.command("比赛我的", alias={"我的推流", "我的直播间", "推流地址", "我的地址"})
    async def cmd_mine(self, event: AstrMessageEvent):
        """查**自己**的推流地址与直播间地址（私聊发给你，不刷群）。"""
        who = _sender_id(event)
        if not who:
            yield event.plain_result("没识别到你的 QQ，请稍后再试。")
            return
        data = await self._get("my-links", qq=who)
        if not data.get("ok"):
            yield event.plain_result(str(data.get("error") or "查询失败"))
            return
        text = "\n\n".join(data.get("parts") or []) or "（没有可显示的内容）"
        sent = await self._notify(who, text)
        if sent.get("ok"):
            yield event.plain_result("你的推流与直播间地址已私聊发给你，去查收。")
            return
        # 私聊发不出去（没加好友 / 没配推送）→ 直接回在这里，别让人干等。
        # 推流地址本身不是密码（令牌才是），所以退化成群内回复是可接受的。
        yield event.plain_result(
            f"（私聊没发出去：{sent.get('error') or '未知原因'}，直接回在这里）\n{text}"
        )

    # ------------------------------------------------------------------ #
    # 自助凭据：本人换自己的密钥 / 令牌 / 推流码
    # ------------------------------------------------------------------ #
    async def _credential(self, event, what: str, value: str = ""):
        """把「改凭据」这件事交给站点，群里只报结果。

        不 @ 人 = 改自己；@ 了人 = 替 TA 改（**只有服务器管理员能这么做**，站点那侧判定）。
        新值**谁都不经过**：站点直接私聊发给**被改的那个人**，响应里没有明文——
        所以插件连「不小心打进群」的机会都没有，管理员也不会看到别人的新密钥 /
        令牌。这是**结构上**的保证，不靠这里的自觉。

        还要说清一件事：私聊发不出去时，凭据**已经换好了**（旧值已作废），
        否则本人会以为「没生效」而反复重试——每重试一次就白作废一把。
        """
        who = _sender_id(event)
        if not who:
            yield event.plain_result("没识别到你的 QQ，请稍后再试。")
            return
        targets = _at_targets(event)
        target = targets[0] if targets else ""
        data = await self._post(
            "credential", {"qq": who, "targetQq": target, "what": what, "value": value}
        )
        if not data.get("ok"):
            yield event.plain_result(str(data.get("error") or "操作失败"))
            return
        note = str(data.get("note") or "已处理")
        other = bool(data.get("forOther"))
        if data.get("sent"):
            if other:
                yield event.plain_result(
                    f"{note}；新值已私聊发给 TA 本人（不经过你，你也看不到）——"
                    "让 TA 自己去查收。"
                )
                return
            yield event.plain_result(f"{note}；已私聊发给你，去查收（里面的注意事项一起看下）。")
            return
        if other:
            yield event.plain_result(
                f"{note}，但私聊没能发给 TA（{data.get('detail') or '未知原因'}）。\n"
                "让 TA 加机器人好友后自己发一次同样的命令，或你再加一次"
                "——旧的那份已经作废，务必让 TA 拿到新的。"
            )
            return
        yield event.plain_result(
            f"{note}，但私聊没能发出去（{data.get('detail') or '未知原因'}）。\n"
            "凭据不能在群里发，所以：先加机器人好友，再发一次同样的命令就能拿到新的。"
        )

    @filter.command("比赛重置密钥", alias={"重置登录密钥", "我的密钥", "换密钥", "比赛密钥"})
    async def cmd_rotate_key(self, event: AstrMessageEvent):
        """换一把**自己**的登录密钥（新密钥只私聊发给你，旧密钥立即失效）。

        也可以 ``@某人`` 替 TA 换——**只有服务器管理员**能这么做，而且新密钥仍然只发给 TA 本人。
        """
        async for item in self._credential(event, "key"):
            yield item

    @filter.command("比赛重置令牌", alias={"重置直播令牌", "我的令牌", "换令牌", "比赛令牌"})
    async def cmd_rotate_token(self, event: AstrMessageEvent):
        """换一把**自己**的直播令牌（Bearer；新令牌只私聊发给你，要先有推流码）。

        ``@某人`` 可替 TA 换（仅服务器管理员），新令牌同样只发给 TA 本人。
        """
        async for item in self._credential(event, "token"):
            yield item

    @filter.command("比赛改推流码", alias={"修改推流码", "设置推流码", "我的推流码", "比赛推流码"})
    async def cmd_set_stream_key(self, event: AstrMessageEvent, stream_key: str = ""):
        """改**自己**的推流码（推流 ID）；用法：@机器人 比赛改推流码 新流名

        ``@某人`` 可替 TA 改（仅服务器管理员）；改完的结果仍然只私聊发给 TA 本人。
        """
        key = (stream_key or "").strip()
        if not key:
            yield event.plain_result(
                "用法：@机器人 比赛改推流码 你的流名\n"
                "（流名只能用英文、数字、连字符(-)与下划线(_)；改完 OBS 里的服务器地址要一起换）"
            )
            return
        async for item in self._credential(event, "streamId", key):
            yield item

    # ------------------------------------------------------------------ #
    # 资料 / 直播注册 / 游戏 UUID：用户自助（管理员可 @ 代办，私信仍只发本人）
    # ------------------------------------------------------------------ #
    async def _profile_text(self, event, target: str, args: list[str]) -> tuple[dict, str, str]:
        """查 / 改资料的**纯文本结果**：返回 ``(接口回的数据, 正文, 出错说明)``。

        命令与 LLM 工具共用这一份：命令拿到正文后**私聊**给本人（群里只报一句），
        工具直接把它回给大模型。出错说明非空就是失败了（``""`` = 成功）。

        正文里**没有密钥 / 令牌本身**（只有「有没有设置」），所以给工具用也安全——
        这条是站点侧保证的（见 ``/api/bot/profile``）。
        """
        who = _sender_id(event)
        if not who:
            return {}, "", "没识别到你的 QQ，请稍后再试。"
        if not args:
            data = await self._get("profile", qq=who, targetQq=target)
        else:
            data = await self._post(
                "profile",
                {
                    "qq": who,
                    "targetQq": target,
                    "field": args[0],
                    "value": " ".join(args[1:]) if len(args) > 1 else "",
                },
            )
        if not data.get("ok"):
            return data, "", str(data.get("error") or "操作失败")
        return data, "\n\n".join(data.get("parts") or []) or str(data.get("text") or ""), ""

    @filter.command("比赛资料", alias={"我的资料", "个人资料", "改资料", "比赛我的资料"})
    async def cmd_profile(self, event: AstrMessageEvent, field: str = "", value: str = "", more: str = ""):
        """查看 / 修改自己的资料（**私聊发你**）：QQ、名字、游戏 UUID、B站 房间号、推流码、直播间标题。

        用法一：``比赛资料`` —— 私聊发完整资料，并把「每一项怎么改」一起写清楚；
        用法二：``比赛资料 名字 新名字``（字段可以是 名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ）；
        用法三：``比赛资料 B站 清空`` —— 清掉那一项。

        为什么统一成这一条：以前「改推流码」单独一条、改名 / 改 UUID 没有入口，
        用户得记好几条命令。现在**一条命令一块表**，私聊那份资料里还写着每种改法。

        结果**只私聊发给被改的那个人**：服务器管理员 @ 某人 代办时，私聊也发给 TA
        （要发给谁由站点回的 ``toQq`` 决定，插件不会发错人）。
        """
        who = _sender_id(event)
        if not who:
            yield event.plain_result("没识别到你的 QQ，请稍后再试。")
            return
        targets = _at_targets(event)
        target = targets[0] if targets else ""
        args = [str(x).strip() for x in (field, value, more) if str(x or "").strip()]
        if target:
            # 被 @ 的人的 QQ 有时会混进参数里：它不是字段名，剔掉
            args = [x for x in args if not (x.isdigit() and x == target)]
        data, text, error = await self._profile_text(event, target, args)
        if error:
            yield event.plain_result(error)
            return
        to_qq = str(data.get("toQq") or target or who)
        other = bool(data.get("forOther"))
        changed = [str(x) for x in (data.get("changed") or [])]
        sent = await self._notify(to_qq, text)
        note = ("已改：" + "、".join(changed)) if changed else "资料"
        if sent.get("ok"):
            if other:
                yield event.plain_result(f"{note}；新资料只私聊发给了 TA 本人（你这边看不到）。")
                return
            yield event.plain_result(f"{note}已私聊发给你，去查收（每项怎么改也写在里面）。")
            return
        if other:
            yield event.plain_result(
                f"{note}；但私聊没能发给 TA（{sent.get('error') or '未知原因'}）。\n"
                "让 TA 加机器人好友后自己发一次「比赛资料」就能看到。"
            )
            return
        # 自己看自己的资料：没有敏感内容（密钥 / 令牌只有「有没有设置」），
        # 私聊发不出去时直接回在群里，别让人白等
        yield event.plain_result(
            f"（私聊没发出去：{sent.get('error') or '未知原因'}，直接回在这里）\n{text}"
        )

    @filter.command("比赛直播注册", alias={"直播注册", "开播注册", "注册直播", "比赛开播注册"})
    async def cmd_stream_setup(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """直播注册：把「开播要用的东西」一次给你（**缺什么补什么**）。

        三种情况：
        * 没推流码也没令牌 → 生成推流码 + 一把新令牌，连推流地址与注意事项一起私聊给你；
        * 只有推流码 → 推流码照用，补一把新令牌；
        * 两个都有 → **什么都不改**，只把推流码 / 推流地址 / 直播间地址与注意事项给你，
          并提醒「令牌忘了就再发一次这条命令」。

        还能顺手指定流名：``比赛直播注册 tom``。管理员 @ 某人 可代办，私聊仍只发 TA。
        """
        who = _sender_id(event)
        if not who:
            yield event.plain_result("没识别到你的 QQ，请稍后再试。")
            return
        targets = _at_targets(event)
        data = await self._post(
            "stream-setup",
            {
                "qq": who,
                "targetQq": targets[0] if targets else "",
                "streamKey": _stream_key_arg(arg1, arg2),
            },
        )
        if not data.get("ok"):
            yield event.plain_result(str(data.get("error") or "操作失败"))
            return
        note = str(data.get("note") or "已处理")
        other = bool(data.get("forOther"))
        if data.get("sent"):
            if other:
                yield event.plain_result(f"{note}——让 TA 去私聊查收（你这边看不到内容）。")
                return
            yield event.plain_result(f"{note}，去私聊查收（推流地址、令牌与注意事项都在里面）。")
            return
        # 私聊发不出去：**绝不把令牌打进群**（它等同于推流凭据，泄了就能顶掉他的画面）
        if other:
            yield event.plain_result(
                f"{note}；但私聊没能发给 TA（{data.get('detail') or '未知原因'}）。\n"
                "让 TA 加机器人好友后自己发一次「比赛直播注册」——那时才拿得到令牌。"
            )
            return
        yield event.plain_result(
            f"{note}；但私聊没能发出去（{data.get('detail') or '未知原因'}）。\n"
            "令牌不能在群里发，所以：先加机器人好友，再发一次「比赛直播注册」就能拿到。"
        )

    @filter.command("比赛UID", alias={"游戏UID", "游戏uuid", "我的UID", "异环UID", "比赛uid"})
    async def cmd_uid(self, event: AstrMessageEvent):
        """游戏 UUID：**群里直接回**——不用私聊、也不用管理员权限。

        不 @ 人 = 查自己；``比赛UID @某人`` = 查那个人。举办者收名单时要的就是这一串
        （游戏里加人得靠它），所以它必须能在群里当着人答出来。
        """
        data = await self._get("uid", qq=_sender_id(event), targetQq=(_at_targets(event) or [""])[0])
        if not data.get("ok"):
            yield event.plain_result(str(data.get("error") or "查询失败"))
            return
        for text in self._texts(data):
            yield event.plain_result(text)

    @filter.command("比赛", alias={"当前比赛", "赛事", "ntematch"})
    async def cmd_current(self, event: AstrMessageEvent):
        """当前赛事的信息 + 进度。"""
        async for block in self._run("detail"):
            yield self._emit(event, block)

    @filter.command(
        "比赛直播",
        alias={"直播", "当前直播", "谁在播", "谁在直播", "直播间", "开播了吗", "ntelive"},
    )
    async def cmd_live(self, event: AstrMessageEvent):
        """当前直播：主直播间是否开播 + 正在推流的选手 / 成员机位与观看地址。

        直播是**全局**信息（成员直播间不属于任何一届），所以这条命令不接届次。
        """
        for text in self._texts(await self._query("live")):
            yield event.plain_result(text)

    @filter.command(
        "比赛列表", alias={"全部比赛", "历届", "往届", "比赛目录", "nte列表", "有哪些比赛"}
    )
    async def cmd_list(self, event: AstrMessageEvent, page: str = ""):
        """全部赛事（分页）：`比赛列表 2` / `比赛列表 第2页` 都认。

        参数**刻意收成字符串**（而不是 int）：见 :func:`_page_number` 的说明——
        写成 int 时，非数字参数会让 AstrBot 的参数强转抛异常，命令直接失效。
        """
        data = await self._query("list", page=_page_number(page))
        for text in self._texts(data):
            yield event.plain_result(text)

    @filter.command("比赛届次", alias={"届次列表", "比赛编号"})
    async def cmd_ids(self, event: AstrMessageEvent, arg: str = "", page: str = ""):
        """届次编号列表（**填参数前先发它**）：`比赛届次` / `比赛届次 我的` / `比赛届次 2`。

        三种用法叠在一起：默认全部；写「我的」只看**自己创建的**届（按 QQ 认人，
        站点侧过滤）；页码翻页。**一页装不下时走私聊发本人**——在群里刷一长串届次
        纯属噪音，而喊命令的人多半只是想抄一个编号。私聊发不出去（没加好友 /
        没配推送）就退回群里发当前页。

        参数收成字符串、由 :func:`_ids_args` 宽松解析：顺序随意，认不出的词忽略。
        """
        scope, page_num = _ids_args(arg, page)
        sender = _sender_id(event)
        data = await self._query("ids", page=page_num, scope=scope, qq=sender)
        if not data.get("ok"):
            yield event.plain_result(f"查询失败：{data.get('error') or '未知原因'}")
            return
        texts = self._texts(data)
        pages = int(data.get("pages") or 1)
        if pages > 1 and sender:
            # 多于一页 → 私聊；群里只留一句「发到私聊了」+ 下一页怎么写
            sent = await self._notify(sender, "\n".join(texts))
            if sent.get("ok"):
                nxt = min(int(data.get("page") or 1) + 1, pages)
                mine = "我的 " if scope == "mine" else ""
                yield event.plain_result(
                    f"届次较多（共 {pages} 页），已私聊发你第 {data.get('page')} 页；"
                    f"还要看就发「比赛届次 {mine}{nxt}」。"
                )
                return
            logger.info("[NTE 比赛] 届次私聊失败，改为群内回复：%s", sent.get("error"))
        for text in texts:
            yield event.plain_result(text)

    @filter.command("比赛信息", alias={"赛事信息", "比赛时间", "nteginfo"})
    async def cmd_event(self, event: AstrMessageEvent, event_id: str = ""):
        """某一届的比赛信息：**发一张卡片图**（名字 / 时间 / 赛制 / 人数 + 完整比赛规则）。"""
        async for block in self._run("event", event_id):
            yield self._emit(event, block)

    @filter.command(
        "比赛进度",
        alias={"进度", "赛程", "赛程进度", "现在打谁", "nte进度", "打到哪了", "谁领先", "什么情况"},
    )
    async def cmd_progress(self, event: AstrMessageEvent, event_id: str = ""):
        """赛程进展：已赛多少、正在打谁 vs 谁。"""
        async for block in self._run("progress", event_id):
            yield self._emit(event, block)

    @filter.command(
        "比赛下一场", alias={"下一场", "下场", "接下来", "现在打", "接着打谁", "等下打谁"}
    )
    async def cmd_next(self, event: AstrMessageEvent, event_id: str = ""):
        """接下来看哪一场（正在打就报正在打的）。"""
        async for block in self._run("next", event_id):
            yield self._emit(event, block)

    @filter.command(
        "比赛结果", alias={"结果", "成绩", "比分", "nte结果", "赢了吗", "什么比分", "结果咋样"}
    )
    async def cmd_result(self, event: AstrMessageEvent, event_id: str = ""):
        """比赛结果：冠军 / 榜单 + 逐场比分。"""
        async for block in self._run("result", event_id):
            yield self._emit(event, block)

    @filter.command("比赛冠军", alias={"冠军", "榜首", "谁赢了"})
    async def cmd_champion(self, event: AstrMessageEvent, event_id: str = ""):
        """冠军（锦标赛）或榜首前三（积分制）。"""
        async for block in self._run("champion", event_id):
            yield self._emit(event, block)

    @filter.command(
        "比赛名单", alias={"参赛名单", "选手名单", "比赛选手", "队伍", "都有谁"}
    )
    async def cmd_roster(self, event: AstrMessageEvent, event_id: str = ""):
        """参赛名单（选手 / 队伍 / 替补）。"""
        async for block in self._run("roster", event_id):
            yield self._emit(event, block)

    @filter.command("比赛详情", alias={"赛事详情", "场次详情", "单场"})
    async def cmd_detail(self, event: AstrMessageEvent, event_id: str = "", ref: str = ""):
        """某一届的详情；带场次编号（如 L-1）时细说那一场。"""
        async for block in self._run("detail", event_id, ref):
            yield self._emit(event, block)

    @filter.command("比赛UUID", alias={"选手UUID", "UUID列表", "全部UUID", "nteuuid"})
    async def cmd_uuids(self, event: AstrMessageEvent, event_id: str = ""):
        """本届参赛选手的游戏 UUID 清单：**每行一个「名字 UUID」**，可整段复制。

        与 `比赛UID` 的分工：那条查**一个人**（不 @ 就是你自己），这条一次列全本届
        选手——建局 / 加好友时要的就是这么一段纯文本。
        """
        async for block in self._run("uuids", event_id):
            yield self._emit(event, block)

    @filter.command("比赛召集", alias={"召集参赛", "集合", "喊人"})
    async def cmd_call(self, event: AstrMessageEvent, event_id: str = ""):
        """@ 本届参赛者，请他们到场准备。

        插件跑在 AstrBot 里面，所以这里用 ``At`` 消息组件发**真正的 @**——
        走 HTTP 推送时（OpenAPI 没有 at 段）做不到。

        两道闸门：**认身份**（只有**本届创建者**或服务器管理员能召集，按 QQ 对号）
        与**冷却**（同一会话最小间隔 + 每小时上限，防刷屏）。

        认身份的规则是「谁创建的届谁可以召集」：一位赛事管理员能召集的是*他自己创建
        那些届*，不是任何一届——所以被拒时把「你自己哪几届能召集」一并回给他
        （理由见 :func:`_call_denied_text`）。
        """
        target, error = await self._resolve_event(event_id)
        if error:
            yield event.plain_result(error)
            return
        # ① 认身份：召集会 @ 一大片人，不该谁都能触发。
        # 带上提问者的 QQ：被拒时站点会顺便回「他自己创建的届」，提示才说得具体
        # （见 _callable_events）。
        sender = _sender_id(event)
        managers = await self._get("managers", eventId=target, qq=sender)
        if not managers.get("ok"):
            yield event.plain_result(f"查询失败：{managers.get('error') or '未知原因'}")
            return
        allowed = {str(q) for q in (managers.get("qqs") or [])}
        if not allowed or sender not in allowed:
            yield event.plain_result(_call_denied_text(managers))
            return
        # ② 冷却：防刷屏（默认 60 秒间隔 / 每小时 6 次，可在插件配置里调）
        ok, reason = self._call_allowed(event)
        if not ok:
            yield event.plain_result(reason)
            return
        who = await self._get("participants", eventId=target)
        texts = self._texts(await self._query("call", target, at=False))  # 文本里不夹 CQ 码
        qqs = [str(q) for q in (who.get("qqs") or [])] if who.get("ok") else []
        if not qqs:
            for text in texts:
                yield event.plain_result(text)
            return
        try:
            # 真 @：消息链里放 At 组件（平台 / 版本不支持时退化成纯文本）
            chain = [At(qq=qq) for qq in qqs] + [Plain(text="\n" + "\n".join(texts))]
            yield event.chain_result(chain)
        except Exception as exc:  # noqa: BLE001  (不支持 At 时别把命令打挂)
            logger.warning("[NTE 比赛] At 组件不可用，退化为纯文本：%s", exc)
            head = "".join(f"@{qq}" for qq in qqs)
            yield event.plain_result(f"{head}\n" + "\n".join(texts))

    # ------------------------------------------------------------------ #
    # LLM 工具（可选）：让大模型也能查这些数据
    # ------------------------------------------------------------------ #
    # 改这一节之前先读这四条——它们每一条都有测试盯着（tests/test_plugin_helpers.py）：
    #
    # 1. **只回文本**（``return str``）：AstrBot 把返回值当「工具结果」交给大模型，由它按
    #    **当前人格**说出最终那句话。工具里**不许** yield / ``plain_result`` / 私聊 / 停事件
    #    —— 那等于绕开人格自己发言，还会和模型的话重复一遍；
    # 2. **不碰系统提示词与人格**：不注册 ``on_llm_request``、不读不写 persona。工具**只**
    #    在「大模型决定调它」时执行，人格怎么说话完全不受影响（工具描述也只写「能查什么」，
    #    不写任何语气 / 角色要求）；
    # 3. **不给凭据、不给「喊话」能力**：密钥 / 令牌 / 推流地址 / 改推流码 / 直播注册 / 召集
    #    一律只走命令。工具结果会进大模型的上下文，而它会照着重述到群里；
    # 4. **参数定义来自 docstring**（不是类型注解）：AstrBot 解析 ``Args:`` 里的
    #    ``参数名(类型): 说明``，类型只能是 string / number / boolean / object / array
    #    （一层泛型如 ``array[string]`` 也行）。写漏类型会在**装饰时**抛异常 →
    #    插件加载失败 → 连命令一起没。所以参数名必须与签名逐个对上、说明写一行。
    #
    # 想让人格**完全看不见**这些工具：AstrBot 人格设定里有「工具」范围（不填 = 全部；
    # 空 = 一个都不用；填名字 = 白名单），或在 WebUI「函数工具」里逐个关。
    # 模型不支持 function calling 时 AstrBot 自己会去掉工具，命令照常。
    @_llm_tool("nte_query")
    async def tool_query(
        self,
        event: AstrMessageEvent,
        kind: str = "progress",
        event_id: str = "",
        match: str = "",
        page: float = 1,
        mine: bool = False,
    ) -> str:
        """查「NTE 比赛」的赛事数据（届次、赛程进度、对阵结果、冠军、名单、直播等）。

        只转述查到的内容，别自己编比分 / 编名字。用户没说清是「哪一届」时先问一句，
        或先查 kind=ids 拿到编号。

        Args:
            kind(string): 查什么：event 届次信息 / progress 赛程进度 / next 下一场 / result 结果 / champion 冠军 / roster 参赛名单 / detail 单届详情（配 match 细说那一场）/ live 当前直播 / list 全部赛事 / ids 届次编号 / uuids 选手 UUID 清单
            event_id(string): 届次：编号（如 e001）或名称片段（如 春节）。live / list / ids 不用填
            match(string): 场次编号（如 L-1 / 八强赛-1），只在 kind=detail 时用
            page(number): 页码，从 1 开始，只在 kind=list / ids 时用
            mine(boolean): 只要「自己创建的届」，只在 kind=ids 时用
        """
        key = (kind or "").strip().lower()
        if key not in _AGENT_KINDS:
            return f"kind 只能是 {' / '.join(_AGENT_KINDS)}（收到的是「{kind}」）。"
        try:
            number = max(1, int(float(page or 1)))
        except (TypeError, ValueError):  # 模型给了胡话：按第 1 页查，别把它变成一次失败
            number = 1
        scope = "mine" if (mine and key == "ids") else "all"
        qq = _sender_id(event) if scope == "mine" else ""
        return await self._agent_query(key, event_id, match, number, scope, qq)

    @_llm_tool("nte_signup")
    async def tool_signup(
        self, event: AstrMessageEvent, event_id: str = "", action: str = "join"
    ) -> str:
        """替发消息这个人报名 / 取消报名某一届比赛（只改参赛名单，不组队、不定赛制）。

        只在用户明确说要报名或退赛时调用；被拒时（不是筹备中 / 已经组队 / 不在白名单群
        且还不是成员）把给的原因照实说清，别当成报名成功。

        Args:
            event_id(string): 届次：编号（如 e001）或名称片段（如 春节）
            action(string): join 报名 / cancel 取消报名
        """
        act = (action or "join").strip().lower()
        if act not in ("join", "cancel"):
            return "action 只能是 join（报名）或 cancel（取消报名）。"
        return await self._signup_text(event, act, event_id)

    @_llm_tool("nte_me")
    async def tool_me(self, event: AstrMessageEvent, field: str = "", value: str = "") -> str:
        """查 / 改发消息这个人自己的站内资料（名字、游戏 UUID、B站 房间号、推流码等）。

        不带 field 就是查看（内容里不含密钥 / 令牌，只说「有没有设置」）；带 field 才是修改，
        只在用户明确要求时改。只能改自己的，别人的资料请让他自己发命令。

        Args:
            field(string): 要改的字段：名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ（不填 = 只看不改）
            value(string): 新的值；清空某一项就写 清空
        """
        args = [str(x).strip() for x in (field, value) if str(x or "").strip()]
        data, text, error = await self._profile_text(event, "", args)
        if error:
            return error
        changed = [str(x) for x in (data.get("changed") or [])]
        head = f"已改：{'、'.join(changed)}。\n" if changed else ""
        return head + text

    @_llm_tool("nte_help")
    async def tool_help(self, event: AstrMessageEvent) -> str:
        """这机器人能做的事（命令清单）+ 哪些在群里回、哪些只私聊发本人。

        用户问「你能干什么」「怎么报名」「有什么命令」时用它，挑相关的几条说，别整段念完；
        清单里没有的能力就是没有，别自己发明。
        """
        return HELP_TEXT

    # ------------------------------------------------------------------ #
    # 真 @ 投递：站点排队，**这里**来发
    #
    # 为什么非得插件发：AstrBot 的 OpenAPI（`POST /api/v1/im/message`）**没有 at 段**
    # ——它的消息段解析只认 plain / image / record / file / video，而且是严格模式
    # （塞 at 直接报错）。所以站点从外面怎么发都 @ 不到人，只能把 `[CQ:at,qq=…]`
    # 写进文本（多数 OneBot 实现不解析数组段里的 CQ 码）。
    # 真 @ 只能在 AstrBot **进程内部**用 `At` 组件发，而那正是本插件呆的地方：
    # 站点把「要 @ 谁 + 正文」排进队列（站点侧 app/outbox.py），这里每隔几秒取一次、
    # 发出去、再回执。站点那边等到期还没回执就自己退回文本写法，消息不会因为插件挂了而丢。
    # ------------------------------------------------------------------ #
    def _start_outbox(self) -> None:
        """起后台取件任务（已经在跑就什么都不做）。"""
        if self._outbox_task is not None and not self._outbox_task.done():
            return
        try:
            self._outbox_task = asyncio.create_task(self._outbox_loop())
        except RuntimeError as exc:  # 没有事件循环（少见的加载方式）：不起就不起
            logger.warning("[NTE 比赛] 真 @ 投递没起来：%s", exc)

    async def _outbox_loop(self) -> None:
        interval = max(2, int(_conf(self.config, "outbox_interval", 5) or 5))
        logger.info("[NTE 比赛] 真 @ 投递已启动 | 每 %s 秒取一次件", interval)
        while True:
            try:
                await self._outbox_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  (取件失败不能把循环打停)
                logger.debug("[NTE 比赛] 取件失败（下一轮再试）：%s", exc)
            await asyncio.sleep(interval)

    async def _outbox_once(self) -> int:
        """取一轮件并逐条发出去，返回发出几条。"""
        data = await self._get("outbox", limit=10)
        if not data.get("ok"):
            return 0
        sent = 0
        for item in data.get("items") or []:
            ok, detail = await self._deliver(item)
            # 回执：发不出去的由站点**立刻**退回文本写法重发（见站点侧 app/outbox.py）
            await self._post("outbox/ack", {"id": item.get("id"), "ok": ok, "detail": detail})
            if ok:
                sent += 1
        return sent

    async def _deliver(self, item: dict) -> tuple[bool, str]:
        """发一条：``At`` 组件（**这才是真 @**）在前，正文在后。"""
        umo = str(item.get("umo") or "")
        if not umo:
            return False, "这条消息没有目标会话"
        mentions = [str(q) for q in (item.get("mentions") or []) if str(q).strip()]
        body = str(item.get("body") or "")
        chain = [At(qq=qq) for qq in mentions]
        if body:
            # 开头那个零宽空格不是装饰：aiocqhttp 会把 Plain 的首尾空白去掉，
            # 直接拼 "\n" 会被吃掉，@ 和正文就挤在同一行了。
            chain.append(Plain(text="\u200b\n" + body))
        try:
            await self.context.send_message(umo, _wrap_chain(chain))
        except Exception as exc:  # noqa: BLE001  (发失败要回报给站点，由它兜底)
            logger.warning("[NTE 比赛] 真 @ 投递失败：%s", exc)
            return False, f"{type(exc).__name__}: {exc}"
        logger.info("[NTE 比赛] 真 @ 已投递 | @ %d 人 | %s", len(mentions), umo)
        return True, ""

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """插件加载后**探活一次**：地址或令牌配错时，立刻在日志里说清楚。

        站点侧专门留了 ``/api/bot/ping`` 就是给这一步用的——否则要等到群友发命令，
        才发现「令牌忘了填」，而且错误只能靠 401 / 403 的文案去猜。
        """
        # 真 @ 投递先起：站点地址 / 令牌配错也先让它跑着（配好之后下一轮自己就接上了）
        if bool(_conf(self.config, "outbox_enabled", True)):
            self._start_outbox()
        try:
            data = await self._get("ping")
        except Exception as exc:  # noqa: BLE001  (探活失败绝不能影响插件加载)
            logger.warning("[NTE 比赛] 探活异常（忽略）：%s", exc)
            return
        if not data.get("ok"):
            logger.warning("[NTE 比赛] 自检未通过：%s", data.get("error") or "未知原因")
            return
        current = (data.get("currentEvent") or {}).get("name") or "—"
        logger.info(
            "[NTE 比赛] 已连接站点 | 站点当前届=%s | 共 %s 届",
            current,
            data.get("eventCount") or 0,
        )

    async def terminate(self):
        """插件卸载 / 停用：停掉取件任务，再把复用的连接池关掉。"""
        task = self._outbox_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001  (收尾失败不值得报错)
                logger.debug("[NTE 比赛] 停投递任务失败（忽略）", exc_info=True)
        self._outbox_task = None
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001  (关闭失败不值得报错)
                logger.debug("[NTE 比赛] 关闭连接池失败（忽略）", exc_info=True)
        logger.info("[NTE 比赛] 插件已停用")
