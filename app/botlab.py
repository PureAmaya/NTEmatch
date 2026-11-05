"""调试模式下的「模拟群」：``/bot`` 里像 QQ 群一样发一句话，看机器人怎么回。

这里只做两件事：

* **认命令**：把群友打的那句话（``比赛`` / ``进度`` / ``结果`` …）翻译成站点侧的查询类型；
* **留记录**：环形缓冲（用户消息 + 机器人的回复），页面按聊天窗口画出来。

回复内容**不在这里拼**：认出来之后交给 :func:`app.qqbot_api.build_message`——那是真实推送
用的同一条链路，所以群里会收到什么，模拟器里就显示什么。

命令别名与 AstrBot 插件（``integrations/astrbot_plugin_nte_match``）保持一致：
两边靠 ``tests/test_botlab.py`` 里那条「逐条比对插件源码」的用例钉住，改了插件忘了改这里会红。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from . import debug, qqbot

#: 缓冲上限：够看清一次对话的前后文，又不会把内存当日志文件用
MAX_MESSAGES = 120

#: 「发给谁」：群里 / 私聊。只影响页面上的标注，不参与组装。
SCENES = ("group", "private")

#: 消息来源：模拟器里的用户发言 / 机器人回复 / 真实推送 / 手动分段
SOURCES = ("user", "preview", "push", "text")

#: 角色：页面上决定气泡靠哪边
ROLES = ("user", "bot")

#: 查询类命令（别名 → 站点侧的 kind）。**别名与插件逐条对齐**，见模块说明。
QUERY_COMMANDS: dict[str, str] = {
    # 比赛信息（发卡片图）
    "比赛信息": "event",
    "赛事信息": "event",
    "比赛时间": "event",
    "nteginfo": "event",
    # 当届详情（比赛 / 赛事 就是这个）
    "比赛": "detail",
    "当前比赛": "detail",
    "赛事": "detail",
    "ntematch": "detail",
    "比赛详情": "detail",
    "赛事详情": "detail",
    "场次详情": "detail",
    "单场": "detail",
    # 直播
    "比赛直播": "live",
    "直播": "live",
    "当前直播": "live",
    "谁在播": "live",
    "谁在直播": "live",
    "直播间": "live",
    "开播了吗": "live",
    "ntelive": "live",
    # 列表 / 届次
    "比赛列表": "list",
    "全部比赛": "list",
    "历届": "list",
    "往届": "list",
    "比赛目录": "list",
    "nte列表": "list",
    "有哪些比赛": "list",
    "比赛届次": "ids",
    "届次列表": "ids",
    "比赛编号": "ids",
    # 进度 / 下一场 / 结果 / 冠军
    "比赛进度": "progress",
    "进度": "progress",
    "赛程": "progress",
    "赛程进度": "progress",
    "现在打谁": "progress",
    "nte进度": "progress",
    "打到哪了": "progress",
    "谁领先": "progress",
    "什么情况": "progress",
    "比赛下一场": "next",
    "下一场": "next",
    "下场": "next",
    "接下来": "next",
    "现在打": "next",
    "接着打谁": "next",
    "等下打谁": "next",
    "比赛结果": "result",
    "结果": "result",
    "成绩": "result",
    "比分": "result",
    "nte结果": "result",
    "赢了吗": "result",
    "什么比分": "result",
    "结果咋样": "result",
    "比赛冠军": "champion",
    "冠军": "champion",
    "榜首": "champion",
    "谁赢了": "champion",
    # 名单 / UUID / 召集
    "比赛名单": "roster",
    "参赛名单": "roster",
    "选手名单": "roster",
    "比赛选手": "roster",
    "队伍": "roster",
    "都有谁": "roster",
    "比赛uuid": "uuids",
    "选手uuid": "uuids",
    "uuid列表": "uuids",
    "全部uuid": "uuids",
    "nteuuid": "uuids",
    "比赛召集": "call",
    "召集参赛": "call",
    "集合": "call",
    "喊人": "call",
}

#: 帮助命令
HELP_COMMANDS = ("比赛帮助", "赛事帮助", "比赛命令", "比赛功能", "ntehelp", "比赛help", "帮助", "help")

#: 需要**真实群身份**的命令（报名 / 授权 / 我的直播间…）：站点侧要 QQ / 成员身份，
#: 模拟器不伪造身份，只如实说明「这条在群里才用得了」。
IDENTITY_COMMANDS: dict[str, str] = {
    "比赛授权": "授权赛事管理员",
    "授权赛事管理员": "授权赛事管理员",
    "授予赛事管理员": "授权赛事管理员",
    "设为赛事管理员": "授权赛事管理员",
    "比赛添加": "添加成员",
    "添加成员": "添加成员",
    "添加群友": "添加成员",
    "比赛添加成员": "添加成员",
    "设为成员": "添加成员",
    "比赛报名": "报名",
    "我要报名": "报名",
    "报名": "报名",
    "比赛我要报名": "报名",
    "比赛取消报名": "取消报名",
    "取消报名": "取消报名",
    "退赛": "取消报名",
    "我不打了": "取消报名",
    "比赛我的": "我的直播间",
    "我的推流": "我的直播间",
    "我的直播间": "我的直播间",
    "推流地址": "我的直播间",
    "我的地址": "我的直播间",
    "比赛重置密钥": "重置登录密钥",
    "重置登录密钥": "重置登录密钥",
    "我的密钥": "重置登录密钥",
    "换密钥": "重置登录密钥",
    "比赛密钥": "重置登录密钥",
    "比赛重置令牌": "重置直播令牌",
    "重置直播令牌": "重置直播令牌",
    "我的令牌": "重置直播令牌",
    "换令牌": "重置直播令牌",
    "比赛令牌": "重置直播令牌",
    "比赛改推流码": "修改推流码",
    "修改推流码": "修改推流码",
    "设置推流码": "修改推流码",
    "我的推流码": "修改推流码",
    "比赛推流码": "修改推流码",
    "比赛资料": "我的资料",
    "我的资料": "我的资料",
    "个人资料": "我的资料",
    "改资料": "我的资料",
    "比赛我的资料": "我的资料",
    "比赛直播注册": "直播注册",
    "直播注册": "直播注册",
    "开播注册": "直播注册",
    "注册直播": "直播注册",
    "比赛开播注册": "直播注册",
    "比赛uid": "我的 UID",
    "游戏uid": "我的 UID",
    "游戏uuid": "我的 UID",
    "我的uid": "我的 UID",
    "异环uid": "我的 UID",
}

_lock = threading.Lock()
_messages: deque[dict[str, Any]] = deque(maxlen=MAX_MESSAGES)
_next_id = 0


def active() -> bool:
    """模拟器开着吗（= 调试模式）。"""
    return debug.active()


def kinds() -> list[dict[str, str]]:
    """推送类型表：**直接取**机器人自己的 ``KIND_META``，不在这里另写一份。"""
    return [{"key": key, "name": name, "note": note} for key, (name, note) in qqbot.KIND_META.items()]


# --------------------------------------------------------------------------- #
# 认命令
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    """取第一个词并去掉群聊里常见的引导符（``/`` / ``！`` / ``。``）。"""
    parts = str(text or "").strip().split()
    if not parts:
        return ""
    return parts[0].lstrip("/!！.。，,、").strip()


def parse(text: str) -> tuple[str, str]:
    """把一句话翻译成 ``(动作, 值)``。

    动作只有四种：

    * ``("kind", "result")``   —— 查一条，交给 :func:`app.qqbot_api.build_message`；
    * ``("help", "")``         —— 要命令表；
    * ``("identity", "报名")`` —— 这条需要真实群身份，模拟器只解释；
    * ``("unknown", "")``      —— 没听懂。
    """
    token = _normalize(text)
    if not token:
        return "unknown", ""
    key = token.lower()
    if key in (cmd.lower() for cmd in HELP_COMMANDS):
        return "help", ""
    if key in QUERY_COMMANDS:
        return "kind", QUERY_COMMANDS[key]
    if key in IDENTITY_COMMANDS:
        return "identity", IDENTITY_COMMANDS[key]
    return "unknown", ""


def help_text() -> str:
    """命令表（群里发「比赛帮助」看到的那段）。

    直接渲染 ``app.helpcard_content.SECTIONS``：那份是站点侧**唯一**的命令清单
    （帮助图也是拿它画的，且由测试与插件的 ``HELP_TEXT`` 逐条对齐）。
    在这里另抄一份，早晚会出现「模拟群能听懂、群里却不认」的两套命令。
    """
    from . import helpcard_content as content

    lines = ["【NTE 比赛】命令一览（参数可省略，[ ] 里是可选）："]
    for title, rows in content.SECTIONS:
        lines.append("")
        lines.append(title)
        for cmd, desc in rows:
            lines.append(f"· {cmd} —— {desc}")
        note = content.SECTION_NOTES.get(title)
        if note:
            lines.append(f"  （{note}）")
    lines += [
        "",
        "模拟群回的是「看比赛」这一类的查询；报名 / 授权 / 我的直播间这类要在真实群里用——",
        "站点侧按 QQ / 成员身份认人，模拟器不伪造身份。",
    ]
    return "\n".join(lines)


def unknown_text() -> str:
    return "没听懂这句话。可用的有：比赛 / 进度 / 结果 / 名单 / 直播 / 冠军；发「比赛帮助」看全部。"


# --------------------------------------------------------------------------- #
# 缓冲
# --------------------------------------------------------------------------- #
def record(
    *,
    kind: str,
    parts: list[str] | tuple[str, ...] = (),
    title: str = "",
    image: dict[str, Any] | None = None,
    mentions: list[str] | tuple[str, ...] = (),
    scene: str = "group",
    source: str = "preview",
    role: str = "bot",
) -> dict[str, Any]:
    """记一条消息（用户或机器人），返回它（含自增 id 与时间）。

    空消息也记：真的遇到过「拼出来是空的」，看不见反而更难查。
    """
    global _next_id
    cleaned = [str(part) for part in parts if str(part).strip()]
    labels = {key: label for key, (label, _note) in qqbot.KIND_META.items()}
    name = title or labels.get(kind, kind)
    with _lock:
        _next_id += 1
        message = {
            "id": _next_id,
            "at": time.strftime("%H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "kind": kind,
            "title": name,
            "scene": scene if scene in SCENES else "group",
            "source": source if source in SOURCES else "preview",
            "role": role if role in ROLES else "bot",
            "parts": cleaned,
            "image": image,
            "mentions": [str(qq) for qq in mentions if str(qq).strip()],
            "chars": sum(len(part) for part in cleaned),
        }
        _messages.append(message)
    return message


def recent() -> list[dict[str, Any]]:
    """按发生顺序（旧 → 新）返回，页面从下往上长。"""
    with _lock:
        return list(_messages)


def clear() -> int:
    """清空缓冲，返回清掉几条。"""
    with _lock:
        count = len(_messages)
        _messages.clear()
    return count


def snapshot() -> dict[str, Any]:
    """给页面的一次性快照。"""
    return {
        "enabled": active(),
        "messages": recent(),
        "kinds": kinds(),
        "max": MAX_MESSAGES,
    }
