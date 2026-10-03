"""成员 / 服务器管理 / 直播封禁的路由。

设计要点：

* **成员**是全局账号：``uid`` 是网站用户 UUID（自动生成、不可改），
  ``permission`` 决定权限；密钥（登录凭证）与 Bearer 令牌只由服务端随机生成，
  明文只在生成 / 轮换的这一次回给前端，库里只存 sha256；
* **服务器管理员**全局有且只有一个（启动自检见 ``store.ensure_server_admin``），
  可管理任意成员、任意届次、全局直播封禁与自定义 HTML；
* **赛事管理员**可管理自己创建的届，可对本届直播间的推流做封禁（scope=event），
  但不能解除封禁、也不能碰成员账号；
* **普通成员**只能改自己的资料（``/api/me``），可自行轮换自己的密钥 / 令牌。

本模块只做「路由 + 参数校验 + 调用 store/live」，业务规则尽量下沉。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import avatars, live, logic, login_guard
from .auth import Session, auth
from .logging_conf import get_logger
from .models import LiveBan, Member, NTEModel
from .security import (
    current_event_owner,
    require_admin,
    require_event,
    require_event_owned,
    require_server,
)
from .store import now_iso, store

log = get_logger("members")

router = APIRouter(prefix="/api", tags=["members"])

# 允许通过接口设置的权限值（服务器管理员也在此列，但接口层会阻止出现第二个）
_PERMISSIONS = ("member", "event_admin", "server_admin")


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class MemberPayload(NTEModel):
    """新增 / 更新成员。``uid`` 留空 = 新建（uid 自动生成）。"""

    uid: str = ""
    name: str = ""
    qq: str = ""
    avatar: str = ""
    game_uuid: str = ""
    stream_id: str = ""
    room_title: str = ""
    note: str = ""
    permission: str = "member"
    active: bool = True


class RotatePayload(NTEModel):
    """轮换凭据：指定要轮换哪一项。"""

    key: bool = False
    bearer: bool = False


class MePayload(NTEModel):
    """成员自助修改个人资料（不含 uid / permission / 凭据）。"""

    name: str = ""
    qq: str = ""
    avatar: str = ""
    game_uuid: str = ""
    stream_id: str = ""
    room_title: str = ""
    note: str = ""


class ServerConfigPayload(NTEModel):
    """服务器级配置：自定义 HTML（用于接入统计 / 数据采集）。"""

    custom_html: str = ""


class LoginGuardPayload(NTEModel):
    """登录失败限制（类 fail2ban）配置；字段缺省 = 不改动。"""

    enabled: bool | None = None
    max_attempts: int | None = None
    window_seconds: int | None = None
    ban_seconds: int | None = None
    trusted_proxies: str | None = None
    whitelist: str | None = None


class LiveBanPayload(NTEModel):
    """封禁直播间（并强制掐断）。

    ``minutes`` > 0 = 封 N 分钟；``permanent`` = 永久；两者都不给 = 只掐断、不封禁。
    ``until`` 可直接给 ISO 时间（优先于 minutes）。
    """

    member_uid: str = ""
    stream_id: str = ""
    reason: str = ""
    minutes: int = 0
    permanent: bool = False
    until: str = ""
    scope: str = ""      # global / event；缺省按权限推断
    kick: bool = True    # 是否同时强制掐断


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _member_or_404(uid: str) -> Member:
    member = store.member(uid)
    if member is None:
        raise HTTPException(status_code=404, detail=f"成员 {uid} 不存在")
    return member


def _ensure_stream_unique(stream_id: str, uid: str) -> None:
    """推流 ID 全局唯一：与其它成员、以及遗留频道冲突都拒绝。"""
    key = logic.clean_key(stream_id)
    if not key:
        return
    clash = next((m for m in store.members() if m.uid != uid and m.stream_id == key), None)
    if clash is not None:
        raise HTTPException(
            status_code=400, detail=f"推流 ID「{key}」已被成员 {clash.display_name} 使用"
        )
    clash_channel = next(
        (c for c in store.channels() if logic.clean_key(c.stream_key) == key), None
    )
    if clash_channel is not None:
        raise HTTPException(
            status_code=400, detail=f"推流 ID「{key}」已被频道 {clash_channel.display_name} 使用"
        )


def _member_public(member: Member, session: Session, *, self_view: bool = False) -> dict[str, Any]:
    """成员视图：服务器管理员与**成员本人**可拿到隐私字段（QQ / 备注），其余只拿公开字段。"""
    cfg = store.snapshot()
    view = logic.member_view(
        cfg,
        member,
        bans=store.live_bans(),
        live_keys=live.ready_paths_snapshot() or set(),
        event_id=store.current_id,
    )
    if session.is_server or self_view:
        view.update(member.private())
    return view


# --------------------------------------------------------------------------- #
# 成员管理
# --------------------------------------------------------------------------- #
@router.get("/members")
async def api_members(session: Session = Depends(require_event)) -> dict[str, Any]:
    """成员列表。

    服务器管理员拿到含隐私字段的完整视图；赛事管理员 / 成员只能拿到公开字段
    （用于组队选人 / 展示），不含 QQ、备注与凭据状态之外的任何敏感信息。
    """
    members = store.members()
    return {
        "ok": True,
        "members": [_member_public(m, session) for m in members],
        "canManage": session.is_server,
        # 推流 ID / 流名重复（成员之间、以及成员与传统频道之间）：管理端据此高亮提示
        "duplicates": logic.duplicate_streams(members, store.channels()),
    }


@router.post("/members")
async def api_member_save(
    payload: MemberPayload, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """新增 / 更新成员（仅服务器管理员）。

    * 新建时自动生成 ``uid`` 与随机密钥 / Bearer 令牌，**明文只在本次响应里出现一次**；
    * 修改时不动密钥 / 令牌（要换请用 ``/api/members/{uid}/rotate``）；
    * 权限只能有一个服务器管理员：把别人设为 server_admin 会被拒绝。
    """
    permission = (payload.permission or "member").strip()
    if permission not in _PERMISSIONS:
        raise HTTPException(status_code=400, detail="权限只能是 member / event_admin / server_admin")
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="成员名称不能为空")

    existing = store.member(payload.uid) if payload.uid else None
    if permission == "server_admin":
        current_admin = store.server_admin()
        if current_admin is not None and (existing is None or current_admin.uid != existing.uid):
            raise HTTPException(
                status_code=400, detail="服务器管理员全局有且只有一个，不能把其它成员设为服务器管理员"
            )
    if (
        existing is not None
        and existing.permission == "server_admin"
        and permission != "server_admin"
    ):
        # 防止唯一的管理员把自己降级成普通成员，导致全站无人可管
        raise HTTPException(
            status_code=400, detail="服务器管理员有且只有一个，不能降级自己的权限"
        )
    if existing is not None and existing.permission == "server_admin" and not payload.active:
        # 同理：唯一的服务器管理员被停用后，全站就没有人能管理了
        raise HTTPException(status_code=400, detail="服务器管理员不能被停用（有且只有一个）")

    _ensure_stream_unique(payload.stream_id, payload.uid)

    member = Member(
        uid=payload.uid,
        name=payload.name,
        qq=payload.qq,
        avatar=payload.avatar,
        game_uuid=payload.game_uuid,
        stream_id=logic.clean_key(payload.stream_id),
        room_title=payload.room_title,
        note=payload.note,
        permission=permission,  # type: ignore[arg-type]
        active=payload.active,
    )
    saved, key_plain, bearer_plain = await store.save_member(member)
    # 权限变动或停用的成员，在线会话立即失效（否则停用后仍能用旧会话操作）
    if existing is not None and (
        existing.permission != saved.permission or (existing.active and not saved.active)
    ):
        auth.revoke_by_uid(saved.uid)
    # 同步到各届里关联的选手（选手就是成员）
    await store.propagate_member(saved, actor=f"web:member-save:{session.uid or 'server'}")
    view = _member_public(saved, session)
    return {
        "ok": True,
        "member": view,
        "created": existing is None,
        # 明文凭据：**仅此一次**（前端弹窗展示后即丢弃，刷新页面不再可见）
        "secretKey": key_plain,
        "bearerToken": bearer_plain,
    }


@router.delete("/members/{uid}")
async def api_member_delete(uid: str, session: Session = Depends(require_server)) -> dict[str, Any]:
    """删除成员（仅服务器管理员）。"""
    member = _member_or_404(uid)
    if member.permission == "server_admin":
        raise HTTPException(status_code=400, detail="不能删除服务器管理员（有且只有一个）")
    removed = await store.delete_member(uid, actor=f"web:member-delete:{session.uid or 'server'}")
    if not removed:
        raise HTTPException(status_code=404, detail=f"成员 {uid} 不存在")
    auth.revoke_by_uid(uid)
    return {"ok": True, "uid": uid}


@router.post("/members/{uid}/rotate")
async def api_member_rotate(
    uid: str, payload: RotatePayload, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """轮换成员的登录密钥 / Bearer 令牌（仅服务器管理员）。

    轮换后旧值立即失效；新明文**只在本次响应里出现一次**。
    轮换密钥会注销该成员的所有在线会话。
    """
    member = _member_or_404(uid)
    if not payload.key and not payload.bearer:
        raise HTTPException(status_code=400, detail="请指定要轮换的项目（key / bearer）")
    saved, key_plain, bearer_plain = await store.save_member(
        member.model_copy(), new_key=payload.key, new_bearer=payload.bearer
    )
    if key_plain:
        auth.revoke_by_uid(uid)
    return {
        "ok": True,
        "uid": saved.uid,
        "secretKey": key_plain,
        "bearerToken": bearer_plain,
    }


# --------------------------------------------------------------------------- #
# 成员自助（/api/me）
# --------------------------------------------------------------------------- #
@router.get("/me")
async def api_me(session: Session = Depends(require_admin)) -> dict[str, Any]:
    """当前登录者的身份与个人资料。

    服务器管理员用管理 KEY 登录时 ``member`` 为空（key 级会话），
    成员用密钥登录时回自己的成员视图（含封禁状态）。
    """
    member = store.member(session.uid) if session.uid else None
    return {
        "ok": True,
        "uid": session.uid,
        "name": session.name or (member.display_name if member else "服务器管理员"),
        "permission": session.permission,
        # 本人视角：包含自己的 QQ / 备注，便于在「我的」页编辑
        "member": _member_public(member, session, self_view=True) if member else None,
        "canManageEvents": session.can_manage_events,
        "isServer": session.is_server,
    }


@router.put("/me")
async def api_me_update(payload: MePayload, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """成员修改自己的资料（uid / 权限 / 凭据不可改，只能轮换）。"""
    if not session.uid:
        raise HTTPException(status_code=400, detail="当前为管理 KEY 登录，没有可编辑的成员资料")
    member = _member_or_404(session.uid)
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="成员名称不能为空")
    _ensure_stream_unique(payload.stream_id, member.uid)
    updated = member.model_copy(
        update={
            "name": payload.name,
            "qq": "".join(ch for ch in (payload.qq or "") if ch.isdigit()),
            "avatar": payload.avatar,
            "game_uuid": payload.game_uuid,
            "stream_id": logic.clean_key(payload.stream_id),
            "room_title": payload.room_title,
            "note": payload.note,
        }
    )
    saved, _key, _bearer = await store.save_member(updated)
    # 成员改了自己的资料：同步到各届里关联的选手
    await store.propagate_member(saved, actor=f"web:me-save:{session.uid}")
    return {"ok": True, "member": _member_public(saved, session, self_view=True)}


@router.post("/me/rotate")
async def api_me_rotate(payload: RotatePayload, session: Session = Depends(require_admin)) -> dict[str, Any]:
    """成员轮换自己的密钥 / 令牌（明文仅此一次；轮换密钥会注销本人会话）。"""
    if not session.uid:
        raise HTTPException(status_code=400, detail="当前为管理 KEY 登录，无法轮换成员凭据")
    member = _member_or_404(session.uid)
    if not payload.key and not payload.bearer:
        raise HTTPException(status_code=400, detail="请指定要轮换的项目（key / bearer）")
    saved, key_plain, bearer_plain = await store.save_member(
        member.model_copy(), new_key=payload.key, new_bearer=payload.bearer
    )
    if key_plain:
        # 密钥换了：本人的会话立即失效，必须用新密钥重新登录
        auth.revoke_by_uid(session.uid)
    return {
        "ok": True,
        "uid": saved.uid,
        "secretKey": key_plain,
        "bearerToken": bearer_plain,
        "reauth": bool(key_plain),
    }


# --------------------------------------------------------------------------- #
# 服务器级配置（自定义 HTML）
# --------------------------------------------------------------------------- #
@router.get("/server/config")
async def api_server_config(session: Session = Depends(require_server)) -> dict[str, Any]:
    """服务器级配置（仅服务器管理员）。"""
    cfg = store.snapshot()
    members = store.members()
    return {
        "ok": True,
        "customHtml": store.custom_html(),
        "members": len(members),
        "admins": sum(1 for m in members if m.permission == "event_admin"),
        "bans": len(store.live_bans()),
        "customHtmlKey": "custom_html",
        "eventId": store.current_id,
        "eventName": cfg.event.name or cfg.event.title,
    }


@router.put("/server/config")
async def api_server_config_update(
    payload: ServerConfigPayload, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """设置服务器级自定义 HTML（仅服务器管理员）。

    用于接入统计 / 数据采集脚本：会原样注入到页面里，因此只在受信任的管理端配置。
    """
    text = await store.set_custom_html(payload.custom_html, actor=f"web:server:{session.uid or 'server'}")
    return {"ok": True, "customHtml": text}


# --------------------------------------------------------------------------- #
# 登录失败限制（类 fail2ban）
# --------------------------------------------------------------------------- #
@router.get("/server/login-guard")
async def api_login_guard(
    request: Request, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """登录失败限制：配置 + 封禁 / 失败计数（仅服务器管理员）。

    ``clientIp`` 是本次请求解析出的客户端 IP——配好可信代理后，可在这里确认
    是否拿到了反向代理后面的真实 IP。
    """
    settings = store.guard_settings()
    return {
        "ok": True,
        "settings": settings,
        "status": login_guard.snapshot(int(settings.get("windowSeconds") or 300)),
        "clientIp": login_guard.client_ip(request, settings),
        "proxyConfigured": bool(str(settings.get("trustedProxies") or "").strip()),
    }


@router.put("/server/login-guard")
async def api_login_guard_update(
    payload: LoginGuardPayload, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """更新登录限制配置（仅服务器管理员）。"""
    patch = payload.model_dump(by_alias=True, exclude_none=True)
    if "maxAttempts" in patch and not 1 <= int(patch["maxAttempts"]) <= 1000:
        raise HTTPException(status_code=400, detail="失败次数阈值需在 1~1000 之间")
    if "windowSeconds" in patch and not 10 <= int(patch["windowSeconds"]) <= 86400:
        raise HTTPException(status_code=400, detail="统计时间窗需在 10~86400 秒之间")
    if "banSeconds" in patch and not 0 <= int(patch["banSeconds"]) <= 2592000:
        raise HTTPException(status_code=400, detail="封禁时长需在 0~2592000 秒之间（0 = 只计数不封禁）")
    try:
        settings = await store.set_login_guard(
            patch, actor=f"web:login-guard:{session.uid or 'server'}"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "settings": settings}


@router.delete("/server/login-guard/{ip}")
async def api_login_guard_unban(
    ip: str, session: Session = Depends(require_server)
) -> dict[str, Any]:
    """解除某个 IP 的登录封禁（仅服务器管理员）。"""
    return {"ok": True, "ip": ip, "removed": login_guard.unban(ip)}


@router.delete("/server/login-guard")
async def api_login_guard_clear(session: Session = Depends(require_server)) -> dict[str, Any]:
    """清空全部登录封禁与失败计数（仅服务器管理员）。"""
    return {"ok": True, "cleared": login_guard.clear()}


# --------------------------------------------------------------------------- #
# 直播封禁 / 强制掐断
# --------------------------------------------------------------------------- #
def _resolve_ban_target(payload: LiveBanPayload) -> Member:
    member = None
    if payload.member_uid:
        member = store.member(payload.member_uid)
    if member is None and payload.stream_id:
        member = store.member_by_stream_id(logic.clean_key(payload.stream_id))
    if member is None:
        raise HTTPException(status_code=404, detail="找不到对应的成员（请提供 memberUid 或 streamId）")
    if not member.stream_id:
        raise HTTPException(status_code=400, detail=f"成员 {member.display_name} 还没有推流 ID")
    return member


@router.get("/live/bans")
async def api_live_bans(session: Session = Depends(require_event)) -> dict[str, Any]:
    """全部直播封禁记录（含已过期的，前端据此区分历史与生效中）。"""
    bans = [logic.ban_view(b) for b in store.live_bans()]
    for view, ban in zip(bans, store.live_bans()):
        view["active"] = logic.ban_active(ban)
    return {"ok": True, "bans": bans, "isServer": session.is_server}


@router.post("/live/bans")
async def api_live_ban_create(
    payload: LiveBanPayload, session: Session = Depends(require_event)
) -> dict[str, Any]:
    """封禁某个直播间（并可同时强制掐断）。

    * 服务器管理员：可签发**全局封禁**（``scope=global``，任何届都生效）；
      也可指定 ``scope=event`` 配合 ``eventId`` 做单届封禁；
    * 赛事管理员：只能对自己那届（当前届、且自己创建）的在赛直播间签发
      ``scope=event`` 封禁。
    """
    member = _resolve_ban_target(payload)
    event_id = store.current_id

    if session.is_server:
        scope = (payload.scope or "global").strip() or "global"
        if scope not in ("global", "event"):
            raise HTTPException(status_code=400, detail="scope 只能是 global 或 event")
    else:
        # 赛事管理员：必须是自己创建的当前届，且目标成员在本届参赛
        require_event_owned(session, current_event_owner())
        scope = "event"
        if not any(p.member_uid == member.uid for p in store.snapshot().players):
            raise HTTPException(status_code=403, detail="只能封禁本届参赛选手的直播间")

    # 时长：直到 指定时间 > N 分钟 > 永久
    until = ""
    if (payload.until or "").strip():
        until = logic.check_time(payload.until, "解禁时间")
    elif payload.minutes and payload.minutes > 0:
        until = (datetime.now() + timedelta(minutes=int(payload.minutes))).replace(  # noqa: DTZ005
            microsecond=0
        ).isoformat()
    elif not payload.permanent:
        # 只掐断、不写封禁记录
        until = "__kick__"

    kick_result: dict[str, Any] = {"ok": False, "kicked": 0, "reason": ""}
    banned: dict[str, Any] | None = None
    if until == "__kick__":
        if payload.kick:
            kick_result = await live.kick_stream(member.stream_id)
        return {"ok": True, "banned": None, "kick": kick_result}

    ban = LiveBan(
        scope=scope,  # type: ignore[arg-type]
        member_uid=member.uid,
        stream_id=member.stream_id,
        name=member.display_name,
        reason=(payload.reason or "").strip(),
        until=until,
        event_id=event_id if scope == "event" else "",
        created_at=now_iso(),
        created_by=session.name or ("服务器管理员" if session.is_server else "赛事管理员"),
    )
    saved = await store.add_live_ban(ban, actor=f"web:ban:{session.uid or 'server'}")
    banned = logic.ban_view(saved)
    banned["active"] = True
    if payload.kick:
        kick_result = await live.kick_stream(member.stream_id)
    return {"ok": True, "banned": banned, "kick": kick_result}


@router.delete("/live/bans/{ban_id}")
async def api_live_ban_delete(ban_id: str, session: Session = Depends(require_server)) -> dict[str, Any]:
    """解除封禁（**仅服务器管理员**；解除后立即可再次推流）。"""
    removed = await store.remove_live_ban(ban_id, actor=f"web:unban:{session.uid or 'server'}")
    if not removed:
        raise HTTPException(status_code=404, detail=f"封禁记录 {ban_id} 不存在")
    return {"ok": True, "banId": ban_id}


# --------------------------------------------------------------------------- #
# 头像（成员）
# --------------------------------------------------------------------------- #
@router.get("/avatar/m/{uid}")
async def api_avatar_member(
    uid: str,
    size: int = Query(default=100),
    refresh: bool = Query(default=False),
) -> Response:
    """按**成员 uid** 取头像（与选手 / 频道同一套代理，客户端看不到 QQ）。"""
    member = store.member(uid)
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if not member.qq:
        raise HTTPException(status_code=404, detail="该成员未配置 QQ 头像")
    if not avatars.is_valid_qq(member.qq):
        raise HTTPException(status_code=400, detail="成员的 QQ 号格式不正确")
    body, mime, source = await avatars.get_avatar(member.qq, size, member.name, refresh=refresh)
    return Response(
        content=body,
        media_type=mime,
        headers={
            "Cache-Control": (
                "no-store" if refresh else "public, max-age=3600, stale-while-revalidate=86400"
            ),
            "X-NTE-Avatar": source,
        },
    )
