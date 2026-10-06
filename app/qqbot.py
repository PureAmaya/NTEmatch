"""QQ 机器人（AstrBot）推送：把比赛信息 / 进度 / 召集 / 结果 / 列表推到群里。

对接 AstrBot 的 **OpenAPI**（v4.18+，见官方 wiki「dev-openapi」）：

| 项 | 值 |
| --- | --- |
| 鉴权 | `Authorization: Bearer abk_xxx`（也支持 `X-API-Key`） |
| 发消息 | `POST {base}/api/v1/im/message`，body `{"umo": "...", "message": "..."}` |
| 目标会话 | `umo` = `平台:消息类型:会话标识`，如 `aiocqhttp:GroupMessage:123456789` |

设计取舍：

* **设置存在数据库 meta（``qqbot``）**：跟着备份 / 还原一起走，还原后不用重配；
  API Key 只进不出——接口只回 ``hasKey`` 布尔，绝不回明文；
* **消息分段**：单条有长度上限，超长自动按行切成多条依次发送；比赛列表额外支持翻页；
* **@ 人**：AstrBot 的 OpenAPI 目前**没有 at 消息段**（只有
  plain / reply / image / record / file / video），所以「@」做成三种写法可切换：
  ``cq``（默认，按 OneBot v11 惯例把 ``[CQ:at,qq=…]`` 写进文本）、
  ``text``（退化成 ``@QQ号`` 纯文本）、``none``（只列名字，不 @）；
  设置页有「发送测试」按钮，一试就知道你的适配器吃哪种。
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from datetime import datetime
from typing import Any

import httpx

from . import logic, metrics
from .defaults import sport_meta
from .logging_conf import get_logger
from .models import Config

log = get_logger("qqbot")

# 全局 meta 键：QQ 机器人（AstrBot）接入配置
QQBOT_KEY = "qqbot"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    # AstrBot 面板地址（不带尾部斜杠）。出厂**留空**：每套部署的 AstrBot 地址都不一样，
    # 预填某个具体地址会让人「看着配好了、其实把消息（连同 API Key）发到别人的机器人上」。
    "baseUrl": "",
    # 在 AstrBot → WebUI → 设置 → OpenAPI 里创建，形如 abk_xxx
    "apiKey": "",
    # 目标会话：可以填完整 UMO，也可以只填群号（按 platform 拼成 …:GroupMessage:群号）
    "umo": "",
    "platform": "aiocqhttp",
    # 发送接口路径（不同版本可能不同，留默认即可）
    "path": "/api/v1/im/message",
    # @ 的写法：cq / text / none
    "atMode": "cq",
    # 单条消息上限（字符），超出自动分段
    "maxChars": 1200,
    "timeout": 10,
    # 只读查询 API 令牌的**加盐哈希**（给 AstrBot 插件调用，明文形如 nte_xxx）；
    # 为空 = 关闭查询 API。明文只在生成那一次显示，接口只回 hasBotToken。
    "botApiTokenHash": "",
    # ---- 频率限制（防止把机器人刷到被平台风控）----
    # 两次推送之间的最小间隔（秒），0 = 不限制间隔
    "cooldownSeconds": 20,
    # 每个自然小时最多推送几次
    "maxPerHour": 30,
    # 单次推送最多分几段（分段之间还会各停 0.5 秒）
    "maxParts": 8,
    # ---- 图片推送（比赛卡片，见 app/card.py）----
    # 开：比赛信息先发一张卡片图（信息 + 完整规则），再跟一行说明；
    # 关：只发文字（文本里本来就带规则摘要，信息一条不少）。
    # 图发不出去（没装 Pillow / AstrBot 不收图片段）时**自动退回文字**，与这个开关无关。
    "imageCards": True,
    # ---- 赛前提醒（见 app/remind.py）----
    # 开关：到点自动在群里 @ 举办者。需要「已启用推送」+ 举办者登记了 QQ 才发得出去。
    "remindEnabled": True,
    # 提前量（分钟，逗号分隔）：默认「前一天」与「前 2 小时」。
    # 只在【提前量 - 1 小时, 提前量】这个窗口内发——服务器中途重启，
    # 不会把「明天开赛」这条补发成「还有 3 小时开赛」。
    "remindLeads": "1440,120",
}
SETTINGS_KEYS = tuple(DEFAULT_SETTINGS)
# 这些键不接受前端回填（避免把「已配置」的 Key 用空串覆盖掉）
SECRET_KEYS = ("apiKey", "botApiTokenHash")


def new_bot_token() -> str:
    """生成只读查询 API 的令牌（给 AstrBot 插件配置用）。"""
    return "nte_" + secrets.token_urlsafe(24)

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ROUND_STATUS = {"pending": "待赛", "live": "进行中", "done": "已结束"}
EVENT_STATUS = {"draft": "筹备中", "active": "进行中", "closed": "已结束"}


# --------------------------------------------------------------------------- #
# 纯文本化
#
# QQ 不认 Markdown：`**加粗**`、`## 标题`、`| 表格 |` 发过去就是一串符号。
# 所有出站消息都在这里过一道，把 Markdown 语法收拾成纯文本——消息构建器照常
# 用最自然的写法，格式问题只在这一个地方兜住。
# --------------------------------------------------------------------------- #
_MD_FENCE = re.compile(r"^\s*```[^\n]*\n(.*?)^\s*```\s*$", re.MULTILINE | re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_MD_BOLD = re.compile(r"\*{1,3}([^*\n]+)\*{1,3}")
_MD_UNDERLINE_BOLD = re.compile(r"__([^_\n]+)__")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_STRIKE = re.compile(r"~~([^~\n]+)~~")
# 逐行处理的几条（行内正则不好区分「表格分隔行」和「正文里的竖线」）
_MD_HEAD = re.compile(r"^#{1,6}\s*")
_MD_QUOTE = re.compile(r"^>\s?")
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_MD_RULE = re.compile(r"^\s*([-*_]){3,}\s*$")
_TABLE_SEP_ROW = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _plain_line(line: str) -> str:
    """单行去 Markdown：标题井号 / 引用箭头 / 列表符号 / 表格行。"""
    out = _MD_QUOTE.sub("", _MD_HEAD.sub("", line))
    stripped = out.strip()
    # 表格：`| a | b |` → `a · b`；分隔行 `| --- | --- |` 整行丢掉
    if stripped.startswith("|") and stripped.count("|") >= 2:
        if "-" in stripped and _TABLE_SEP_ROW.match(stripped):
            return ""
        cells = [cell.strip() for cell in stripped.strip("|").split("|") if cell.strip()]
        return " · ".join(cells)
    if "-" in out and _TABLE_SEP_ROW.match(out):
        return ""
    if _MD_RULE.match(out):
        return ""
    return _MD_BULLET.sub(r"\1· ", out)


def to_plain_text(text: str) -> str:
    """把 Markdown 语法收拾成**纯文本**（QQ / 群聊不认 Markdown）。

    只去掉「标记」，保留内容：`**粗**` → `粗`、`## 标题` → `标题`、
    `[文字](链接)` → `文字（链接）`、表格行去掉竖线、列表符号统一成 `· `。
    多余的连续空行会压成一个空行——QQ 里空行太多很难看。
    """
    out = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = _MD_FENCE.sub(lambda m: m.group(1), out)
    out = _MD_IMAGE.sub(r"\1", out)
    out = _MD_LINK.sub(r"\1（\2）", out)
    out = _MD_STRIKE.sub(r"\1", out)
    out = _MD_BOLD.sub(r"\1", out)
    out = _MD_UNDERLINE_BOLD.sub(r"\1", out)
    out = _MD_CODE.sub(r"\1", out)
    out = "\n".join(_plain_line(line) for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def normalize_settings(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """把前端提交的设置规整成可存储的结构（只认已知键）。"""
    clean = dict(current)
    internal = bool((patch or {}).get("__internal__"))
    for key in SETTINGS_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "apiKey":
            # 空串 = 「不改」，避免前端把已有的 Key 抹掉
            text = str(value or "").strip()
            if text:
                clean[key] = text
            continue
        if key in SECRET_KEYS:
            # botApiTokenHash 之类：只能由服务端生成 / 清除，前端回填一律忽略
            if internal:
                clean[key] = str(value or "")
            continue
        if key in ("enabled", "remindEnabled", "imageCards"):
            # 注意：**别让布尔键落到下面的 else**——那里会把 False 存成字符串 "False"，
            # 而字符串恒为真，开关就再也关不掉了。
            clean[key] = bool(value)
        elif key == "remindLeads":
            # 只留数字与逗号：写错的（比如「一天, 2小时」）宁可回落到默认值，
            # 也不要存成一个永远解析不出提前量的配置
            raw = re.sub(r"[^\d,，]", "", str(value or "")).replace("，", ",")
            parsed = {
                int(part)
                for part in raw.split(",")
                if part.strip().isdigit() and 5 <= int(part) <= 20160
            }
            clean[key] = ",".join(str(x) for x in sorted(parsed, reverse=True)) or str(
                DEFAULT_SETTINGS[key]
            )
        elif key == "maxChars":
            # 下限与 split_message 的硬下限一致（200），否则「每页几届」和
            # 「单条切多长」两处口径会打架，算出来的页反而塞不进一条消息
            clean[key] = max(200, min(6000, int(value or DEFAULT_SETTINGS[key])))
        elif key == "timeout":
            clean[key] = max(3, min(6000, int(value or DEFAULT_SETTINGS[key])))
        elif key in ("cooldownSeconds", "maxPerHour", "maxParts"):
            clean[key] = max(0 if key == "cooldownSeconds" else 1, min(3600, int(value or 0)))
        else:
            text = str(value or "").strip()
            clean[key] = text or DEFAULT_SETTINGS[key]
    clean["baseUrl"] = clean["baseUrl"].rstrip("/")
    if not clean["path"].startswith("/"):
        clean["path"] = "/" + clean["path"]
    return clean


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """给前端看的设置：**抹掉 API Key**，只回「配没配」。

    ``umo`` 是解析后的目标会话（给用户看），``umoRaw`` 是原始输入（用于回填表单）。
    """
    data = {k: v for k, v in settings.items() if k not in SECRET_KEYS}
    data["hasKey"] = bool(settings.get("apiKey"))
    data["hasBotToken"] = bool(settings.get("botApiTokenHash"))
    data["umoRaw"] = str(settings.get("umo") or "")
    data["umo"] = resolved_umo(settings)
    return data


def resolved_umo(settings: dict[str, Any]) -> str:
    """把「群号」或「完整 UMO」统一成 UMO 字符串。

    群号（纯数字）按 ``{platform}:GroupMessage:{群号}`` 拼；其它值原样使用。
    """
    raw = str(settings.get("umo") or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        platform = str(settings.get("platform") or "aiocqhttp").strip() or "aiocqhttp"
        return f"{platform}:GroupMessage:{raw}"
    return raw


def merge_settings(stored: dict[str, Any] | None) -> dict[str, Any]:
    """存量设置 + 默认值（老配置缺字段时补上）。"""
    settings = dict(DEFAULT_SETTINGS)
    for key, value in (stored or {}).items():
        if key in SETTINGS_KEYS and value is not None:
            settings[key] = value
    return settings


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
async def send_text(
    text: str,
    *,
    settings: dict[str, Any],
    umo: str = "",
) -> dict[str, Any]:
    """把一条文本发给群。返回 ``{ok, status, detail}``（**不抛异常**，调用方看 ok）。

    出站前统一过一次 :func:`to_plain_text`——QQ 不认 Markdown，这一步是最后一道闸。
    """
    text = to_plain_text(text)
    target = umo or resolved_umo(settings)
    result: dict[str, Any] = {"ok": False, "status": 0, "detail": "", "umo": target}
    if not settings.get("enabled"):
        result["detail"] = "未启用 QQ 机器人推送"
        return result
    if not settings.get("apiKey"):
        result["detail"] = "未配置 AstrBot API Key"
        return result
    # 地址必须显式检查：留空时 httpx 会抛 UnsupportedProtocol（**不是** HTTPError，
    # 不会被下面的 except 接住），最后变成 500。宁可在这里给一句人话。
    base = str(settings.get("baseUrl") or "").strip()
    if not base:
        result["detail"] = "未配置 AstrBot 地址（到「服务器 → QQ 机器人」填写）"
        return result
    if not target:
        result["detail"] = "未配置目标会话（群号 / UMO）"
        return result

    path = str(settings.get("path") or "/api/v1/im/message")
    url = f"{base.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {settings['apiKey']}",
        "X-API-Key": str(settings["apiKey"]),
        "Content-Type": "application/json",
    }
    timeout = float(settings.get("timeout") or 10)
    # 先按「纯文本」发；万一该版本只收消息段数组，再退回数组形态重试一次
    bodies: list[Any] = [
        {"umo": target, "message": text},
        {"umo": target, "message": [{"type": "plain", "text": text}]},
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, body in enumerate(bodies):
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                result["detail"] = f"请求 AstrBot 失败：{exc}"
                log.warning("QQ 推送失败 | %s | %s", url, exc)
                return result
            result["status"] = resp.status_code
            if resp.status_code < 400:
                result["ok"] = True
                log.info(
                    "QQ 推送成功 | umo=%s | 形态=%s | 长度=%d",
                    target,
                    "text" if index == 0 else "segments",
                    len(text),
                )
                return result
            result["detail"] = _error_text(resp)
            # 只有「请求体格式不对」才值得换形态重试
            if resp.status_code not in (400, 415, 422):
                break
    log.warning("QQ 推送失败 | umo=%s | HTTP %s | %s", target, result["status"], result["detail"])
    return result


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200] or f"HTTP {resp.status_code}"
    detail = data.get("detail") or data.get("error") or data.get("message") or data
    if isinstance(detail, list):  # FastAPI 校验错误
        detail = "；".join(str(item.get("msg") or item) for item in detail)
    return f"HTTP {resp.status_code}：{detail}"


def split_message(text: str, limit: int = 1200) -> list[str]:
    """按行把长消息切成多条（单行本身就超长时硬切）。

    QQ / AstrBot 单条消息有长度上限，比赛列表或逐场结果很容易超；
    宁可多发几条，也不要被截断。
    """
    limit = max(200, int(limit or 1200))
    lines = str(text or "").splitlines()
    parts: list[str] = []
    buf = ""
    for line in lines:
        while len(line) > limit:  # 超长单行：先硬切
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            if buf:
                parts.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts or [""]


class PushLimiter:
    """推送限流器（内存态，进程重启即清零——限流本来就是「当下」的事）。

    一道闸门管住**所有会让机器人发消息的动作**，避免短时间内反复请求把机器人
    刷到被 QQ / 平台风控：

    * ``cooldownSeconds``：两次推送之间的最小间隔，防手滑连点；
    * ``maxPerHour``：每个自然小时最多推几次，防长时间高频刷群；
    * ``maxParts``：单次最多分几段（分段之间另有 0.5 秒间隔），防一次把群刷屏。

    一次「推送」无论切成几段都只算 **1 次**；**预览不占额度**（它不碰机器人），
    而**测试发送占额度**（它真的发了消息）。失败也会占额度——请求确实打到机器人了。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._times: list[float] = []

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        self._times = [t for t in self._times if t > cutoff]

    @staticmethod
    def _budget(settings: dict[str, Any]) -> tuple[int, int]:
        cooldown = max(0, int(settings.get("cooldownSeconds") or 0))
        per_hour = max(1, int(settings.get("maxPerHour") or 30))
        return cooldown, per_hour

    async def acquire(self, settings: dict[str, Any]) -> tuple[bool, str, int]:
        """占用一次推送额度：返回 ``(是否放行, 拒绝原因, 建议等待秒数)``。

        判定与记账在同一把锁里完成，两个并发请求不会同时挤过去。
        """
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            cooldown, per_hour = self._budget(settings)
            if cooldown and self._last and now - self._last < cooldown:
                wait = int(cooldown - (now - self._last)) + 1
                return False, f"推送太频繁：请等 {wait} 秒（最小间隔 {cooldown} 秒）", wait
            if len(self._times) >= per_hour:
                wait = int(3600 - (now - self._times[0])) + 1
                return (
                    False,
                    f"本小时已推送 {len(self._times)} 次（上限 {per_hour} 次）：请等 {wait} 秒",
                    wait,
                )
            self._last = now
            self._times.append(now)
            return True, "", 0

    async def snapshot(self, settings: dict[str, Any]) -> dict[str, Any]:
        """当前额度状态（面板展示「还要等多久 / 本小时还能推几次」）。"""
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            cooldown, per_hour = self._budget(settings)
            left = 0
            if cooldown and self._last and now - self._last < cooldown:
                left = int(cooldown - (now - self._last)) + 1
            return {
                "cooldownSeconds": cooldown,
                "maxPerHour": per_hour,
                "maxParts": max(1, int(settings.get("maxParts") or 8)),
                "cooldownLeft": left,
                "usedLastHour": len(self._times),
                "remaining": max(0, per_hour - len(self._times)),
            }


# 全局唯一：所有推送共用同一份额度
limiter = PushLimiter()


async def send_parts(parts: list[str], *, settings: dict[str, Any], umo: str = "") -> dict[str, Any]:
    """依次发送多段；任一段失败即停止并回报失败原因。"""
    sent = 0
    for part in parts:
        res = await send_text(part, settings=settings, umo=umo)
        if not res["ok"]:
            return {**res, "sent": sent, "total": len(parts)}
        sent += 1
        if sent < len(parts):
            await asyncio.sleep(0.5)  # 别把群刷屏 / 触发风控
    return {"ok": True, "status": 200, "detail": "", "sent": sent, "total": len(parts), "umo": umo}


#: 图片消息段里「图在哪」的字段名（各版本叫法不一，逐个试，见 send_image）
_IMAGE_KEYS = ("file", "url", "image")


async def send_image(url: str, *, settings: dict[str, Any], umo: str = "") -> dict[str, Any]:
    """发一张图（就是本站生成的比赛卡片，``url`` 由 :mod:`app.card` 给出）。

    与 :func:`send_text` 同一条规矩：**逐个形态试**，谁被接受就用谁——
    AstrBot 各版本对图片段的字段名不完全一致（``file`` / ``url`` / ``image``），
    而这里没法像文本那样「等价替换」（发不出图就是发不出），所以只能试完再说。

    失败**不抛异常**，只回 ``ok=False``：调用方据此退回纯文本推送（信息一条不少）。
    """
    target = umo or resolved_umo(settings)
    result: dict[str, Any] = {"ok": False, "status": 0, "detail": "", "umo": target}
    if not str(url or "").strip():
        result["detail"] = "没有图片地址"
        return result
    if not settings.get("enabled"):
        result["detail"] = "未启用 QQ 机器人推送"
        return result
    if not settings.get("apiKey"):
        result["detail"] = "未配置 AstrBot API Key"
        return result
    base = str(settings.get("baseUrl") or "").strip()
    if not base:
        result["detail"] = "未配置 AstrBot 地址（到「服务器 → QQ 机器人」填写）"
        return result
    if not target:
        result["detail"] = "未配置目标会话（群号 / UMO）"
        return result

    path = str(settings.get("path") or "/api/v1/im/message")
    endpoint = f"{base.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {settings['apiKey']}",
        "X-API-Key": str(settings["apiKey"]),
        "Content-Type": "application/json",
    }
    timeout = float(settings.get("timeout") or 10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for key in _IMAGE_KEYS:
            body = {"umo": target, "message": [{"type": "image", key: url}]}
            try:
                resp = await client.post(endpoint, headers=headers, json=body)
            except httpx.HTTPError as exc:
                result["detail"] = f"请求 AstrBot 失败：{exc}"
                log.warning("QQ 图片推送失败 | %s | %s", endpoint, exc)
                return result
            result["status"] = resp.status_code
            if resp.status_code < 400:
                result["ok"] = True
                result["shape"] = key
                log.info("QQ 图片推送成功 | umo=%s | 字段=%s", target, key)
                return result
            result["detail"] = _error_text(resp)
            # 只有「请求体格式不对」才值得换个字段名重试
            if resp.status_code not in (400, 415, 422):
                break
    log.warning("QQ 图片推送失败 | umo=%s | HTTP %s | %s", target, result["status"], result["detail"])
    return result


# --------------------------------------------------------------------------- #
# 文本工具
# --------------------------------------------------------------------------- #
def _fmt_dt(raw: str, *, with_weekday: bool = True, with_time: bool = True) -> str:
    dt = logic.parse_time(raw or "")
    if dt is None:
        return ""
    text = f"{dt.year}年{dt.month}月{dt.day}日"
    if with_weekday:
        text += f"（{_WEEKDAYS[dt.weekday()]}）"
    if with_time:
        text += f" {dt.strftime('%H:%M')}"
    return text


def _human_delta(target: str, now: datetime | None = None) -> str:
    """「还有 3 天 2 小时」/「已开始 1 小时 20 分」。"""
    dt = logic.parse_time(target or "")
    if dt is None:
        return ""
    now = now or datetime.now()  # noqa: DTZ005
    delta = dt - now
    future = delta.total_seconds() > 0
    seconds = abs(int(delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    chunks = []
    if days:
        chunks.append(f"{days} 天")
    if hours:
        chunks.append(f"{hours} 小时")
    if minutes and not days:
        chunks.append(f"{minutes} 分")
    if not chunks:
        chunks.append("不到 1 分钟")
    return ("还有 " if future else "已开始 ") + " ".join(chunks)


def _format_label(cfg: Config) -> str:
    return "积分制" if cfg.rules.format == "league" else "锦标赛制"


def _mode_label(cfg: Config) -> str:
    return "启用排名（竞技）" if cfg.event.ranked else "娱乐模式（不排名，只记录）"


def _join_names(players: list[dict[str, Any]]) -> str:
    names = [str(p.get("name") or p.get("id") or "") for p in players]
    names = [n for n in names if n]
    return "、".join(names) if names else "待定"


def _player_name(cfg: Config, player_id: str) -> str:
    """选手 ID → 展示名（积分榜的行里只有 ``playerId``，没有内嵌选手对象）。"""
    player = next((p for p in cfg.players if p.id == player_id), None)
    return (player.display_name if player is not None else "") or player_id or "—"


def _side_text(side: dict[str, Any]) -> str:
    """一方的显示名：有队伍就用队名，没有就列出场选手。"""
    label = str(side.get("label") or "").strip()
    players = side.get("players") or []
    if label and not re.fullmatch(r"[AB]\s*队", label):
        return label
    return _join_names(players)


def _round_line(
    rnd: dict[str, Any], *, with_stage: bool = True, scoring: object = None
) -> str:
    """`八强赛 · 甲队 2:1 乙队` 这样的一行。

    比分按计分口径显示：时间型写 ``1:23.456``、小数按小数写；**填了轮次**时
    ``score`` 是「赢的轮数」（计数），一律按整数显示。
    """
    sc = metrics.as_scoring(scoring) if scoring is not None else metrics.Scoring()
    sides = rnd.get("sides") or []
    left = _side_text(sides[0]) if sides else "待定"
    right = _side_text(sides[1]) if len(sides) > 1 else "待定"
    counted = bool(rnd.get("sets"))
    # 「有没有比分」要用录入痕迹判断：数值型的 0 是合法读数，不能用真假值糊过去
    scored = rnd.get("status") == "done" or any(sc.has_entered(s.get("score")) for s in sides)
    scores = ""
    if scored and len(sides) > 1:
        scores = (
            f" {sc.format_score(sides[0].get('score', 0), counted=counted)}"
            f":{sc.format_score(sides[1].get('score', 0), counted=counted)}"
        )
    head = f"{rnd.get('stageName')} · " if with_stage and rnd.get("stageName") else ""
    label = rnd.get("label") or rnd.get("code") or ""
    return f"{head}{label} {left}{scores} vs {right}".replace("  ", " ").strip()


# --------------------------------------------------------------------------- #
# 消息构建
# --------------------------------------------------------------------------- #
#: 比赛规则里**跟「怎么打、怎么晋级」直接相关**的几节（推送时按这个顺序摘）
_RULE_SECTIONS = ("赛制概览", "小组赛", "淘汰赛", "积分与排名", "娱乐模式（不排名）")


def rules_digest(cfg: Config, limit: int = 14) -> str:
    """比赛规则的**摘要**（给纯文本推送用）；``limit`` 是行数上限。

    规则本身全部由 :func:`app.logic.rulebook` 按当前赛制现算——**不存文案、不写死**，
    所以改了人数 / 分组 / 计分口径，这里跟着变，不会出现「规则说的和实际打的不一样」。

    为什么是摘要而不是全文：规则十几条，全塞进群消息会被切成好几段刷屏
    （推送本身还有单次段数上限）。全文由**推送图片**承载（见 :mod:`app.card`），
    这里只留「怎么打、怎么晋级、怎么判」这几条，末尾一句指向完整规则。
    """
    try:
        rb = logic.rulebook(cfg)
    except Exception:
        log.warning("比赛规则摘要生成失败（跳过这一段）", exc_info=True)
        return ""
    lines: list[str] = []
    for section in rb.get("sections") or []:
        if str(section.get("title") or "") not in _RULE_SECTIONS:
            continue
        for item in section.get("items") or []:
            text = " ".join(str(item or "").split())
            # 摘要里不该出现 Markdown 记号（群消息是纯文本，星号只会显得莫名其妙）
            for token in ("**", "*", "`"):
                text = text.replace(token, "")
            if not text:
                continue
            lines.append(text)
            if len(lines) >= limit:
                break
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "—— 比赛规则（摘要）——\n" + "\n".join(f"· {line}" for line in lines) + (
        f"\n（共 {len(lines)} 条要点；完整规则见推送图片与站点「比赛规则」）"
    )


def card_parts(kind: str, card: dict[str, Any] | None, parts: list[str]) -> list[str]:
    """有卡片时，``比赛信息`` 的正文**只留一行说明**。

    信息与规则全在图里了，再补一屏文字只是刷屏；图没发出去时（不支持图片 / 拉不到图）
    调用方原样用 ``parts``，信息一条不少——这个判断故意放在调用方（它才知道图发成功了没）。
    """
    if card and kind == "event" and card.get("caption"):
        return [str(card["caption"])]
    return parts


def build_event_message(cfg: Config, state: dict[str, Any]) -> str:
    """比赛信息：名字 / 赛制 / 时间（年月日 + 星期几 + 起止 + 倒计时）/ 人数 / 简介 / 排名。"""
    evt = cfg.event
    name = evt.name or evt.title or "比赛"
    start = _fmt_dt(evt.start_time)
    end = _fmt_dt(evt.end_time)
    if start:
        when = f"{start} 开始"
        if end:
            when += f"，{end} 结束"
        delta = _human_delta(evt.start_time)
        if delta:
            when += f"（{delta}）"
    elif end:
        when = f"结束时间 {end}"
    else:
        when = "时间待定"

    players = logic.joined_players(cfg)
    per_match = max(2, min(6, cfg.rules.teams_per_match or 2))
    shape = "组 vs 组" if per_match == 2 else f"{per_match} 队同场"
    format_line = (
        f"{_format_label(cfg)} · 每方 {cfg.rules.team_size} 人"
        + (
            f" · 共 {cfg.rules.total_rounds} 局"
            if cfg.rules.format == "league"
            else f" · 小组赛每场 {shape} · {'双败' if cfg.rules.loser_bracket else '单败'}淘汰"
        )
    )
    lines = [
        f"【NTE 比赛】{name}",
        f"时间：{when}",
        f"赛制：{format_line}",
        f"参赛：{len(players)} 人"
        + (f" / {len(cfg.teams)} 支队伍" if cfg.teams else ""),
        f"排名：{_mode_label(cfg)}",
    ]
    extra = [x for x in (evt.venue, evt.organizer) if x]
    if extra:
        lines.append("场地/主办：" + " · ".join(extra))
    if evt.brief:
        lines.append(f"简介：{evt.brief}")
    if cfg.event.status == "closed":
        lines.append("状态：已结束")
    # 比赛规则（摘要）：随赛制自动生成，见 rules_digest
    rules = rules_digest(cfg)
    if rules:
        lines.append("")
        lines.append(rules)
    return "\n".join(lines)


def build_progress_message(cfg: Config, state: dict[str, Any]) -> str:
    """赛程进度：已赛 / 总数 + 正在打的是谁 vs 谁（+ 下一场）。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    rounds = state.get("rounds") or []
    total = len(rounds)
    done = sum(1 for r in rounds if r.get("status") == "done")
    live = [r for r in rounds if r.get("status") == "live"]
    pending = [r for r in rounds if r.get("status") == "pending"]
    percent = f"{round(done * 100 / total)}%" if total else "0%"
    lines = [
        f"【NTE 比赛】{name} · 赛程进度",
        f"进度：已赛 {done} / {total} 场（{percent}）"
        + (f"，进行中 {len(live)} 场" if live else ""),
    ]
    if live:
        lines.append("正在打：")
        lines.extend(f"· {_round_line(r, scoring=cfg.rules.scoring)}" for r in live[:4])
    elif pending:
        lines.append(f"下一场：{_round_line(pending[0], scoring=cfg.rules.scoring)}")
    else:
        lines.append("赛程已全部结束" if total else "赛程还没生成")
    champion = state.get("champion")
    if champion:
        lines.append(f"冠军：{champion.get('name') or champion.get('id')}")
    if state.get("format", {}).get("kind") == "league":
        leader = (state.get("standings") or {}).get("leader")
        if leader and leader.get("playerId"):
            lines.append(
                f"当前榜首：{_player_name(cfg, leader['playerId'])}"
                f"（{leader.get('played', 0)} 场 · 均分 {leader.get('average', 0)}）"
            )
    return "\n".join(lines)


def participant_qqs(cfg: Config, members: list[Any] | None = None) -> list[str]:
    """参与名单里各位的 QQ：优先成员关联的 QQ，其次选手自己填的 QQ，去重保序。"""
    by_uid = {m.uid: m for m in (members or [])}
    out: list[str] = []
    for player in logic.joined_players(cfg):
        candidates = []
        member = by_uid.get(player.member_uid)
        if member is not None:
            candidates.append(member.qq)
        candidates.append(player.qq)
        for qq in candidates:
            clean = "".join(ch for ch in str(qq or "") if ch.isdigit())
            if len(clean) >= 5 and clean not in out:
                out.append(clean)
                break
    return out


def at_text(qqs: list[str], settings: dict[str, Any]) -> str:
    """按设置生成 @ 片段（AstrBot 的 OpenAPI 没有 at 段，所以写在文本里）。"""
    mode = str(settings.get("atMode") or "cq")
    if mode == "none" or not qqs:
        return ""
    if mode == "text":
        return " ".join(f"@{qq}" for qq in qqs)
    return "".join(f"[CQ:at,qq={qq}]" for qq in qqs)


def private_umo(settings: dict[str, Any], qq: str) -> str:
    """某个 QQ 的**私聊**会话（UMO）：``{platform}:FriendMessage:{QQ}``。

    与群会话（``…:GroupMessage:群号``）同一套拼法，只有类型段不同——AstrBot 用 UMO
    同时表达「发到哪个会话」。私聊拿来发**只该本人看到**的东西（登录密钥、推流地址、帮助）。
    """
    clean = "".join(ch for ch in str(qq or "") if ch.isdigit())
    if not clean:
        return ""
    platform = str(settings.get("platform") or "aiocqhttp").strip() or "aiocqhttp"
    return f"{platform}:FriendMessage:{clean}"


def remind_leads(settings: dict[str, Any]) -> list[int]:
    """解析赛前提醒的提前量（分钟），从大到小。非法值直接忽略。"""
    raw = str(settings.get("remindLeads") or "")
    out: list[int] = []
    for chunk in raw.replace("，", ",").split(","):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            continue
        value = int(digits)
        if 5 <= value <= 20160 and value not in out:  # 5 分钟 ~ 14 天
            out.append(value)
    return sorted(out, reverse=True)


def _lead_label(minutes: int) -> str:
    """「1 天」/「2 小时」/「30 分钟」——写进提醒标题里。"""
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes >= 60:
        hours = minutes / 60
        return f"{int(hours)} 小时" if hours == int(hours) else f"{hours:.1f} 小时"
    return f"{minutes} 分钟"


def build_remind_message(
    cfg: Config,
    *,
    lead_minutes: int,
    owner_qq: str = "",
    owner_name: str = "",
    settings: dict[str, Any],
    base_url: str = "",
) -> str:
    """赛前提醒：@ 举办者 + 开赛时间 + 一句「该做什么」。

    举办者没登记 QQ 时退化成 ``@名字``（纯文本）——总比什么都不说强，
    同时也能让人意识到「该去把 QQ 填上」。
    """
    name = cfg.event.name or cfg.event.title or "比赛"
    lines: list[str] = []
    mention = at_text([owner_qq], settings) if owner_qq else ""
    if mention:
        lines.append(mention)
    elif owner_name:
        lines.append(f"@{owner_name}")
    lines.append(f"【NTE 比赛】{name} · {_lead_label(lead_minutes)}后开赛")
    start = _fmt_dt(cfg.event.start_time)
    if start:
        delta = _human_delta(cfg.event.start_time)
        lines.append(f"开赛时间：{start}{f'（{delta}）' if delta else ''}")
    if cfg.event.venue:
        lines.append(f"场地：{cfg.event.venue}")
    if cfg.event.organizer:
        lines.append(f"主办：{cfg.event.organizer}")
    lines.append("记得通知参赛选手、确认名单与直播设置。")
    if base_url:
        lines.append(f"赛程与名单：{base_url}")
    return "\n".join(lines)


def build_call_message(
    cfg: Config, state: dict[str, Any], settings: dict[str, Any], members: list[Any] | None = None
) -> str:
    """召集参赛：@ 参与名单里的人 + 比赛名称，请他们到场准备。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    qqs = participant_qqs(cfg, members)
    mention = at_text(qqs, settings)
    start = _fmt_dt(cfg.event.start_time)
    lines = []
    if mention:
        lines.append(mention)
    lines.append(f"【NTE 比赛】{name} 集合啦！")
    if start:
        lines.append(f"时间：{start}（{_human_delta(cfg.event.start_time)}）")
    lines.append(f"赛制：{_format_label(cfg)} · 每方 {cfg.rules.team_size} 人")
    lines.append("请以上选手按时到场、提前调试好设备；未能到场请提前说明。")
    if not qqs:
        lines.append("（提示：参与名单里没有可 @ 的 QQ，先在成员资料里补上 QQ）")
    return "\n".join(lines)


def build_result_message(cfg: Config, state: dict[str, Any]) -> str:
    """比赛结果：冠军 / 排名榜 + 逐场比分。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    rounds = state.get("rounds") or []
    done = [r for r in rounds if r.get("status") == "done"]
    lines = [f"【NTE 比赛】{name} · 结果", f"已赛 {len(done)} / {len(rounds)} 场"]
    champion = state.get("champion")
    if champion:
        lines.append(f"冠军：{champion.get('name') or champion.get('id')}")
    if state.get("format", {}).get("kind") == "league":
        rows = (state.get("standings") or {}).get("players") or []
        qualified = [r for r in rows if r.get("qualified")]
        if qualified:
            lines.append("积分榜：")
            for row in qualified[:8]:
                lines.append(
                    f"{row.get('rank')}. {_player_name(cfg, str(row.get('playerId') or ''))}"
                    f"（{row.get('played', 0)} 场 · 均分 {row.get('average', 0)}"
                    f" · {row.get('points', 0)} 分）"
                )
    else:
        ranking = state.get("ranking") or []
        if ranking:
            lines.append("最终排名：")
            for row in ranking[:8]:
                team = row.get("team") or {}
                lines.append(
                    f"{row.get('seed')}. {team.get('name') or team.get('id') or '—'}"
                    + ("（晋级）" if row.get("advanced") else "")
                )
    if done:
        lines.append("逐场比分：")
        for rnd in done[-12:]:
            lines.append(f"· {_round_line(rnd, scoring=cfg.rules.scoring)}")
        if len(done) > 12:
            lines.append(f"（仅列出最近 12 场，共 {len(done)} 场）")
    return "\n".join(lines)


def build_next_message(cfg: Config, state: dict[str, Any]) -> str:
    """下一场（正在打就报正在打的）：只回答「接下来看哪场」，比进度更聚焦。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    rounds = state.get("rounds") or []
    live = [r for r in rounds if r.get("status") == "live"]
    pending = [r for r in rounds if r.get("status") == "pending"]
    lines = [f"【NTE 比赛】{name} · 下一场"]
    if live:
        lines.append("正在进行：")
        lines.extend(f"· {_round_line(r, scoring=cfg.rules.scoring)}" for r in live[:4])
        if pending:
            lines.append(f"接着：{_round_line(pending[0], scoring=cfg.rules.scoring)}")
    elif pending:
        lines.append(_round_line(pending[0], scoring=cfg.rules.scoring))
        if pending[0].get("scheduledAt"):
            lines.append(
                f"计划：{_fmt_dt(pending[0]['scheduledAt'])}"
                f"（{_human_delta(pending[0]['scheduledAt'])}）"
            )
    elif rounds:
        lines.append("赛程已全部结束")
    else:
        lines.append("赛程还没生成")
    return "\n".join(lines)


def build_roster_message(cfg: Config, state: dict[str, Any]) -> str:
    """参赛名单：本届参与名单（名字 / 编号 / 替补），不含任何凭据。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    players = logic.joined_players(cfg)
    meta = sport_meta(cfg.event.sport)
    noun = meta.get("participants") or "选手"
    lines = [f"【NTE 比赛】{name} · 参赛名单", f"{noun}共 {len(players)} 人"]
    if cfg.teams:
        names = {p.id: p.display_name for p in cfg.players}
        lines.append(f"队伍：{len(cfg.teams)} 支")
        for team in cfg.teams[:12]:
            who = "、".join(names.get(pid, pid) for pid in team.player_ids) or "（空）"
            lines.append(f"· {team.label}：{who}")
        if len(cfg.teams) > 12:
            lines.append(f"（仅列出前 12 支，共 {len(cfg.teams)} 支）")
    else:
        for player in players[:40]:
            tag = f"（{player.tag}）" if player.tag else ""
            lines.append(f"· {player.display_name}{tag}")
        if len(players) > 40:
            lines.append(f"（仅列出前 40 人，共 {len(players)} 人）")
    if not players and not cfg.teams:
        lines.append("（还没定参与名单）")
    return "\n".join(lines)


def build_champion_message(cfg: Config, state: dict[str, Any]) -> str:
    """冠军（锦标赛）或榜首前三（积分制）。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    lines = [f"【NTE 比赛】{name} · 冠军 / 榜首"]
    champion = state.get("champion")
    if champion:
        lines.append(f"冠军：{champion.get('name') or champion.get('id')}")
        return "\n".join(lines)
    if state.get("format", {}).get("kind") == "league":
        rows = [r for r in ((state.get("standings") or {}).get("players") or []) if r.get("qualified")]
        if rows:
            for row in rows[:3]:
                lines.append(
                    f"{row.get('rank')}. {_player_name(cfg, str(row.get('playerId') or ''))}"
                    f"（{row.get('played', 0)} 场 · 均分 {row.get('average', 0)}）"
                )
            return "\n".join(lines)
        lines.append("还没有满场次的人上榜")
        return "\n".join(lines)
    ranking = state.get("ranking") or []
    if ranking:
        lines.append("当前排名：")
        for row in ranking[:3]:
            team = row.get("team") or {}
            lines.append(f"{row.get('seed')}. {team.get('name') or team.get('id') or '—'}")
        return "\n".join(lines)
    lines.append("还没有产生冠军（淘汰赛还没打完）")
    return "\n".join(lines)


def build_events_message(
    events: list[dict[str, Any]], *, page: int = 1, per_page: int = 8
) -> tuple[str, int]:
    """全部比赛列表（分页）。返回 ``(文本, 总页数)``。"""
    total = len(events)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(pages, int(page or 1)))
    chunk = events[(page - 1) * per_page : page * per_page]
    lines = [f"【NTE 比赛】全部比赛共 {total} 届（第 {page} / {pages} 页）"]
    if not chunk:
        lines.append("（暂无赛事）")
    for pos, item in enumerate(chunk, start=(page - 1) * per_page + 1):
        head = f"{pos}. {item.get('name') or item.get('id')}"
        bits = [EVENT_STATUS.get(item.get("status"), "")]
        bits.append("娱乐模式" if item.get("ranked") is False else "排名制")
        bits.append(f"{item.get('players') or 0} 人")
        bits.append(f"已赛 {item.get('played') or 0}/{item.get('rounds') or 0}")
        if item.get("hidden"):
            bits.append("已隐藏")
        if item.get("champion"):
            bits.append(f"榜首 {item['champion']}")
        line = f"{head} · " + " · ".join(b for b in bits if b)
        if item.get("brief"):
            line += f"\n    {item['brief']}"
        lines.append(line)
    if pages > 1:
        # 这里必须给**真实存在**的写法：以前写「发送「下一页」」，但「下一页」既不是
        # 命令也不是别名，用户照着发只会石沉大海。
        lines.append(
            f"（发「比赛列表 {min(page + 1, pages)}」看下一页；共 {pages} 页）"
            if page < pages
            else f"（已是最后一页，共 {pages} 页）"
        )
    return "\n".join(lines), pages


def build_ids_message(
    events: list[dict[str, Any]], *, page: int = 1, per_page: int = 10, scope: str = "all"
) -> tuple[str, int]:
    """届次编号列表（**填参数用**；分页）。返回 ``(文本, 总页数)``。

    与 :func:`build_events_message`（比赛列表）的分工：那个说的是「有哪些比赛、各自什么
    情况」，这个只给**能写进命令里的编号**——行更短，所以每页塞得更多。

    隐藏届**不列**：机器人这边按 ``/api/bot/events``（同样过滤隐藏项）解析届次，
    列出来只会让人照着发一句「没找到这一届」。

    ``scope == "mine"`` 时标题改成「你创建的届」——调用方（``/api/bot/query``）已经
    按 QQ 过滤好了列表，这里只负责措辞与翻页提示里的命令该带不带「我的」。
    """
    rows = [item for item in events if not item.get("hidden")]
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(pages, int(page or 1)))
    chunk = rows[(page - 1) * per_page : page * per_page]
    scope_args = "我的 " if scope == "mine" else ""
    lines = [
        (
            f"【NTE 比赛】{'你创建的届' if scope == 'mine' else '届次列表'}："
            f"共 {total} 届（第 {page} / {pages} 页）"
        )
    ]
    if not chunk:
        lines.append(
            "（你还没有创建过届次：在站点新建一届后，这里就能看到它）"
            if scope == "mine"
            else "（暂无赛事）"
        )
    for pos, item in enumerate(chunk, start=(page - 1) * per_page + 1):
        state = EVENT_STATUS.get(item.get("status"), "")
        head = f"{pos}. {item.get('id')} {item.get('name') or item.get('id')}"
        lines.append(f"{head} · {state}" if state else head)
    if pages > 1:
        # 翻页提示必须给**真实可用**的写法：以前写「下一页」，而它既不是命令也不是别名
        lines.append(
            f"（发「比赛届次 {scope_args}{min(page + 1, pages)}」看下一页；共 {pages} 页）"
            if page < pages
            else f"（已是最后一页，共 {pages} 页）"
        )
    return "\n".join(lines), pages


def build_detail_message(cfg: Config, state: dict[str, Any], ref: str = "") -> str:
    """某一届的信息 + 进度 + 结果；给了 ``ref`` 就详细说那一场（对局）。"""
    name = cfg.event.name or cfg.event.title or "比赛"
    if ref:
        target = next(
            (r for r in (state.get("rounds") or []) if r.get("code") == ref or r.get("id") == ref),
            None,
        )
        if target is None:
            return f"【NTE 比赛】{name}：没有找到「{ref}」这一场，可用编号见赛程页。"
        sides = target.get("sides") or []
        lines = [f"【NTE 比赛】{name} · {target.get('label') or ref}"]
        if target.get("stageName"):
            lines.append(f"阶段：{target['stageName']}")
        lines.append(f"状态：{ROUND_STATUS.get(str(target.get('status')), target.get('status') or '')}")
        if target.get("scheduledAt"):
            lines.append(f"计划时间：{_fmt_dt(target['scheduledAt'])}")
        if target.get("startedAt"):
            lines.append(f"开始时间：{_fmt_dt(target['startedAt'])}")
        sc = cfg.rules.scoring
        counted = bool(target.get("sets"))
        for side in sides:
            who = _side_text(side)
            names = _join_names(side.get("players") or [])
            suffix = f"（{names}）" if names and names != who else ""
            text = sc.format_score(side.get("score", 0), counted=counted)
            label = "大比分" if counted else sc.label_text
            lines.append(f"· {who}：{label} {text}{suffix}")
        rounds = target.get("sets") or []
        if rounds:
            detail = " / ".join(
                f"{sc.format(item.get('a', 0))}:{sc.format(item.get('b', 0))}" for item in rounds
            )
            lines.append(f"各轮{sc.label_text}：{detail}")
        if target.get("winner") == "DRAW":
            lines.append("结果：平局")
        elif target.get("winner"):
            winner = next((s for s in sides if s.get("winner")), None)
            lines.append(f"胜方：{_side_text(winner) if winner else target['winner']}")
        return "\n".join(lines)
    # 不带 ref：给这一届的「信息 + 进度 + 结果」摘要
    return "\n".join(
        [
            build_event_message(cfg, state),
            "",
            build_progress_message(cfg, state),
        ]
    )


# 直播各路的称呼（与 ``live.LIVE_KIND_LABEL`` 一致，这里只用于排版文案）
LIVE_KIND_LABEL = {
    "main": "主直播间",
    "player": "选手机位",
    "member": "成员直播间",
    "channel": "成员频道",
    "bili": "B站直播",
}
# 单条消息里最多列几路（跟其它列表消息一样，超了就说明一句）
LIVE_MAX_ITEMS = 12


def build_live_message(live_info: dict[str, Any] | None) -> str:
    """当前直播：主直播间开关情况 + 正在推流的各路（含观看地址）。

    数据来自 ``live.collect_live()``（**已经真探过一次**）。直播是**全局的**
    ——成员直播间不属于任何一届——所以这条消息不挑届次。

    查不到状态（媒体服务器没配 / 不可达）时**直说查不到**，绝不假装「没人在播」：
    那两件事对用户的意义完全不同（一个是「现在没人开播」，一个是「我查不了」）。
    """
    info = live_info or {}
    lines = ["【NTE 比赛】当前直播"]
    items = info.get("items") or []
    # 媒体服务器查不到**而且**没有任何一路可看时才直说「查不到」：
    # B站 直播是另一条链路，媒体服务器没配也不影响它
    if not info.get("known") and not items:
        lines.append("暂时查不到直播状态。")
        reason = str(info.get("reason") or "").strip()
        if reason:
            lines.append(f"原因：{reason}")
        lines.append("（在站点「直播」页打开一次可触发检测；未配置媒体服务器时忽略本条）")
        return "\n".join(lines)

    main = info.get("main") or {}
    main_play = main.get("play") or {}
    if not info.get("known"):
        reason = str(info.get("reason") or "媒体服务器不可达").strip()
        lines.append(f"媒体服务器状态查不到（{reason}）：下面只有 B站 直播。")
    elif main.get("live"):
        lines.append("主直播间：直播中")
        if main_play.get("webrtc"):
            lines.append(f"    WebRTC：{main_play['webrtc']}")
    else:
        lines.append("主直播间：未开播")

    if items:
        lines.append(f"正在直播 {len(items)} 路：")
        for item in items[:LIVE_MAX_ITEMS]:
            bili = item.get("bili") or {}
            if bili.get("jump"):
                # B站 这一路没有本站地址：给观众一个能直接点开的直播间链接。
                # 标题与在线人数都是从 B站 现取的（开播后自动同步），一并写出来。
                bits = ["B站直播"]
                if bili.get("title"):
                    bits.append(f"《{bili['title']}》")
                if int(bili.get("online") or 0) > 0:
                    bits.append(f"{bili['online']} 人在看")
                lines.append(f"· {item.get('name')}（{' · '.join(bits)}）")
                lines.append(f"    B站：{bili['jump']}")
                continue
            tag = LIVE_KIND_LABEL.get(str(item.get("kind")), "")
            note = str(item.get("note") or "").strip()
            suffix = " · ".join(part for part in (tag, note) if part)
            lines.append(f"· {item.get('name')}" + (f"（{suffix}）" if suffix else ""))
            play = item.get("play") or {}
            if play.get("webrtc"):
                lines.append(f"    WebRTC：{play['webrtc']}")
            if play.get("hls"):
                lines.append(f"    HLS：{play['hls']}")
        if len(items) > LIVE_MAX_ITEMS:
            lines.append(f"（仅列出前 {LIVE_MAX_ITEMS} 路，共 {len(items)} 路）")
    else:
        lines.append("现在没有选手 / 成员在推流。")
    if not info.get("enabled"):
        lines.append("（提示：直播功能在「直播配置」里是关闭的）")
    lines.append("观看提示：WebRTC 延迟最低；HLS（.m3u8）更稳。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
# 推送 / 查询类型：``key → (名称, 说明)``。既是推送面板的菜单，也是
# 只读查询 API（``/api/bot/manifest``）与 AstrBot 插件「能做什么」的唯一来源。
KIND_META: dict[str, tuple[str, str]] = {
    "event": ("比赛信息", "名字 / 赛制 / 时间 / 人数 / 简介 / 是否启用排名"),
    "live": ("当前直播", "主直播间是否开播 + 正在推流的选手 / 成员机位与观看地址"),
    "progress": ("赛程进度", "已赛多少、正在打谁 vs 谁；没在打就报下一场"),
    "call": ("召集参赛", "列出参与名单与 @ 片段，请他们到场准备"),
    "result": ("比赛结果", "冠军 / 榜单 + 逐场比分"),
    "list": ("比赛列表", "全部届次（分页；过长自动分段）"),
    "ids": ("届次列表", "全部届次的编号与名称（分页；写「我的」只看自己创建的）"),
    "detail": ("单届详情", "信息 + 进度 + 结果；带 ref 时细说某一场"),
    "next": ("下一场", "正在打的场次；没有就报下一场与计划时间"),
    "roster": ("参赛名单", "本届参与名单（名字 / 编号 / 替补 / 队伍）"),
    "champion": ("冠军与榜首", "锦标赛的冠军，或积分制的榜首前三"),
}
KINDS = tuple(KIND_META)


def dispatch(
    kind: str,
    *,
    settings: dict[str, Any],
    cfg: Config | None = None,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    ref: str = "",
    page: int = 1,
    members: list[Any] | None = None,
    live_info: dict[str, Any] | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """构建要发的消息（不发送）。返回 ``{parts, pages, page}``。

    ``kind == "live"``（当前直播）需要调用方先 ``await live.collect_live()`` 把
    数据取好传进来——本模块只负责排版，不做网络探测（它跑在 ``to_thread`` 里）。

    ``scope`` 只给 ``ids`` 用：调用方若已按 QQ 过滤成「他自己创建的届」，这里就按
    「你创建的届」措辞并把翻页提示写成带「我的」的命令。
    """
    limit = int(settings.get("maxChars") or 1200)
    pages = 1
    if kind == "list":
        text, pages = build_events_message(
            events or [], page=page, per_page=max(3, min(20, limit // 90))
        )
    elif kind == "ids":
        text, pages = build_ids_message(events or [], page=page, scope=scope)
    elif kind == "event":
        text = build_event_message(cfg, state or {})
    elif kind == "progress":
        text = build_progress_message(cfg, state or {})
    elif kind == "call":
        text = build_call_message(cfg, state or {}, settings, members)
    elif kind == "result":
        text = build_result_message(cfg, state or {})
    elif kind == "detail":
        text = build_detail_message(cfg, state or {}, ref)
    elif kind == "next":
        text = build_next_message(cfg, state or {})
    elif kind == "roster":
        text = build_roster_message(cfg, state or {})
    elif kind == "champion":
        text = build_champion_message(cfg, state or {})
    elif kind == "live":
        text = build_live_message(live_info)
    else:
        raise ValueError(f"未知的推送类型：{kind}")
    # 先纯文本化再分段：否则「按 Markdown 长度切的段」和「纯文本化后的实际长度」对不上
    plain = to_plain_text(text)
    return {"parts": split_message(plain, limit), "pages": pages, "page": max(1, int(page or 1))}
