"""给 QQ 机器人（AstrBot）插件用的**只读查询 API**。

配好令牌后，AstrBot 里那个配套插件（``integrations/astrbot_plugin_nte_match``）
就能把「比赛列表 / 进度 / 结果 / 详情」直接当成**群命令**用。插件侧**不用大模型**：
它只做字符串匹配 + 调这些只读接口，所以这里的返回一律是**可以直接发到群里**的纯文本
（分段切好、不含 Markdown），其它接入方（脚本 / 自建机器人）也能照这个约定用。

| 接口 | 说明 |
| --- | --- |
| `GET /api/bot/ping` | 探活：确认地址与令牌对不对 |
| `GET /api/bot/manifest` | 能力清单（有哪些查询、各自能问什么） |
| `GET /api/bot/events` | 届次的结构化列表（比纯文本更好解析，供脚本 / 其它接入方用） |
| `GET /api/bot/participants` | 本届参与者的 QQ（插件用它发真正的 @） |
| `GET /api/bot/managers` | **有资格召集**的人的 QQ（**该届创建者** + 服务器管理员）；带 `qq` 时另回他自己创建过哪些届 |
| `GET /api/bot/query` | 直接拿到**可以原样发到群里**的纯文本（分段已切好） |
| `GET /api/bot/whoami` | 按 QQ 认人：这个人在站内是什么身份、有没有权限 |
| `POST /api/bot/members` | 群里授权 / 添加成员（**仅服务器管理员**；新建成员时**站点直接把密钥私聊给本人**） |
| `GET /api/bot/my-links` | 本人的推流地址 + **站内**直播间地址 |
| `POST /api/bot/credential` | 凭据重置：重置登录密钥 / 重置直播令牌 / 改推流码（默认改自己；服务器管理员可代改，**新值只私聊给被改的那个人**） |
| `POST /api/bot/stream-setup` | **比赛直播注册**：缺什么补什么（没推流码就给一个、没令牌就发一把），结果只私聊给本人 |
| `GET /api/bot/uid` | 查游戏 UUID（自己或 @ 到的人）：**群内可见**，不需要权限 |
| `GET /api/bot/profile` | 资料全文：QQ / 名字 / 游戏 UUID / B站 房间号 / 推流码 / 推流地址 / 密钥与令牌**有没有**（都不是明文） |
| `POST /api/bot/profile` | 改资料（名字 / 游戏 UUID / B站 房间号 / 推流码 / 直播间标题 / QQ），回一份新资料 |
| `POST /api/bot/notify` | 把一条消息**私聊**发给某人（帮助说明 / 推流地址 / 资料） |

前面几个 ``GET`` 是只读查询；``POST /members``、``POST /credential`` 与 ``POST /notify`` 会
**写库或发消息**：认人一律靠插件上报的 QQ（取自平台事件，不是用户手输），权限判定在站点这一侧；
密钥只走私聊、不授予 ``server_admin``。

**凭据永远不由接口回话**：轮换出来的密钥 / 令牌只在**站点发出的那条私聊**里出现，
响应体里只有「发了没发出去」——插件拿不到明文，也就打不进群里。

``/api/bot/query`` 的 ``kind`` 清单见 ``/api/bot/manifest``。其中 ``live``（当前直播）
是**全局**信息（不挑届次），并且会**真的探测一次**媒体服务器（最多等 3 秒）——
其它 kind 都只读内存 / 数据库。

鉴权：``Authorization: Bearer nte_xxx``（也支持 ``X-NTE-Token``）。**不支持 ``?token=``**——
查询串会进反向代理 / CDN 的访问日志，而这个令牌不会过期，理由见 ``require_bot_token``。
令牌在「服务器 → QQ 机器人」里生成，**服务端只存加盐哈希**，明文只在生成那一次显示。

全是只读接口，不会碰机器人、也不占推送额度。
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from . import card, live, logic, qqbot
from .auth import verify_secret
from .logging_conf import get_logger
from .members import ensure_stream_unique
from .models import Member, NTEModel
from .store import store

log = get_logger("botapi")

router = APIRouter(prefix="/api/bot", tags=["bot"])

#: 群里能授予的权限。**故意不含 server_admin**：全站只有一位服务器管理员，
#: 而且是最高权限，不该能靠一句群命令授予（真要换人请到「服务器 → 成员管理」）。
BOT_GRANTABLE = {
    "member": "普通成员",
    "event_admin": "赛事管理员",
}


class BotGrantPayload(NTEModel):
    """群里授权 / 添加成员：谁在操作、给谁、什么权限。"""

    actor_qq: str = ""
    target_qq: str = ""
    name: str = ""
    permission: str = "member"


class BotNotifyPayload(NTEModel):
    """私聊投递：给某个 QQ 发一条文本（帮助文档 / 推流地址）。"""

    qq: str = ""
    text: str = ""


class BotCredentialPayload(NTEModel):
    """凭据重置：默认改**自己**，服务器管理员可以替别人改。

    ``what`` 三选一：

    * ``key``    —— 重置登录密钥（旧密钥立刻失效）；
    * ``token``  —— 重置直播令牌（Bearer；要求已有推流码）；
    * ``streamId`` —— 改推流码，新值放在 ``value`` 里。

    ``target_qq`` 留空 = 改自己（谁都能改自己的）；填了别人 = 只有服务器管理员能这么做。
    **无论改谁，新值都只私聊给被改的那个人**——服务器管理员也看不到。
    """

    qq: str = ""
    target_qq: str = ""
    what: str = ""
    value: str = ""


#: ``what`` 的写法容错：插件与脚本都可能写成别的形式
_CREDENTIAL_ALIAS = {
    "key": "key",
    "password": "key",
    "secret": "key",
    "登录密钥": "key",
    "密钥": "key",
    "token": "token",
    "bearer": "token",
    "streamtoken": "token",
    "直播令牌": "token",
    "令牌": "token",
    "streamid": "streamId",
    "stream_id": "streamId",
    "streamkey": "streamId",
    "推流码": "streamId",
    "推流id": "streamId",
}


def _clean_qq(raw: Any) -> str:
    """只留数字：@ 出来的 QQ 有时带空格、CQ 码残留或前缀，非数字一律当没给。"""
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _site_base(request: Request) -> str:
    """本站的绝对地址（不含尾斜杠）。与 og:url 用同一套推导，反代下也一致。"""
    return str(request.base_url).rstrip("/")


def _actor_member(qq: str) -> Member:
    """把插件上报的 QQ 换成站内成员；查不到 / 被停用就 403。

    这是**权限闸门真正所在的位置**：插件那份判断只是为了早点给出人话提示，绕过插件
    直接调接口也过不了这一关——因为「谁在操作」由 QQ 决定，而令牌只代表「这是本站的
    机器人」。
    """
    clean = _clean_qq(qq)
    member = store.member_by_qq(clean)
    if member is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "没对上你的成员资料：请先在站点「我的」页把自己的 QQ 登记上"
                "（服务器管理员也一样），再让机器人认人。"
            ),
        )
    if not member.active:
        raise HTTPException(status_code=403, detail="你的成员账号已被停用，无法操作")
    return member


async def _send_private(settings: dict[str, Any], qq: str, text: str) -> dict[str, Any]:
    """让站点把一条消息**私聊**发给某个 QQ（群里推送走的是同一条通道）。

    凭据就靠这一层做到「只走私聊」：接口只回「发了没有」，明文连调用方都拿不到。
    """
    clean = _clean_qq(qq)
    if not clean:
        return {"ok": False, "detail": "没有目标 QQ"}
    umo = qqbot.private_umo(settings, clean)
    result = await qqbot.send_text(text, settings=settings, umo=umo)
    if not result.get("ok"):
        log.warning("私聊投递失败 | qq=%s | %s", clean, result.get("detail"))
    return result


def _secret_notice(kind: str, site: str) -> str:
    """拿到新凭据后必须跟着的那段提醒。

    密钥与令牌都是「谁拿到谁就能用」：密钥能登录、令牌能推流。所以除了让他保存好，
    还要说清两件事——这条消息本身也别外传；**觉得可能泄露就立刻再重置一次**
    （旧值随即作废，这正是自助重置存在的意义）。
    """
    if kind == "key":
        return (
            "注意：任何拿到这把密钥的人都能用它登录你的账号（改资料，有权限的话还能管赛事）。\n"
            "别把这条消息转给别人；如果它可能被别人看到，马上再发一次「比赛重置密钥」"
            f"（旧密钥立即失效），或到 {site}/user 再轮换一次。"
        )
    return (
        "注意：任何拿到这串令牌的人都能用你的推流码往你直播间推流（把你的画面顶掉）。\n"
        "别把这条消息转给别人；如果它可能被别人看到，马上再发一次「比赛重置令牌」"
        "（旧令牌立即失效）。"
    )


def _target_for(actor: Member, raw_qq: str, *, action: str) -> tuple[Member, bool]:
    """这次操作**谁**；返回 ``(成员, 是不是在替别人操作)``。

    不填 / 填自己 = 自助。填了别人的 QQ 必须是**服务器管理员**：替别人改凭据 / 看资料
    都是管理动作，不是「谁都能顺手把别人的登录口令作废、把资料看一遍」。

    ``action`` 只用来拼提示语（「替别人重置凭据」/「替别人查看资料」…），
    其它逻辑完全一致——凭据与资料走的是同一条认人闸门。
    """
    target_qq = _clean_qq(raw_qq)
    if not target_qq or target_qq == _clean_qq(actor.qq):
        return actor, False
    if actor.permission != "server_admin":
        raise HTTPException(status_code=403, detail=f"只有服务器管理员能{action}")
    target = store.member_by_qq(target_qq)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"这个 QQ 还不是成员：先用「比赛添加 @某人」把 TA 加进来，再{action}。",
        )
    if not target.active:
        raise HTTPException(status_code=403, detail=f"TA 的成员账号已被停用，先启用再{action}")
    return target, True


def _credential_target(actor: Member, raw_qq: str) -> tuple[Member, bool]:
    """凭据路径的入口：这次改**谁的**凭据（规则见 :func:`_target_for`）。

    新值始终只发给**被改的那个人**（见 ``api_bot_credential`` 末尾），
    所以服务器管理员代改也拿不到密钥 / 令牌——他要的只是「帮不上线的人重置」。
    """
    return _target_for(actor, raw_qq, action="重置凭据")


def _bearer(header: str | None) -> str:
    """从 ``Authorization: Bearer xxx`` 里取令牌。"""
    raw = (header or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw


async def require_bot_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_nte_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """校验插件令牌；返回当前的 QQ 机器人设置（含只读查询所需的一切）。"""
    settings = store.qqbot_settings()
    stored = str(settings.get("botApiTokenHash") or "")
    if not stored:
        raise HTTPException(
            status_code=403,
            detail="查询 API 未启用：请先在「服务器 → QQ 机器人」里生成接口令牌",
        )
    # 只认请求头。刻意**不收** ``?token=``：查询串会被反向代理 / CDN 的访问日志原样
    # 记下来，而这个令牌**不会过期**（只能手动重置），漏一次就等于长期只读权限
    # （含参与名单里的 QQ）。配套插件发的本来就是 ``Authorization`` 头，没有调用方依赖它。
    token = _bearer(authorization) or (x_nte_token or "").strip()
    if not token or not verify_secret(token, stored):
        log.warning("查询 API 令牌校验失败 | ip=%s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="令牌不正确")
    return settings


@router.get("/ping")
async def api_bot_ping(
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """探活：插件启动时调一次，确认地址 / 令牌都通。"""
    cfg = store.snapshot()
    events = await store.list_events()
    return {
        "ok": True,
        "service": "nte-match",
        "currentEvent": {"id": store.current_id, "name": cfg.event.name or cfg.event.title},
        "eventCount": len(events),
        "pushEnabled": bool(settings.get("enabled")),
    }


@router.get("/manifest")
async def api_bot_manifest(
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """能力清单：插件里的命令与这里一一对应，避免两边各写一份说明。"""
    return {
        "ok": True,
        "kinds": [
            {"key": key, "label": label, "hint": hint}
            for key, (label, hint) in qqbot.KIND_META.items()
        ],
        "params": {
            "kind": "查询类型（见 kinds）",
            "eventId": "届次 id（如 e001）；仅 live 不用填。留空 = 服务器记的那届（谁最近打开过就是谁，"
            "属于实现细节：插件侧一律要求写明，站点推送才用它当默认）",
            "ref": "场次编号（如 L-1 / 八强赛-1）；仅 detail 用",
            "page": "页码，从 1 开始；仅 list 用",
            "at": "是否带 @ 片段（召集类）；插件自己发 At 组件时传 0",
        },
        # 与插件里的命令一一对应（插件是执行方，这里是给调试/其它接入方看的清单）。
        # 别名刻意收得比较宽：**能命中命令**是唯一依靠——插件不用大模型，没有兜底（见 README）。
        "commands": [
            {"command": "比赛", "kind": "detail", "args": "—", "alias": ["当前比赛", "赛事", "ntematch"]},
            {
                "command": "比赛直播",
                "kind": "live",
                "args": "—",
                "alias": ["直播", "当前直播", "谁在播", "谁在直播", "直播间", "开播了吗", "ntelive"],
                "note": "全局信息（不挑届次）：主直播间 + 正在推流的选手 / 成员机位",
            },
            {"command": "比赛列表", "kind": "list", "args": "页码（可选）", "alias": ["全部比赛", "历届", "往届", "比赛目录", "有哪些比赛"]},
            {
                "command": "比赛届次",
                "kind": "ids",
                "args": "「我的」/ 页码（都可选）",
                "alias": ["届次列表", "比赛编号"],
                "note": (
                    "编号与名称（分页）。写「我的」只看自己创建的；一页装不下时插件会私聊发本人"
                    "（不是群发）"
                ),
            },
            {"command": "比赛信息", "kind": "event", "args": "届次（必填）", "alias": ["赛事信息", "比赛时间", "nteginfo"]},
            {"command": "比赛进度", "kind": "progress", "args": "届次（必填）", "alias": ["进度", "赛程", "赛程进度", "现在打谁", "打到哪了", "谁领先", "什么情况"]},
            {"command": "比赛下一场", "kind": "next", "args": "届次（必填）", "alias": ["下一场", "下场", "接下来", "现在打", "接着打谁", "等下打谁"]},
            {"command": "比赛结果", "kind": "result", "args": "届次（必填）", "alias": ["结果", "成绩", "比分", "赢了吗", "什么比分", "结果咋样"]},
            {"command": "比赛冠军", "kind": "champion", "args": "届次（必填）", "alias": ["冠军", "榜首", "谁赢了"]},
            {"command": "比赛名单", "kind": "roster", "args": "届次（必填）", "alias": ["参赛名单", "选手名单", "比赛选手", "队伍", "都有谁"]},
            {"command": "比赛详情", "kind": "detail", "args": "届次（必填） + 场次（可选）", "alias": ["赛事详情", "场次详情", "单场"]},
            {
                "command": "比赛召集",
                "kind": "call",
                "args": "届次（必填）",
                "alias": ["召集参赛", "集合", "喊人"],
                "note": "@ 由插件用 At 组件发；只有本届创建者（举办者）/ 服务器管理员能召集（名单见 /api/bot/managers）",
            },
            {
                "command": "比赛我的",
                "kind": "—",
                "args": "—",
                "alias": ["我的推流", "我的直播间", "推流地址", "我的地址"],
                "note": "用 /api/bot/my-links：按 QQ 认人，私聊回本人的推流地址 + 站内直播间地址",
            },
            {
                "command": "比赛直播注册",
                "kind": "—",
                "args": "「流名」/@某人（都可选）",
                "alias": ["直播注册", "开播注册", "我的推流码", "注册直播"],
                "note": (
                    "用 POST /api/bot/stream-setup：缺什么补什么（没推流码就给一个、没令牌就发一把），"
                    "已有的一律不动；详情只私聊给本人，服务器管理员可 @ 代办"
                ),
            },
            {
                "command": "比赛UID",
                "kind": "—",
                "args": "@某人（可选）",
                "alias": ["游戏UID", "游戏uuid", "我的UID", "异环UID", "uid"],
                "note": "用 GET /api/bot/uid：游戏 UUID，群里直接回，不需要权限",
            },
            {
                "command": "比赛UUID",
                "kind": "uuids",
                "args": "届次（必填）",
                "alias": ["选手UUID", "UUID列表", "全部UUID", "nteuuid"],
                "note": "本届参赛选手的 UUID 清单：每行「名字 UUID」，可整段复制（kind=uuids）",
            },
            {
                "command": "比赛资料",
                "kind": "—",
                "args": "「字段 新值」/@某人（都可选）",
                "alias": ["我的资料", "改资料", "个人资料"],
                "note": (
                    "用 GET/POST /api/bot/profile：不填字段 = 私聊发完整资料（含怎么改）；"
                    "填了 = 改那一项（名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ）"
                ),
            },
            {
                "command": "比赛授权",
                "kind": "—",
                "args": "@某人",
                "alias": ["授权赛事管理员", "授予赛事管理员", "设为赛事管理员"],
                "note": "用 POST /api/bot/members：仅服务器管理员；不是成员则自动建号，密钥私聊给本人",
            },
            {
                "command": "比赛添加",
                "kind": "—",
                "args": "@某人",
                "alias": ["添加成员", "添加群友", "比赛添加成员", "设为成员"],
                "note": "同上，权限为普通成员",
            },
            {
                "command": "比赛帮助",
                "kind": "—",
                "args": "—",
                "alias": ["赛事帮助", "比赛命令", "比赛功能", "ntehelp", "比赛help"],
                "note": "帮助文档私聊发给提问者（POST /api/bot/notify）",
            },
        ],
    }


@router.get("/events")
async def api_bot_events(
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """届次的结构化列表（已过滤隐藏项）。"""
    events = await store.list_events()
    return {
        "ok": True,
        "current": store.current_id,
        "events": [
            {
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "status": item.get("status"),
                "ranked": item.get("ranked") is not False,
                "sport": item.get("sport") or "",
                "brief": item.get("brief") or "",
                "players": item.get("players") or 0,
                "played": item.get("played") or 0,
                "rounds": item.get("rounds") or 0,
                "startTime": item.get("startTime") or "",
                "endTime": item.get("endTime") or "",
                "champion": item.get("champion") or "",
                "current": bool(item.get("current")),
            }
            for item in events
            if not item.get("hidden")
        ],
    }


@router.get("/participants")
async def api_bot_participants(
    event_id: str = Query(default="", alias="eventId"),
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """参与名单里能 @ 到的 QQ（插件据此用 ``At`` 组件发**真正的 @**）。

    走 HTTP 发消息时 @ 只能写进文本（AstrBot 的 OpenAPI 没有 at 段），但**插件跑在
    AstrBot 里面**，可以直接用消息组件——所以这条路才能真正 @ 到人。
    """
    target = (event_id or "").strip() or store.current_id
    try:
        cfg = store.snapshot() if target == store.current_id else await store.read_event(target)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    qqs = qqbot.participant_qqs(cfg, store.members())
    return {
        "ok": True,
        "eventId": target,
        "eventName": cfg.event.name or cfg.event.title,
        "qqs": qqs,
        "count": len(qqs),
        "atMode": settings.get("atMode"),
    }


def _events_of_qq(events: list[dict[str, Any]], qq: str) -> list[dict[str, Any]]:
    """从届次列表里挑出这个 QQ **自己创建**的那些。

    认人靠 QQ：``ownerUid`` → 成员资料 → ``qq``（群里只能靠 QQ 认人，这是唯一的对号方式）。
    与 ``/managers`` 的 ``mine`` 同一口径——那边的「你能召集哪些届」就是这里的子集。

    过滤放在**站点**这一侧：插件不该自己去猜谁是创建者（它拿不到 uid 与成员表）。
    """
    if not qq:
        return []
    by_uid = {m.uid: m for m in store.members() if m.active}
    out: list[dict[str, Any]] = []
    for item in events:
        owner = by_uid.get(str(item.get("ownerUid") or ""))
        if owner and str(owner.qq or "") == qq:
            out.append(item)
    return out


async def _callable_events(qq: str) -> list[dict[str, str]]:
    """这个 QQ **自己创建**的届——也就是他能召集的那些。

    为什么要它：一个赛事管理员在群里被拒时，光说「你不是本届创建者」他无从下手——
    他可能只是**写错了届次**。有了这份清单，插件就能直接告诉他
    「你自己创建的届是「小队长杯」（e005），用『比赛召集 e005』」。

    口径与 ``/api/bot/events`` 一致：隐藏届不算（那种届机器人也解析不到），
    回的是 ``{id, name}``，不含 uid。
    """
    visible = [item for item in await store.list_events() if not item.get("hidden")]
    return [
        {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
        for item in _events_of_qq(visible, qq)
    ]


@router.get("/managers")
async def api_bot_managers(
    event_id: str = Query(default="", alias="eventId"),
    qq: str = Query(default="", description="提问者的 QQ；填了就顺便回他自己能召集哪些届"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """本届**有资格召集**的人的 QQ（**该届创建者** + 服务器管理员）。

    规则只有一条：**谁创建的届，谁可以召集**。服务器管理员另有一层全局权限
    （他创建的届自然归他，别人建的届他也能召集）。所以这里**不看「赛事管理员」这个
    身份**：一位赛事管理员能召集的是*他自己创建的那些届*，不是随便是哪一届。

    为什么要这个接口：召集会 @ 全场参赛者，属于「会打扰很多人」的动作；而**在群里
    只能靠 QQ 认人**——谁是这一届的创建者，只有站点这边知道。插件拿这个名单比对
    发命令的人，不在名单里就拒绝。

    另外两个字段是给**被拒的人**看的（插件据此把原因说清楚，见插件 ``_call_denied_text``）：

    * ``owner``：本届创建者叫什么、有没有登记 QQ——「名单为空」时这句话就是全部解释；
    * ``mine``：带 ``qq`` 参数时，回这个 QQ 自己创建的届（他可照着一句
      「比赛召集 <届次>」召集自己那届）。

    只回 QQ 列表与创建者的名字，不回 uid / 权限；名单为空时 ``note`` 里说明原因
    （最常见：创建者还没在「我的」页填 QQ）。
    """
    target = (event_id or "").strip() or store.current_id
    try:
        cfg = store.snapshot() if target == store.current_id else await store.read_event(target)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    members = {m.uid: m for m in store.members()}
    owner_uid = str(cfg.event.owner_uid or "")
    owner = members.get(owner_uid) if owner_uid else None
    qqs: list[str] = []
    for member in members.values():
        if not member.active or not member.qq:
            continue
        if member.permission == "server_admin" or (owner_uid and member.uid == owner_uid):
            qqs.append(str(member.qq))
    unique = list(dict.fromkeys(qqs))
    if unique:
        note = "召集权限：本届创建者 + 服务器管理员（按成员资料里的 QQ 认人）"
    elif owner is not None and not owner.qq:
        note = f"本届由「{owner.name}」创建，但他还没在站点登记 QQ，现在没有能召集的人"
    else:
        note = "本届没有登记 QQ 的创建者或服务器管理员，无法判断谁有资格召集"
    payload: dict[str, Any] = {
        "ok": True,
        "eventId": target,
        "eventName": cfg.event.name or cfg.event.title,
        "qqs": unique,
        "count": len(unique),
        "owner": {
            # 创建者的名字（站点成员列表里本来就公开）与「他登记 QQ 了没」。
            # 「名单为空」时，这两项就是唯一说得清的原因（见 note 与插件提示）。
            "name": (owner.name if owner else ""),
            "hasQq": bool(owner and owner.qq),
        },
        "note": note,
    }
    asker = (qq or "").strip()
    if asker:
        payload["mine"] = await _callable_events(asker)
    return payload


@router.get("/query")
async def api_bot_query(
    request: Request,
    kind: str = Query(default="event"),
    event_id: str = Query(default="", alias="eventId"),
    ref: str = Query(default=""),
    page: int = Query(default=1),
    at: bool = Query(default=True, description="召集类是否带 @ 片段（插件自己发 At 时传 0）"),
    scope: str = Query(default="all", description="ids 用：all = 全部；mine = 只看这个 QQ 创建的"),
    qq: str = Query(default="", description="发命令那个人的 QQ（scope=mine 时用）"),
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """按类型组装**可直接发到群里**的纯文本。

    ``parts`` 是已经按长度切好的段（QQ 单条有上限），插件依次发出去即可；
    ``text`` 是拼起来的全文（不分段，给脚本 / 存档用）。文本已做**纯文本化**，不含 Markdown。
    """
    if not at:
        settings = {**settings, "atMode": "none"}
    key = (kind or "event").strip().lower()
    if key not in qqbot.KINDS:
        raise HTTPException(status_code=400, detail=f"kind 只能是 {' / '.join(qqbot.KINDS)}")
    scope_key = (scope or "all").strip().lower()
    if scope_key not in ("all", "mine"):
        raise HTTPException(status_code=400, detail="scope 只能是 all（全部）或 mine（自己创建的）")
    if scope_key == "mine" and not _clean_qq(qq):
        # 「只看自己创建的」不认人就没法做：宁可报清楚，也别悄悄回个空列表
        raise HTTPException(status_code=400, detail="scope=mine 要带上 qq（发命令那个人的 QQ）")
    target = (event_id or "").strip() or store.current_id
    cfg = None
    state: dict[str, Any] = {}
    # list / ids / live 都是**全局**信息，不需要挑届次（live 是成员机位 / 主直播间的事）
    if key not in ("list", "ids", "live"):
        try:
            if target == store.current_id:
                cfg = store.snapshot()
                state = logic.build_state(cfg)
            else:
                cfg = await store.read_event(target)
                state = logic.build_state(cfg, historical=True)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    # 「当前直播」显式**真探一次**：这是有人此刻要答案的场合（最多等 3 秒，单飞锁防惊群）
    live_info = await live.collect_live() if key == "live" else None
    events = await store.list_events()
    if key == "ids" and scope_key == "mine":
        # 「只看自己创建的」在站点这一侧过滤（认人靠 QQ：ownerUid → 成员 → qq），
        # 插件拿到的就是成品——它不该自己去猜谁是届的创建者。
        events = _events_of_qq(events, _clean_qq(qq))
    result = await asyncio.to_thread(
        qqbot.dispatch,
        key,
        settings=settings,
        cfg=cfg,
        state=state,
        events=events,
        ref=ref,
        page=page,
        members=store.members(),
        live_info=live_info,
        scope=scope_key,
    )
    # 「比赛信息 / 比赛详情」再多给一张**卡片图**（信息 + 自动生成的比赛规则），
    # 「比赛结果」给一张**结果图**（逐场比分 + 淘汰赛树状图）：插件先发图、再发文本；
    # 没有 Pillow 时 card 为 null，文本里也已带规则摘要 / 逐场比分，
    # 所以插件那边**不需要**分支判断——照着发就行。
    site = _site_base(request)
    card_info = None
    if cfg is not None and key in ("event", "detail", "result") and not ref:
        card_info = await card.card_for_event(cfg, target, state, site=site, kind=key)
        if card_info:
            card_info = {k: v for k, v in card_info.items() if k != "bytes"}
            card_info["url"] = f"{site}{card_info['url']}"
    parts = qqbot.card_parts(key, card_info, result["parts"])
    return {
        "ok": True,
        "kind": key,
        "eventId": target,
        "eventName": (cfg.event.name or cfg.event.title) if cfg else "",
        "page": result["page"],
        "pages": result["pages"],
        "parts": parts,
        "text": "\n\n".join(parts),
        "card": card_info,
    }


# --------------------------------------------------------------------------- #
# 认人与写操作：群里授权 / 添加成员、取自己的推流地址、私聊投递
#
# 三条不可绕开的原则：
#
# * **认人靠 QQ，且由站点判定**：插件把「发命令那个人」的 QQ 原样带上（取自平台事件，
#   不是用户手输的），站点按这个 QQ 查成员、判权限。插件那边的提示只是体验层——
#   绕过插件直接调接口，照样过不了 ``_actor_member`` 这一关。
# * **密钥只私聊**：新建成员时返回的登录密钥，插件必须私聊转交本人，绝不发群里。
# * **不授予 server_admin**：见 ``BOT_GRANTABLE``。
# --------------------------------------------------------------------------- #
@router.get("/whoami")
async def api_bot_whoami(
    qq: str = Query(default="", description="发命令那个人的 QQ"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """这个 QQ 在站内是什么身份（插件据此决定放不放行，并给出人话提示）。

    **查不到也算正常**：群里大多数人是路人。这时 ``known=false``，插件可以顺带告诉
    服务器管理员「用『比赛授权』把 TA 加进来」。
    """
    clean = _clean_qq(qq)
    member = store.member_by_qq(clean)
    if member is None:
        return {
            "ok": True,
            "qq": clean,
            "known": False,
            "note": (
                "这个 QQ 还没对上站内成员（可能没登记 QQ，或还没被添加）。"
                "服务器管理员可以用「比赛授权 / 比赛添加」把人拉进来。"
            ),
        }
    return {
        "ok": True,
        "qq": clean,
        "known": True,
        "uid": member.uid,
        "name": member.display_name,
        "permission": member.permission,
        "active": member.active,
        "isServer": member.permission == "server_admin",
        "canManageEvents": member.permission in ("event_admin", "server_admin"),
        "streamId": member.stream_id,
    }


@router.post("/members")
async def api_bot_member_grant(
    request: Request,
    payload: BotGrantPayload,
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """群里「授权赛事管理员 / 添加成员」：**只有服务器管理员能调**。

    * 目标已经是成员 → 只改权限（不碰他的密钥与令牌，不覆盖他的资料）；
    * 目标还不是 → 自动建号：名字取群昵称，QQ 就是 QQ 号，权限按请求给，
      然后**由站点把登录密钥私聊给本人**（`keySent` 说明发了没有）；
    * 不允许授予 ``server_admin``（见 :data:`BOT_GRANTABLE`）。
    """
    actor = _actor_member(payload.actor_qq)
    if actor.permission != "server_admin":
        raise HTTPException(status_code=403, detail="只有服务器管理员能在群里授权 / 添加成员")

    permission = (payload.permission or "member").strip()
    if permission not in BOT_GRANTABLE:
        raise HTTPException(
            status_code=400,
            detail=f"权限只能是 {' / '.join(BOT_GRANTABLE)}（服务器管理员请在站点里改）",
        )
    target_qq = _clean_qq(payload.target_qq)
    if not target_qq:
        raise HTTPException(status_code=400, detail="没识别到对方的 QQ：请 @ 本人之后再发命令")
    label = BOT_GRANTABLE[permission]

    existing = store.member_by_qq(target_qq)
    if existing is not None:
        if existing.permission == "server_admin":
            return {
                "ok": True,
                "created": False,
                "changed": False,
                "uid": existing.uid,
                "name": existing.display_name,
                "permission": existing.permission,
                "note": "对方是服务器管理员（最高权限），权限保持不变",
            }
        if existing.permission == permission:
            return {
                "ok": True,
                "created": False,
                "changed": False,
                "uid": existing.uid,
                "name": existing.display_name,
                "permission": existing.permission,
                "note": f"对方已经是{label}，无需改动",
            }
        saved, _key, _bearer = await store.save_member(
            existing.model_copy(update={"permission": permission})
        )
        log.warning(
            "QQ 机器人改权限 | qq=%s | %s → %s | 操作者=%s",
            target_qq,
            existing.permission,
            permission,
            actor.uid,
        )
        return {
            "ok": True,
            "created": False,
            "changed": True,
            "uid": saved.uid,
            "name": saved.display_name,
            "permission": saved.permission,
            "note": "权限已更新（密钥与令牌不变）",
        }

    name = (payload.name or "").strip()[:24] or f"群友 {target_qq}"
    saved, key_plain, _bearer = await store.save_member(
        Member(uid="", name=name, qq=target_qq, permission=permission)
    )
    log.warning(
        "QQ 机器人新建成员 | qq=%s | 权限=%s | 操作者=%s", target_qq, permission, actor.uid
    )
    # 密钥由**站点**直接私聊给本人：它一个字节都不经过插件（插件只拿「发了没有」），
    # 所以群命令这条路径上根本不存在把它打进群里的可能。
    site = _site_base(request)
    sent = await _send_private(
        settings,
        target_qq,
        f"【NTE 比赛】你好 {saved.display_name}，服务器管理员把你设为了{label}。\n"
        f"登录密钥（只显示这一次，请立即保存）：{key_plain}\n\n"
        f"用法：打开 {site} 用这把密钥登录（{site}/user 改自己的资料）。\n"
        + _secret_notice("key", site),
    )
    return {
        "ok": True,
        "created": True,
        "changed": True,
        "uid": saved.uid,
        "name": saved.display_name,
        "permission": saved.permission,
        "keySent": bool(sent.get("ok")),
        "detail": sent.get("detail") or "",
        "note": "已新建成员；登录密钥只私聊给了 TA 本人",
    }


@router.get("/my-links")
async def api_bot_my_links(
    request: Request,
    qq: str = Query(default="", description="本人的 QQ"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """本人（按 QQ 认）的**推流地址**与**站内直播间地址**。

    直播间给的是**本站**的地址（``/channels/<推流 ID>``），不是媒体服务器的观看地址——
    群里发出去的是「来我们站里看」，而不是一串带端口的内部域名。
    推流地址只能是媒体服务器的 WHIP 地址（推流本来就走它），另需本人的 Bearer 令牌，
    而令牌只存哈希、取不回明文，所以这里给地址、令牌让本人去「我的」页轮换。
    """
    member = _actor_member(qq)  # 查不到 / 被停用都会抛 403，提示文案一致
    stream_id = (member.stream_id or "").strip()
    base = _site_base(request)
    cfg = store.snapshot()
    push = logic.push_endpoints(cfg.stream, stream_id) if stream_id else {}
    push_url = push.get("whipPush", "")
    room_url = f"{base}/channels/{quote(stream_id)}" if stream_id else ""

    parts: list[str] = [f"【{member.display_name}】你的推流与直播间"]
    if not stream_id:
        parts.append(
            "你还没有设置「推流 ID」。\n"
            f"到 {base}/user 填一个（英文 / 数字），之后这里就能给出推流与直播间地址。"
        )
    else:
        block = ["—— 推流（OBS）——", "服务：WHIP"]
        block.append(f"服务器：{push_url}" if push_url else "服务器：站点还没填媒体服务器地址（「服务器 → 直播配置」）")
        block.append("Bearer 令牌：填到 OBS 的「Bearer 令牌」字段")
        block.append("（每人一把；忘了就在「我的」页轮换一次，旧令牌立即失效）")
        block.append("—— 直播间（本站）——")
        block.append(room_url or f"{base}/channels/{stream_id}")
        if member.room_title:
            block.append(f"标题：{member.room_title}")
        block.append("开播后会自动出现在「频道」里，观众点开就能看。")
        parts.append("\n".join(block))
    return {
        "ok": True,
        "uid": member.uid,
        "name": member.display_name,
        "streamId": stream_id,
        "roomTitle": member.room_title,
        "pushUrl": push_url,
        "roomUrl": room_url,
        "siteBase": base,
        "parts": parts,
        "text": "\n\n".join(parts),
    }


@router.post("/notify")
async def api_bot_notify(
    payload: BotNotifyPayload,
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """把一条消息**私聊**发给某个 QQ（插件用它发帮助文档 / 新密钥 / 推流地址）。

    为什么由站点代发：站点已经握着 AstrBot 的 API Key 与统一发送通道（群推送走的就是它），
    插件不必再依赖某个 AstrBot 版本的私聊接口。**只发私聊**——这个口子开的是"给某人
    自己发"，而不是"替我往群里喊话"（往群里发请走网页的推送按钮 / 召集命令）。
    """
    qq = _clean_qq(payload.qq)
    text = str(payload.text or "").strip()
    if not qq:
        raise HTTPException(status_code=400, detail="没有目标 QQ")
    if not text:
        raise HTTPException(status_code=400, detail="消息内容为空")
    result = await _send_private(settings, qq, text)
    return {
        "ok": bool(result.get("ok")),
        "umo": qqbot.private_umo(settings, qq),
        "detail": result.get("detail") or "",
    }


@router.post("/credential")
async def api_bot_credential(
    request: Request,
    payload: BotCredentialPayload,
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """凭据重置：**默认改自己**，服务器管理员可以替别人改。

    四条规矩（都是「凭据」这件事逼出来的）：

    * **谁改谁**：不填 ``targetQq`` = 改自己（身份取自插件上报的 QQ，不是用户手输）；
      填了别人 = 只有**服务器管理员**能这么做（见 :func:`_credential_target`）；
    * **新值只发给被改的那个人**：无论自助还是代改，私聊都发到 ``targetQq``；
      响应体里**不含任何明文**——插件拿不到，也就不可能被它打进群里，
      连服务器管理员自己也看不到别人（或自己）的新密钥 / 令牌；
    * **令牌要有推流码才发**：没有推流码（推流 ID）的成员，令牌没地方填，
      先让他设/改推流码——不然发出去也只是一串无处可用的字符；
    * **私聊发不出去时**只回「没发出去」，让人再发一次命令（凭据已经换了）。

    **先换后发**：凭据换成功但私聊失败时，旧值已经作废，本人再发一次命令就能拿到新的
    （换之前先探一次私聊做不到「原子」——私聊通道本身也可能中途挂掉）。
    """
    actor = _actor_member(payload.qq)
    member, for_other = _credential_target(actor, payload.target_qq)
    kind = _CREDENTIAL_ALIAS.get(str(payload.what or "").strip().lower())
    if kind is None:
        raise HTTPException(status_code=400, detail="要重置什么？只能是 密钥 / 令牌 / 推流码")
    site = _site_base(request)
    cfg = store.snapshot()
    push_hint = "（站点还没填媒体服务器地址，请找服务器管理员）"
    # 代改时在私聊里说清「谁帮你重置的」：本人对不上号时能立刻再改一次
    by_line = f"（这次由服务器管理员「{actor.display_name}」帮你重置）\n" if for_other else ""

    if kind == "key":
        _saved, key_plain, _bearer = await store.save_member(member, new_key=True)
        text = (
            f"【NTE 比赛】你的登录密钥已重置\n{by_line}"
            f"登录密钥（只显示这一次）：{key_plain}\n\n"
            f"用法：打开 {site}，用这把密钥登录（{site}/user 改自己的资料）。\n"
            + _secret_notice("key", site)
        )
        note = "登录密钥已重置"
    elif kind == "token":
        stream_id = (member.stream_id or "").strip()
        if not stream_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "你还没有推流码（推流 ID），令牌没地方填。"
                    "先发「比赛改推流码 你的流名」（英文 / 数字），再来重置令牌。"
                ),
            )
        _saved, _key, bearer_plain = await store.save_member(member, new_bearer=True)
        push = logic.push_endpoints(cfg.stream, stream_id).get("whipPush", "")
        text = (
            f"【NTE 比赛】你的直播令牌已重置\n{by_line}"
            f"推流码（推流 ID）：{stream_id}\n"
            f"推流服务器（WHIP）：{push or push_hint}\n"
            f"Bearer 令牌（只显示这一次）：{bearer_plain}\n\n"
            "用法：OBS → 设置 → 直播 → 服务选 WHIP，服务器填上面的地址，"
            "「Bearer 令牌」填这一串。\n"
            f"你的直播间：{site}/channels/{quote(stream_id)}\n"
            + _secret_notice("token", site)
        )
        note = "直播令牌已重置"
    else:  # streamId：改推流码
        try:
            key = logic.check_stream_key(payload.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not key:
            raise HTTPException(
                status_code=400, detail="推流码不能为空；用法：比赛改推流码 你的流名（英文 / 数字）"
            )
        ensure_stream_unique(key, member.uid)  # 与成员管理共用同一条唯一性规则
        await store.save_member(member.model_copy(update={"stream_id": key}))
        push = logic.push_endpoints(cfg.stream, key).get("whipPush", "")
        text = (
            f"【NTE 比赛】你的推流码已改为 {key}\n{by_line}"
            f"推流服务器（WHIP）：{push or push_hint}\n"
            "Bearer 令牌：没变（还是原来那一串；忘了就发「比赛重置令牌」换一把）\n\n"
            f"你的直播间：{site}/channels/{quote(key)}\n"
            "OBS 里的服务器地址要跟着一起改，令牌不用动；"
            "如果此刻正在推流，改完要在 OBS 里重新「开始推流」才会到新地址。\n"
            "注意：推流码本身是公开的（观看地址里就有它），别拿它当密码——"
            "真正拦住别人的是 Bearer 令牌。"
        )
        note = f"推流码已改为 {key}"

    # 新值发给**被改的那个人**：自助时就是本人；代改时是对方（管理员自己也看不到）
    sent = await _send_private(settings, member.qq, text)
    tail = (
        "，新值只私聊发给你本人"
        if not for_other
        else f"，新值只私聊发给 {member.display_name} 本人（你这边看不到）"
    )
    return {
        "ok": True,
        "kind": kind,
        "name": member.display_name,
        "forOther": for_other,
        "toQq": _clean_qq(member.qq),
        "sent": bool(sent.get("ok")),
        "detail": sent.get("detail") or "",
        "note": f"{note}{tail}",
    }


# --------------------------------------------------------------------------- #
# 比赛直播注册：把「开播要用的东西」一次给全（缺什么补什么）
# --------------------------------------------------------------------------- #
def _push_checklist() -> list[str]:
    """推流注意事项（直播注册 / 我的推流共用同一份，不各写一遍）。"""
    return [
        "OBS → 设置 → 直播 → 服务选 WHIP（WebRTC），服务器填上面的地址。",
        "OBS 里把「B 帧 / B-frames」设为 0、关键帧间隔 2 秒：B 帧在 WebRTC 下最容易花屏，甚至推不上去。",
        "Bearer 令牌填在 OBS 的「Bearer 令牌」字段（不是密码那一栏）。",
        "令牌别外传：谁拿到都能用你的推流码顶掉你的画面；丢了就再发一次「比赛直播注册」。",
        "推流码本身是公开的（观看地址里就有它），别拿它当密码——拦住别人的是令牌。",
    ]


def _suggest_stream_key(member: Member) -> str:
    """给还没设过推流码的人自动生成一个：认得出是谁，又不撞车。

    名字里的 ASCII 部分优先（``tom-3f9a1c`` 一眼看得出是谁），中文名抠不出字符就退回 uid；
    真的撞上了（同名 + uid 前缀巧合）就加长后缀，最后仍不行就交回调用方（让管理员指定）。
    """
    base = logic.clean_key(member.name)
    uid = member.uid or "000000"
    for suffix in (uid[:6], uid[:10], uid):
        key = (f"{base}-{suffix}" if base else f"nte-{suffix}")[:24]
        try:
            ensure_stream_unique(key, member.uid)
        except HTTPException:
            continue
        return key
    return ""


class BotStreamSetupPayload(NTEModel):
    """比赛直播注册：``stream_key`` 可选（他还没有推流码时用这个，不填就自动生成）。"""

    qq: str = ""
    target_qq: str = ""
    stream_key: str = ""


@router.post("/stream-setup")
async def api_bot_stream_setup(
    request: Request,
    payload: BotStreamSetupPayload,
    settings: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """**比赛直播注册**：缺什么补什么，结果只私聊给本人。

    三种情况分别给不同的说法（这是用户唯一能自助搞定推流的入口，含糊不起）：

    * **还没有推流码**（第一次注册，最常见）→ 给一个推流码 + 一把新令牌，
      连推流地址与注意事项一起发给他；
    * **有推流码、没令牌**（老数据里令牌是空的）→ 推流码照用，补一把新令牌；
    * **两个都有** → **什么都不改**，只把推流码 / 推流地址 / 直播间地址与注意事项给他，
      并提醒「令牌是原来那一串；忘了就发「比赛重置令牌」换一把新的」。

    **有推流码的人，令牌绝不会被顺手换掉**：令牌只存哈希、取不回明文，而重置是破坏性动作
    ——正在推流的人会被当场顶下线。所以「已推流码 + 已令牌」这一路只提醒、不动手
    （要换得他自己发「比赛重置令牌」）。

    为什么「没有推流码就一定发新令牌」：新建成员时站点会随手生成一把令牌，但**从来没发给过他**
    （建号那条私聊里只有登录密钥），所以他手上其实一把都没有。而没有推流码的人
    **不可能正在推流**（``live.authorize_publish`` 是按流名反查成员后校验令牌的），
    换掉那把没人见过的令牌不会影响任何人——倒是能让「注册」这条命令一次给全。

    管理员可 @ 代办，私聊仍只发本人。
    """
    actor = _actor_member(payload.qq)
    member, for_other = _target_for(actor, payload.target_qq, action="替别人注册直播")
    cfg = store.snapshot()
    site = _site_base(request)
    current = member
    created: list[str] = []
    stream_id = (member.stream_id or "").strip()
    # 「这次注册之前他还没有推流码」——下面发令牌要按**当时**的状态判断（见 docstring）
    first_time = not stream_id

    if not stream_id:
        raw = str(payload.stream_key or "").strip()
        key = ""
        if raw:
            try:
                key = logic.check_stream_key(raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not key:
                raise HTTPException(
                    status_code=400,
                    detail="推流码只能用 ASCII 字母、数字、连字符(-)与下划线(_)；"
                    "用法：比赛直播注册 你的流名",
                )
        else:
            key = _suggest_stream_key(current)
        if key:
            ensure_stream_unique(key, current.uid)
            current, _key, _bearer = await store.save_member(
                current.model_copy(update={"stream_id": key})
            )
            stream_id = key
            created.append(f"推流码 {key}")
        else:
            raise HTTPException(
                status_code=400,
                detail="没能自动生成推流码（可用的名字都撞车了），请自己指定一个：比赛直播注册 你的流名",
            )

    bearer_plain = ""
    # 发新令牌的两种情形：① 一把都没有（老数据）；② 这次是**第一次注册**（原本没有推流码）
    # ——新建成员那把令牌从没发给过他，而他也不可能正在推流（见 docstring）。
    # 「原本就有推流码 + 已有令牌」的人**什么都不动**——他可能正开着播。
    if not current.bearer_stored or first_time:
        current, _key, bearer_plain = await store.save_member(current, new_bearer=True)
        created.append("一把新的直播令牌")

    push = logic.push_endpoints(cfg.stream, stream_id).get("whipPush", "")
    room_url = f"{site}/channels/{quote(stream_id)}" if stream_id else ""
    by_line = f"（这次由服务器管理员「{actor.display_name}」帮你注册）\n" if for_other else ""
    lines = [
        f"【NTE 比赛】{current.display_name} 的直播注册结果\n{by_line}",
        "—— 你要用的东西 ——",
        f"推流码（推流 ID）：{stream_id}",
        f"推流服务器（WHIP）：{push or '（站点还没填媒体服务器地址，请找服务器管理员）'}",
    ]
    if bearer_plain:
        lines.append(f"Bearer 令牌（只显示这一次，请立即保存）：{bearer_plain}")
    else:
        lines.append(
            "Bearer 令牌：你已经有令牌了，这里不重复显示（服务端只存哈希，看不到原文）。\n"
            "直播继续用原来那一串就行；如果忘了或可能泄露了，发一次「比赛重置令牌」换一把新的"
            "（旧令牌立即失效，所以别在有人的时候乱试）。"
        )
    lines.append(f"你的直播间（本站）：{room_url}")
    if current.room_title:
        lines.append(f"直播间标题：{current.room_title}")
    if current.bili_room:
        lines.append(
            f"B站直播间号：{current.bili_room}"
            "（你在 B站 开播时，赛事直播页会自动多出一路 B站 机位，标题自动同步）"
        )
    lines.append("")
    lines.append("—— 注意事项 ——")
    lines.extend(f"· {item}" for item in _push_checklist())
    lines.append("")
    lines.append(
        "推流码与推流地址随时可以再要一次（再发一次「比赛直播注册」就行）；"
        + (
            "但令牌只显示这一次，请现在就存好——忘了或泄露了就发「比赛重置令牌」换一把新的"
            "（旧的一把会立即失效）。"
            if bearer_plain
            else "令牌本站看不到原文；忘了或泄露了就发「比赛重置令牌」换一把新的。"
        )
    )
    sent = await _send_private(settings, current.qq, "\n".join(lines))
    tail = (
        "，详情只私聊发给你本人"
        if not for_other
        else f"，详情只私聊发给 {current.display_name} 本人（你这边看不到）"
    )
    note = "直播注册完成：" + "、".join(created) if created else "直播注册状态已发给他（无需改动）"
    return {
        "ok": True,
        "name": current.display_name,
        "forOther": for_other,
        "toQq": _clean_qq(current.qq),
        "streamId": stream_id,
        "created": created,
        "hasToken": bool(current.bearer_stored),
        "sent": bool(sent.get("ok")),
        "detail": sent.get("detail") or "",
        "note": f"{note}{tail}",
    }


# --------------------------------------------------------------------------- #
# 游戏 UUID：群内就能查（不需要权限、也不走私聊）
# --------------------------------------------------------------------------- #
@router.get("/uid")
async def api_bot_uid(
    qq: str = Query(default="", description="发命令那个人的 QQ"),
    target_qq: str = Query(default="", alias="targetQq", description="@ 到的人（留空 = 查自己）"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """游戏 UUID：**群里直接回**（举办者要拿它把人加进游戏）。

    为什么不做成私聊：它不是凭据，也不敏感——游戏里加好友本来就要互相给 UUID，
    举办者收名单时也天天在问。**不要求任何权限**：谁都能查自己，也能查 @ 到的人。

    只回**名字 + UUID**，不回 QQ：发话人是谁、被 @ 的是谁，群里本来就看得到。
    """
    target = _clean_qq(target_qq) or _clean_qq(qq)
    member = store.member_by_qq(target)
    if member is None:
        who = "你" if target == _clean_qq(qq) else "TA"
        return {
            "ok": True,
            "known": False,
            "qq": target,
            "parts": [
                (
                    f"没查到 {who} 的成员资料：{who} 的 QQ 还没在站点登记（群里只能靠 QQ 认人）。\n"
                    f"到站点「我的」页填上自己的 QQ，或让服务器管理员发「比赛添加 @{who}」。"
                )
            ],
        }
    uuid = (member.game_uuid or "").strip()
    if uuid:
        body = (
            f"【NTE 比赛】{member.display_name} 的游戏 UUID\n"
            f"{uuid}\n"
            "（把这一串给举办者，他就能把你加进游戏；改它：私聊「比赛资料 游戏UID 新的UUID」）"
        )
    else:
        body = (
            f"【NTE 比赛】{member.display_name} 还没登记游戏 UUID。\n"
            "填上它：私聊「比赛资料 游戏UID 你的UUID」（举办者加人时要用）"
        )
    return {
        "ok": True,
        "known": True,
        "qq": target,
        "name": member.display_name,
        "uuid": uuid,
        "parts": [body],
        "text": body,
    }


# --------------------------------------------------------------------------- #
# 资料：查看与修改（私聊发本人）
# --------------------------------------------------------------------------- #
#: 资料字段的写法容错（用户不必记准我们内部叫什么）
_PROFILE_ALIAS = {
    "name": "name",
    "名字": "name",
    "昵称": "name",
    "uuid": "gameUuid",
    "uid": "gameUuid",
    "游戏uid": "gameUuid",
    "游戏uuid": "gameUuid",
    "异环uid": "gameUuid",
    "游戏id": "gameUuid",
    "bili": "biliRoom",
    "bilibili": "biliRoom",
    "b站": "biliRoom",
    "b站房间号": "biliRoom",
    "b站直播间": "biliRoom",
    "直播间号": "biliRoom",
    "stream": "streamId",
    "streamid": "streamId",
    "推流码": "streamId",
    "推流id": "streamId",
    "流名": "streamId",
    "room": "roomTitle",
    "直播间": "roomTitle",
    "直播间标题": "roomTitle",
    "标题": "roomTitle",
    "qq": "qq",
    "qq号": "qq",
    "手机": "qq",
}

#: 「清空这一项」的写法（不想再填 B站 房间号 / 游戏 UUID 时）
_CLEAR_WORDS = frozenset({"清空", "空", "清除", "删除", "去掉", "无", "没有", "-", "—", "clear", "none"})


def _is_clear(raw: str) -> bool:
    return str(raw or "").strip().lower() in _CLEAR_WORDS


def _profile_text(member: Member, cfg: Any, site: str) -> str:
    """本人资料全文（私聊发本人）：**只有「有没有」**，永远不含密钥 / 令牌明文。"""
    stream_id = (member.stream_id or "").strip()
    push = logic.push_endpoints(cfg.stream, stream_id).get("whipPush", "") if stream_id else ""
    room_url = f"{site}/channels/{quote(stream_id)}" if stream_id else ""
    or_dash = lambda value: value or "（未填）"
    lines = [
        f"【NTE 比赛】{member.display_name} 的资料",
        f"· QQ：{or_dash(member.qq)}（群里靠它认人——写错就等于换了个人）",
        f"· 名字：{or_dash(member.name)}",
        f"· 游戏 UUID：{or_dash(member.game_uuid)}",
        f"· 推流码（推流 ID）：{or_dash(stream_id)}",
        f"· 推流服务器（WHIP）：{push or '（站点还没填媒体服务器地址）'}",
        f"· 直播间（本站）：{room_url or '（先有推流码）'}",
        f"· 直播间标题：{or_dash(member.room_title)}",
        f"· B站直播间号：{or_dash(member.bili_room)}",
        f"· 登录密钥：{'已设置（看不到原文，只能换新的）' if member.key_stored else '未设置'}",
        f"· 直播令牌：{'已设置（看不到原文，只能换新的）' if member.bearer_stored else '未设置'}",
        "",
        "—— 怎么改（把下面某一条原样发给我）——",
        "· 名字          比赛资料 名字 新名字",
        "· 游戏 UUID     比赛资料 游戏UID 你的UUID",
        "· B站 房间号    比赛资料 B站 12345",
        "· 推流码        比赛资料 推流码 新的流名",
        "· 直播间标题    比赛资料 直播间 今晚开黑",
        "· QQ 号         比赛资料 QQ 你的QQ号",
        "· 清空某一项    该命令后面写「清空」（例如：比赛资料 B站 清空）",
        "",
        "—— 另外几条 ——",
        "· 直播怎么推（地址 / 令牌 / 注意事项）：比赛直播注册",
        "· 换登录密钥：比赛重置密钥　换直播令牌：比赛重置令牌",
        "· 看推流地址与直播间地址：比赛我的",
        "",
        "改完只会私聊给你确认，不会发到群里；令牌与密钥只能换新的，看不了原文。",
    ]
    return "\n".join(lines)


class BotProfilePayload(NTEModel):
    """改资料：``field`` 是字段名（见 ``_PROFILE_ALIAS`` 的容错表），``value`` 是新值。"""

    qq: str = ""
    target_qq: str = ""
    field: str = ""
    value: str = ""


@router.get("/profile")
async def api_bot_profile(
    request: Request,
    qq: str = Query(default="", description="发命令那个人的 QQ"),
    target_qq: str = Query(default="", alias="targetQq", description="@ 到的人（留空 = 看自己）"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """资料全文（私聊发本人）：**改什么、怎么改都写在这一份里**，用户不必记命令。

    不 @ 人 = 自己（按发命令的 QQ 认人）；@ 了人 = 替 TA 看，**只有服务器管理员**能这么做，
    而且这份答复仍然只发给**被看的那个人**（要发给谁由站点回的 ``toQq`` 决定，
    插件照它发就不会发错人）。
    """
    actor = _actor_member(qq)
    member, for_other = _target_for(actor, target_qq, action="替别人查看资料")
    text = _profile_text(member, store.snapshot(), _site_base(request))
    return {
        "ok": True,
        "uid": member.uid,
        "name": member.display_name,
        "forOther": for_other,
        "toQq": _clean_qq(member.qq),
        "parts": [text],
        "text": text,
    }


@router.post("/profile")
async def api_bot_profile_update(
    request: Request,
    payload: BotProfilePayload,
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """改一项资料，回一份**新的资料全文**（调用方把这一份私聊发给本人）。

    几条刻意的规矩：

    * **一次只改一项**：`field` + `value`。含糊的批量更新更容易误伤
      （少写一个字段就顺手清空一项），而群里打错字的概率实在不低；
    * **推流码走唯一性校验**：与成员管理、改推流码共用 ``ensure_stream_unique``；
    * **QQ 号改动要查重**：两位成员填同一个 QQ 会让「按 QQ 认人」变成歧义，
      站点那边会直接当作查不到——所以这里先拦住；
    * **值可以清空**：写「清空」即可（不想再填 B站 房间号时），
      但空串不算清空（免得一个手滑就把资料抹了）。
    """
    actor = _actor_member(payload.qq)
    member, for_other = _target_for(actor, payload.target_qq, action="替别人改资料")
    field = _PROFILE_ALIAS.get(str(payload.field or "").strip().lower())
    if field is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "要改哪一项？可用：名字 / 游戏UID / B站 / 推流码 / 直播间 / QQ。\n"
                "用法：比赛资料 名字 新名字（私聊「比赛资料」可以看到完整写法）"
            ),
        )
    raw = str(payload.value or "").strip()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail=f"「{payload.field}」要改成什么？用法：比赛资料 {payload.field} 新值"
            "（想清空就写「清空」）",
        )
    clear = _is_clear(raw)
    update: dict[str, Any] = {}
    label = field
    if field == "name":
        if clear:
            raise HTTPException(status_code=400, detail="名字不能清空——总得有个称呼")
        value = " ".join(raw.split())[:24]
        update, label = {"name": value}, f"名字 → {value}"
    elif field == "gameUuid":
        value = "" if clear else raw[:64]
        update, label = {"game_uuid": value}, ("游戏 UUID 已清空" if clear else f"游戏 UUID → {value}")
    elif field == "biliRoom":
        if clear:
            update, label = {"bili_room": ""}, "B站直播间号已清空（不再显示 B站 那一路）"
        else:
            digits = "".join(ch for ch in raw if ch.isdigit())
            if not digits:
                raise HTTPException(
                    status_code=400,
                    detail="B站直播间号只能填数字（也可以直接粘直播间链接）",
                )
            update, label = {"bili_room": digits[:12]}, f"B站直播间号 → {digits[:12]}"
    elif field == "roomTitle":
        value = "" if clear else raw[:30]
        update, label = {"room_title": value}, ("直播间标题已清空" if clear else f"直播间标题 → {value}")
    elif field == "qq":
        if clear:
            raise HTTPException(
                status_code=400,
                detail="QQ 号不能清空——群里就是靠它认人的（填错了可以改成对的）",
            )
        digits = _clean_qq(raw)
        if not digits:
            raise HTTPException(status_code=400, detail="QQ 号只能是数字")
        clash = store.member_by_qq(digits)
        if clash is not None and clash.uid != member.uid:
            raise HTTPException(
                status_code=400, detail=f"这个 QQ 已经被成员「{clash.display_name}」登记了"
            )
        update, label = {"qq": digits}, f"QQ → {digits}"
    else:  # streamId
        if clear:
            update, label = {"stream_id": ""}, "推流码已清空（直播间地址也会一起失效）"
        else:
            try:
                key = logic.check_stream_key(raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not key:
                raise HTTPException(status_code=400, detail="推流码不能为空；想清空请写「清空」")
            ensure_stream_unique(key, member.uid)  # 与成员管理共用同一条唯一性规则
            update, label = {"stream_id": key}, f"推流码 → {key}"

    saved, _new_key, _new_bearer = await store.save_member(member.model_copy(update=update))
    log.warning(
        "QQ 机器人改资料 | qq=%s | 字段=%s | 操作者=%s | 代改=%s",
        _clean_qq(saved.qq),
        field,
        actor.uid,
        for_other,
    )
    text = _profile_text(saved, store.snapshot(), _site_base(request))
    return {
        "ok": True,
        "name": saved.display_name,
        "field": field,
        "note": label,
        "changed": [label],
        "forOther": for_other,
        "toQq": _clean_qq(saved.qq),
        "parts": [text],
        "text": text,
    }
