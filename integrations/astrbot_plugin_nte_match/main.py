"""NTE 比赛 × AstrBot 插件。

把「NTE 比赛」平台的赛事数据接进群聊，**同一份能力给两个入口**：

* **群命令**（打字即用，不走大模型）—— 由 ``@filter.command`` 注册，覆盖常见问法；
* **LLM 工具**（兜底）—— 由 ``@filter.llm_tool`` 注册，命令覆盖不到的自由问法交给它。

插件本身**不做业务计算**，只调站点的只读查询 API（``/api/bot/*``），
所以文案、赛制、分页逻辑全在站点那一侧，改一处两边同步。

安装：把本目录整个复制到 AstrBot 的 ``data/plugins/`` 下，然后在 AstrBot 的
插件配置里填 **站点地址** 与 **查询 API 令牌**（站点「服务器 → QQ 机器人」生成）。
"""

from __future__ import annotations

import re

import httpx
import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain

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


HELP_TEXT = (
    "【NTE 比赛】可用命令：\n"
    "· 比赛 —— 当前赛事的信息 + 进度\n"
    "· 比赛直播 —— 现在谁在直播（主直播间 + 选手 / 成员机位的观看地址）\n"
    "· 比赛列表 [页码] —— 全部赛事\n"
    "· 比赛信息 [届次] —— 时间 / 赛制 / 人数 / 简介 / 是否排名\n"
    "· 比赛进度 [届次] —— 已赛多少、正在打谁 vs 谁\n"
    "· 比赛下一场 [届次] —— 接下来看哪场（含计划时间）\n"
    "· 比赛结果 [届次] —— 冠军 / 榜单 + 逐场比分\n"
    "· 比赛详情 [届次] [场次] —— 综合信息；给场次编号就细说那一场\n"
    "· 比赛名单 [届次] —— 参赛名单（选手 / 队伍 / 替补）\n"
    "· 比赛冠军 [届次] —— 冠军（或积分制榜首前三）\n"
    "· 比赛届次 —— 全部届次的编号与名称（填参数用）\n"
    "· 比赛召集 [届次] —— @ 参赛者，请他们到场准备\n"
    "· 比赛帮助 —— 就是本条\n"
    "届次可以写 e001 / 1 / 第2届 / 名称里的几个字，不填就是当前主赛事；\n"
    "比赛直播与比赛列表是全局信息，不用填届次。"
)


class NTEMatchPlugin(star.Star):
    """赛事查询：群命令（优先）+ LLM 工具（兜底）。"""

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.base_url = str(_conf(self.config, "base_url", "http://127.0.0.1:8000")).rstrip("/")
        self.token = str(_conf(self.config, "api_token", ""))
        self.timeout = float(_conf(self.config, "timeout", 10))
        self.reply_prefix = str(_conf(self.config, "reply_prefix", ""))
        if not self.token:
            logger.warning("[NTE 比赛] 还没填查询 API 令牌，命令会报「未启用」")

    # ------------------------------------------------------------------ #
    # 与站点通信
    # ------------------------------------------------------------------ #
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _get(self, path: str, **params) -> dict:
        """调一次站点接口；失败时返回 ``{"ok": False, "error": "..."}``（不抛异常）。"""
        url = f"{self.base_url}/api/bot/{path}"
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self._headers, params=clean)
        except httpx.HTTPError as exc:
            logger.warning("[NTE 比赛] 请求失败 %s：%s", url, exc)
            return {"ok": False, "error": f"连不上比赛平台（{exc}）"}
        if resp.status_code == 401:
            return {"ok": False, "error": "查询令牌不正确，请到站点重新生成后在插件配置里更新"}
        if resp.status_code == 403:
            return {"ok": False, "error": "查询接口未启用：请先在站点「服务器 → QQ 机器人」生成令牌"}
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = str(body.get("error") or body.get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:120]
            return {"ok": False, "error": f"平台返回 HTTP {resp.status_code}：{detail}"}
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "error": "平台返回的不是 JSON"}

    async def _query(
        self, kind: str, event_id: str = "", ref: str = "", page: int = 1, at: bool = True
    ) -> dict:
        return await self._get(
            "query", kind=kind, eventId=event_id, ref=ref, page=page, at="" if at else "0"
        )

    def _texts(self, data: dict) -> list[str]:
        """把接口回的分段拼上前缀；出错时只回一行错误说明。"""
        if not data.get("ok"):
            return [f"查询失败：{data.get('error') or '未知原因'}"]
        parts = data.get("parts") or []
        if not parts:
            return ["（没有可显示的内容）"]
        return [f"{self.reply_prefix}{part}" if self.reply_prefix else part for part in parts]

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

        支持：留空（当前主赛事）/ ``e001`` / ``1`` / ``第2届`` / 名称里的几个字。
        **找不到时会列出可用编号**——用户自己就能改对，不用去问大模型。
        """
        raw = (token or "").strip()
        rows, error = await self._events()
        if error:
            return "", error
        if not raw:
            return "", ""
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
        """公共流程：解析届次 → 查询 → 逐段产出文本。"""
        target, error = await self._resolve_event(event_id)
        if error:
            yield error
            return
        for text in self._texts(await self._query(kind, target, ref, page)):
            yield text

    # ------------------------------------------------------------------ #
    # 群命令
    # ------------------------------------------------------------------ #
    @filter.command("比赛帮助", alias={"赛事帮助", "比赛命令", "比赛功能", "ntehelp", "比赛help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """列出可用命令。"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("比赛", alias={"当前比赛", "赛事", "ntematch"})
    async def cmd_current(self, event: AstrMessageEvent):
        """当前赛事的信息 + 进度。"""
        async for text in self._run("detail"):
            yield event.plain_result(text)

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
    async def cmd_list(self, event: AstrMessageEvent, page: int = 1):
        """全部赛事（分页）。"""
        data = await self._query("list", page=max(1, int(page or 1)))
        for text in self._texts(data):
            yield event.plain_result(text)

    @filter.command("比赛届次", alias={"届次列表", "比赛编号"})
    async def cmd_ids(self, event: AstrMessageEvent):
        """列出全部届次的编号与名称（填参数时用）。"""
        rows, error = await self._events()
        if error:
            yield event.plain_result(error)
            return
        if not rows:
            yield event.plain_result("平台上还没有赛事。")
            return
        data = await self._get("events")
        lines = [f"共 {len(rows)} 届（当前主赛事：{data.get('current') or '—'}）"]
        for item in sorted(rows, key=lambda x: str(x.get("id", ""))):
            mark = "（当前）" if item.get("id") == data.get("current") else ""
            state = {"active": "进行中", "closed": "已结束", "draft": "筹备中"}.get(item.get("status"), "")
            lines.append(f"· {item['id']} {item.get('name')}{mark} · {state}")
        yield event.plain_result(self.reply_prefix + "\n".join(lines))

    @filter.command("比赛信息", alias={"赛事信息", "比赛时间", "nteginfo"})
    async def cmd_event(self, event: AstrMessageEvent, event_id: str = ""):
        """某一届的比赛信息。"""
        async for text in self._run("event", event_id):
            yield event.plain_result(text)

    @filter.command(
        "比赛进度",
        alias={"进度", "赛程", "赛程进度", "现在打谁", "nte进度", "打到哪了", "谁领先", "什么情况"},
    )
    async def cmd_progress(self, event: AstrMessageEvent, event_id: str = ""):
        """赛程进展：已赛多少、正在打谁 vs 谁。"""
        async for text in self._run("progress", event_id):
            yield event.plain_result(text)

    @filter.command(
        "比赛下一场", alias={"下一场", "下场", "接下来", "现在打", "接着打谁", "等下打谁"}
    )
    async def cmd_next(self, event: AstrMessageEvent, event_id: str = ""):
        """接下来看哪一场（正在打就报正在打的）。"""
        async for text in self._run("next", event_id):
            yield event.plain_result(text)

    @filter.command(
        "比赛结果", alias={"结果", "成绩", "比分", "nte结果", "赢了吗", "什么比分", "结果咋样"}
    )
    async def cmd_result(self, event: AstrMessageEvent, event_id: str = ""):
        """比赛结果：冠军 / 榜单 + 逐场比分。"""
        async for text in self._run("result", event_id):
            yield event.plain_result(text)

    @filter.command("比赛冠军", alias={"冠军", "榜首", "谁赢了"})
    async def cmd_champion(self, event: AstrMessageEvent, event_id: str = ""):
        """冠军（锦标赛）或榜首前三（积分制）。"""
        async for text in self._run("champion", event_id):
            yield event.plain_result(text)

    @filter.command(
        "比赛名单", alias={"参赛名单", "选手名单", "比赛选手", "队伍", "都有谁"}
    )
    async def cmd_roster(self, event: AstrMessageEvent, event_id: str = ""):
        """参赛名单（选手 / 队伍 / 替补）。"""
        async for text in self._run("roster", event_id):
            yield event.plain_result(text)

    @filter.command("比赛详情", alias={"赛事详情", "场次详情", "单场"})
    async def cmd_detail(self, event: AstrMessageEvent, event_id: str = "", ref: str = ""):
        """某一届的详情；带场次编号（如 L-1）时细说那一场。"""
        async for text in self._run("detail", event_id, ref):
            yield event.plain_result(text)

    @filter.command("比赛召集", alias={"召集参赛", "集合", "喊人"})
    async def cmd_call(self, event: AstrMessageEvent, event_id: str = ""):
        """@ 本届参赛者，请他们到场准备。

        插件跑在 AstrBot 里面，所以这里用 ``At`` 消息组件发**真正的 @**——
        走 HTTP 推送时（OpenAPI 没有 at 段）做不到。
        """
        target, error = await self._resolve_event(event_id)
        if error:
            yield event.plain_result(error)
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
    # LLM 工具（命令覆盖不到的自由问法，交给大模型兜底）
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="nte_match_query")
    async def tool_query(
        self,
        event: AstrMessageEvent,
        kind: str = "detail",
        event_id: str = "",
        ref: str = "",
        page: int = 1,
    ) -> str:
        """查询 NTE 比赛平台的数据。想回答「比赛进行到哪了 / 谁在打 / 结果如何 / 有哪些比赛」时调用。

        Args:
            event(object): 消息事件上下文（框架注入，不用填）。
            kind(string): 查询类型：event（信息）/ live（当前直播）/ progress（进度）/ result（结果）/ detail（综合）/ next（下一场）/ roster（名单）/ champion（冠军）/ list（全部赛事）/ call（召集文案）。
            event_id(string): 届次，可以是 e001、1、第2届 或名称片段；留空表示当前主赛事。
            ref(string): 场次编号，例如 L-1；仅在 kind=detail 时用于细看某一场。
            page(int): 页码，从 1 开始；仅在 kind=list 时有效。
        """
        allowed = {
            "event",
            "live",
            "progress",
            "result",
            "detail",
            "list",
            "call",
            "next",
            "roster",
            "champion",
        }
        key = kind if kind in allowed else "detail"
        target, error = await self._resolve_event(event_id)
        if error:
            return error
        data = await self._query(key, target, ref, page)
        if not data.get("ok"):
            return f"查询失败：{data.get('error') or '未知原因'}"
        return data.get("text") or "（没有可显示的内容）"

    @filter.llm_tool(name="nte_match_events")
    async def tool_events(self, event: AstrMessageEvent) -> str:
        """列出 NTE 比赛平台上的全部赛事（编号 / 名称 / 状态 / 规模 / 榜首）。需要挑选某一届再深入查询时先调用它。

        Args:
            event(object): 消息事件上下文（框架注入，不用填）。
        """
        data = await self._get("events")
        if not data.get("ok"):
            return f"查询失败：{data.get('error') or '未知原因'}"
        rows = data.get("events") or []
        if not rows:
            return "平台上还没有赛事。"
        lines = [f"共 {len(rows)} 届赛事（当前主赛事：{data.get('current') or '—'}）"]
        for item in rows:
            bits = [
                "当前" if item.get("current") else "",
                {"active": "进行中", "closed": "已结束", "draft": "筹备中"}.get(item.get("status"), ""),
                "娱乐模式" if item.get("ranked") is False else "排名制",
                f"{item.get('players') or 0} 人",
                f"已赛 {item.get('played') or 0}/{item.get('rounds') or 0}",
            ]
            line = f"· {item.get('id')} {item.get('name')}：" + "，".join(dict.fromkeys(b for b in bits if b))
            if item.get("brief"):
                line += f"（{item['brief']}）"
            lines.append(line)
        return "\n".join(lines)

    async def terminate(self):
        """插件卸载 / 停用（本插件没有常驻资源，留空即可）。"""
        logger.info("[NTE 比赛] 插件已停用")
