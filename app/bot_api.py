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
| `GET /api/bot/managers` | **有资格召集**的人的 QQ（服务器管理员 + 举办者） |
| `GET /api/bot/query` | 直接拿到**可以原样发到群里**的纯文本（分段已切好） |
| `GET /api/bot/whoami` | 按 QQ 认人：这个人在站内是什么身份、有没有权限 |
| `POST /api/bot/members` | 群里授权 / 添加成员（**仅服务器管理员**；新建成员时**站点直接把密钥私聊给本人**） |
| `GET /api/bot/my-links` | 本人的推流地址 + **站内**直播间地址 |
| `POST /api/bot/credential` | 凭据重置：重置登录密钥 / 重置直播令牌 / 改推流码（默认改自己；服务器管理员可代改，**新值只私聊给被改的那个人**） |
| `POST /api/bot/notify` | 把一条消息**私聊**发给某人（帮助说明 / 推流地址） |

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

from . import live, logic, qqbot
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


def _credential_target(actor: Member, raw_qq: str) -> tuple[Member, bool]:
    """这次改**谁**的凭据；返回 ``(成员, 是不是在替别人改)``。

    不填 / 填自己 = 自助。填了别人的 QQ 必须是**服务器管理员**：代改是管理动作，
    不是「谁都能顺手把别人的登录口令作废」。

    新值始终只发给**被改的那个人**（见 ``api_bot_credential`` 末尾），
    所以服务器管理员代改也拿不到密钥 / 令牌——他要的只是「帮不上线的人重置」。
    """
    target_qq = _clean_qq(raw_qq)
    if not target_qq or target_qq == _clean_qq(actor.qq):
        return actor, False
    if actor.permission != "server_admin":
        raise HTTPException(status_code=403, detail="只有服务器管理员能替别人重置凭据")
    target = store.member_by_qq(target_qq)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="这个 QQ 还不是成员：先用「比赛添加 @某人」把 TA 加进来，再重置凭据。",
        )
    if not target.active:
        raise HTTPException(status_code=403, detail="TA 的成员账号已被停用，先启用再重置凭据")
    return target, True


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
            {"command": "比赛届次", "kind": "—", "args": "—", "alias": ["届次列表", "比赛编号"], "note": "用 /api/bot/events"},
            {"command": "比赛信息", "kind": "event", "args": "届次（可选）", "alias": ["赛事信息", "比赛时间", "nteginfo"]},
            {"command": "比赛进度", "kind": "progress", "args": "届次（可选）", "alias": ["进度", "赛程", "赛程进度", "现在打谁", "打到哪了", "谁领先", "什么情况"]},
            {"command": "比赛下一场", "kind": "next", "args": "届次（可选）", "alias": ["下一场", "下场", "接下来", "现在打", "接着打谁", "等下打谁"]},
            {"command": "比赛结果", "kind": "result", "args": "届次（可选）", "alias": ["结果", "成绩", "比分", "赢了吗", "什么比分", "结果咋样"]},
            {"command": "比赛冠军", "kind": "champion", "args": "届次（可选）", "alias": ["冠军", "榜首", "谁赢了"]},
            {"command": "比赛名单", "kind": "roster", "args": "届次（可选）", "alias": ["参赛名单", "选手名单", "比赛选手", "队伍", "都有谁"]},
            {"command": "比赛详情", "kind": "detail", "args": "届次 + 场次（可选）", "alias": ["赛事详情", "场次详情", "单场"]},
            {
                "command": "比赛召集",
                "kind": "call",
                "args": "届次（可选）",
                "alias": ["召集参赛", "集合", "喊人"],
                "note": "@ 由插件用 At 组件发；只有本届举办者 / 服务器管理员能召集（名单见 /api/bot/managers）",
            },
            {
                "command": "比赛我的",
                "kind": "—",
                "args": "—",
                "alias": ["我的推流", "我的直播间", "推流地址", "我的地址"],
                "note": "用 /api/bot/my-links：按 QQ 认人，私聊回本人的推流地址 + **站内**直播间地址",
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


@router.get("/managers")
async def api_bot_managers(
    event_id: str = Query(default="", alias="eventId"),
    _: dict[str, Any] = Depends(require_bot_token),  # noqa: B008
) -> dict[str, Any]:
    """本届**有资格召集**的人的 QQ（服务器管理员 + 举办者）。

    为什么要这个接口：召集会 @ 全场参赛者，属于「会打扰很多人」的动作；而**在群里
    只能靠 QQ 认人**——谁是这一届的举办者，只有站点这边知道。插件拿这个名单比对
    发命令的人，不在名单里就拒绝。

    只回 QQ 列表，不回 uid / 权限等其它信息；名单为空时 ``note`` 里说明原因
    （常见原因：举办者还没在「我的」页填 QQ）。
    """
    target = (event_id or "").strip() or store.current_id
    try:
        cfg = store.snapshot() if target == store.current_id else await store.read_event(target)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"没有这一届：{target}") from exc
    owner_uid = str(cfg.event.owner_uid or "")
    qqs: list[str] = []
    for member in store.members():
        if not member.active or not member.qq:
            continue
        if member.permission == "server_admin" or (owner_uid and member.uid == owner_uid):
            qqs.append(str(member.qq))
    unique = list(dict.fromkeys(qqs))
    return {
        "ok": True,
        "eventId": target,
        "eventName": cfg.event.name or cfg.event.title,
        "qqs": unique,
        "count": len(unique),
        "note": (
            "召集权限：服务器管理员 + 本届举办者（按成员资料里的 QQ 认人）"
            if unique
            else "本届还没有登记 QQ 的服务器管理员或举办者，无法判断谁有资格召集"
        ),
    }


@router.get("/query")
async def api_bot_query(
    kind: str = Query(default="event"),
    event_id: str = Query(default="", alias="eventId"),
    ref: str = Query(default=""),
    page: int = Query(default=1),
    at: bool = Query(default=True, description="召集类是否带 @ 片段（插件自己发 At 时传 0）"),
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
    target = (event_id or "").strip() or store.current_id
    cfg = None
    state: dict[str, Any] = {}
    # list 与 live 都是**全局**信息，不需要挑届次（live 是成员机位 / 主直播间的事）
    if key not in ("list", "live"):
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
    result = await asyncio.to_thread(
        qqbot.dispatch,
        key,
        settings=settings,
        cfg=cfg,
        state=state,
        events=await store.list_events(),
        ref=ref,
        page=page,
        members=store.members(),
        live_info=live_info,
    )
    return {
        "ok": True,
        "kind": key,
        "eventId": target,
        "eventName": (cfg.event.name or cfg.event.title) if cfg else "",
        "page": result["page"],
        "pages": result["pages"],
        "parts": result["parts"],
        "text": "\n\n".join(result["parts"]),
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
