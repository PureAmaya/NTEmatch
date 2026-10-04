"""NTE 比赛 × AstrBot 插件。

把「NTE 比赛」平台的赛事数据接进群聊，**全部能力都是群命令**（``@filter.command``）：
不注册任何 ``llm_tool``、也不调用任何大模型——命中哪条命令、回什么，全由字符串匹配
与站点数据决定，同一个问题问两次结果一样，不依赖 LLM provider 是否配好。

唯一「不查数据」的是一条快捷键：**问帮助**可以直接回一张帮助图——默认就发**站点内置的
那张**（把图放成站点里的 ``static/help.jpg`` 即可，地址 ``/help.jpg``；没有图就回文字说明，
见 README 与 ``HELP.md``），方便贴群公告。

插件本身**不做业务计算**，只调站点的只读查询 API（``/api/bot/*``），
所以文案、赛制、分页逻辑全在站点那一侧，改一处两边同步。

安装：把本目录整个复制到 AstrBot 的 ``data/plugins/`` 下，然后在 AstrBot 的
插件配置里填 **站点地址** 与 **查询 API 令牌**（站点「服务器 → QQ 机器人」生成）。

源码：https://github.com/PureAmaya/NTEmatch （AGPL-3.0，© 早八时睡觉的你）
"""

from __future__ import annotations

import re
import time

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


# 帮助文本：**唯一的说明来源**——群里回的是它，``HELP.md``（做帮助图用）也照着它写。
# 改命令时顺手改这两处，别让它们各说一套。
#
# 分几块：命令清单 → 届次怎么写 → 怎么参加 → 怎么触发 → 什么会私聊发你。
HELP_TEXT = (
    "【NTE 比赛】可用命令：\n"
    "· 比赛 [届次] —— 该届的信息 + 进度\n"
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
    "· 比赛召集 [届次] —— @ 参赛者到场（仅本届举办者 / 服务器管理员，带冷却）\n"
    "· 比赛我的 —— 你自己的推流地址 + 直播间地址（私聊发你）\n"
    "· 比赛重置密钥 [@某人] —— 换登录密钥（旧密钥立即失效；私聊发本人）\n"
    "· 比赛重置令牌 [@某人] —— 换直播令牌（要先有推流码；私聊发本人）\n"
    "· 比赛改推流码 <流名> [@某人] —— 改推流码（英文 / 数字；令牌不变）\n"
    "· 比赛授权 @某人 —— 把群友设为赛事管理员（仅服务器管理员；不是成员会自动建号）\n"
    "· 比赛添加 @某人 —— 把群友添加为普通成员（仅服务器管理员；密钥私聊发给本人）\n"
    "· 比赛帮助 —— 就是本条（私聊发你）\n"
    "上面带 [届次] 的命令**必须写明哪一届**：写 e001 / 1 / 第2届 / 名称里的几个字都行\n"
    "（不知道有哪些届就发「比赛届次」）；不带 [届次] 的命令不用填。\n"
    "比赛直播与比赛列表是全局信息，不用填届次。\n"
    "—— 怎么参加 ——\n"
    "参赛不用自己注册：本届举办者在站点里把你排进名单就行，群里 @ 你就是要开打了。\n"
    "想用「比赛我的」查自己的推流地址，得先成为成员（服务器管理员发：比赛添加 @你）。\n"
    "「比赛我的 / 比赛重置密钥 / 比赛重置令牌 / 比赛改推流码」不 @ 人就是**只动自己那份**\n"
    "（按你发命令的 QQ 认人）；带上 @某人 才是替 TA 改，那要服务器管理员，新值也只发 TA 本人。\n"
    "—— 怎么触发 ——\n"
    "要先 @ 机器人 再说命令，或按 AstrBot 里设的唤醒前缀发（例如「/比赛进度」）。\n"
    "光打「比赛进度」不会触发：这是 AstrBot 的命令过滤规则（必须被 @ 或命中唤醒前缀），\n"
    "不是本插件能改的——不 @ 就不会有任何回复。\n"
    "—— 会私聊发给你的东西 ——\n"
    "命令说明、你自己的推流地址、新成员的登录密钥都走私聊（只该你看到）。\n"
    "私聊发不出去（没加机器人好友）时，地址与说明会退回群里；\n"
    "但**登录密钥不会**——密钥只显示一次，泄在群里等于白送一个账号。"
)


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
    """赛事查询：全部能力都是群命令，不依赖任何大模型。"""

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
        """公共流程：解析届次 → 查询 → 逐段产出文本。"""
        target, error = await self._resolve_event(event_id)
        if error:
            yield error
            return
        for text in self._texts(await self._query(kind, target, ref, page)):
            yield text

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
    async def cmd_list(self, event: AstrMessageEvent, page: str = ""):
        """全部赛事（分页）：`比赛列表 2` / `比赛列表 第2页` 都认。

        参数**刻意收成字符串**（而不是 int）：见 :func:`_page_number` 的说明——
        写成 int 时，非数字参数会让 AstrBot 的参数强转抛异常，命令直接失效。
        """
        data = await self._query("list", page=_page_number(page))
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
        # 不再标「哪一届是当前届」：届次现在必须写明，服务器那个指针跟群友无关
        lines = [f"共 {len(rows)} 届赛事（发命令时写明届次，编号或名称都行）："]
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

        两道闸门：**认身份**（只有本届举办者 / 服务器管理员能召集，按 QQ 对号）
        与**冷却**（同一会话最小间隔 + 每小时上限，防刷屏）。
        """
        target, error = await self._resolve_event(event_id)
        if error:
            yield event.plain_result(error)
            return
        # ① 认身份：召集会 @ 一大片人，不该谁都能触发
        managers = await self._get("managers", eventId=target)
        if not managers.get("ok"):
            yield event.plain_result(f"查询失败：{managers.get('error') or '未知原因'}")
            return
        allowed = {str(q) for q in (managers.get("qqs") or [])}
        if not allowed:
            yield event.plain_result(
                f"{managers.get('note') or '本届还没有登记 QQ 的赛事管理员'}。\n"
                "请先在站点「我的」页填上自己的 QQ（服务器管理员或本届举办者），再试一次。"
            )
            return
        if _sender_id(event) not in allowed:
            yield event.plain_result(
                "召集需要赛事管理员身份：只有本届举办者或服务器管理员能召集。\n"
                "（在站点「我的」页登记了 QQ 才能对上号；需要权限请联系服务器管理员。）"
            )
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
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """插件加载后**探活一次**：地址或令牌配错时，立刻在日志里说清楚。

        站点侧专门留了 ``/api/bot/ping`` 就是给这一步用的——否则要等到群友发命令，
        才发现「令牌忘了填」，而且错误只能靠 401 / 403 的文案去猜。
        """
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
        """插件卸载 / 停用：把复用的连接池关掉。"""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001  (关闭失败不值得报错)
                logger.debug("[NTE 比赛] 关闭连接池失败（忽略）", exc_info=True)
        logger.info("[NTE 比赛] 插件已停用")
