"""权限面自检：每个写接口都必须带一道**已知**的权限闸门。

这类问题的特点很讨厌：**不报错、不崩**，只是安静地多出一个谁都能调的写接口。
所以这里把清单钉成测试——将来加路由忘了加权限，CI 直接红。

顺带把「哪些接口必须只给服务器管理员」「哪些读取接口不能公开」也钉住，
避免为了图方便把闸门从 `require_server` 降级成 `require_event`。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.main import _apply_stream_patch, _apply_ui_patch, app

# 允许的闸门（值 = 依赖函数名）
GUARDS = {
    "require_server",  # 仅服务器管理员
    "require_event",  # 赛事管理员 / 服务器管理员
    "require_current_event",  # 同上 + 当前届归属校验
    "require_admin",  # 任何已登录（/api/me 这类「只改自己」的接口）
    "require_bot_token",  # 只读机器人令牌
}

# 必须公开的写接口：登录本身、本机免登录、注销、媒体服务器鉴权回调
PUBLIC_WRITES = {
    ("POST", "/api/auth"),
    ("POST", "/api/auth/local"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/live/auth"),
    # 已退休的主管理 KEY 接口：不做任何事，只回 410 告诉老客户端「改用成员密钥」。
    # 因此它**有意**不带闸门（不鉴权就更不需要权限），但必须钉在这里，
    # 免得它哪天悄悄长成一个真的能改凭据的接口。
    ("POST", "/api/admin/key"),
}

# 只允许服务器管理员：这些一旦降级就是越权（跨届改全局资源 / 碰别人账号）
SERVER_ONLY_WRITES = {
    ("PUT", "/api/site/name"),
    ("PUT", "/api/server/config"),
    ("PUT", "/api/server/login-guard"),
    ("DELETE", "/api/server/login-guard"),
    ("DELETE", "/api/server/login-guard/{ip}"),
    ("POST", "/api/members"),
    ("DELETE", "/api/members/{uid}"),
    ("POST", "/api/members/{uid}/rotate"),
    ("POST", "/api/backups"),
    ("PUT", "/api/backups/settings"),
    ("POST", "/api/backups/{name}/restore"),
    ("POST", "/api/backups/upload"),
    ("DELETE", "/api/backups/{name}"),
    ("PUT", "/api/qqbot"),
    ("POST", "/api/qqbot/bot-token"),
    ("DELETE", "/api/qqbot/bot-token"),
    ("POST", "/api/qqbot/test"),
    # 频道与公告是**全局**资源（跨届共享）：赛事管理员只管自己那届，不该动它们
    ("POST", "/api/channels"),
    ("PUT", "/api/channels/notice"),
    ("DELETE", "/api/channels/{channel_id}"),
    # 站点级信息（「关于本站」）同样是全局的：只有服务器管理员能改
    ("PUT", "/api/server/info"),
}

# 含隐私 / 凭据的读取接口：绝不能是公开的
SENSITIVE_READS = {
    "/api/private",  # 选手 UUID / QQ / 推流地址
    "/api/export",  # 全量导出
    "/api/server/config",  # 自定义 HTML 等服务器配置
    "/api/server/login-guard",  # 封禁名单
    "/api/me",  # 自己的资料（含 QQ）
    "/api/qqbot",
    "/api/activity",
    "/api/diagnostics",
    "/api/media",  # 上传占用统计（含文件数 / 体积）
    # 机器人接口里的「认人」与「我的地址」：按 QQ 换身份 / 换推流地址，
    # 令牌本身就是凭据，绝不能裸奔
    "/api/bot/whoami",
    "/api/bot/my-links",
}

# 有意公开的读取接口（写下来是为了让「不小心收紧」也能被发现）
PUBLIC_READS = {
    "/api/state",
    "/api/events",
    "/api/health",
    "/api/live/health",
    "/api/live/info",
    "/og.png",
    # 公告与站点/赛事信息是给所有人看的（渲染在服务端，已做严格白名单过滤）
    "/api/notices",
    "/api/notices/{notice_id}",
    "/api/server/info",
    "/api/event/info",
    "/api/media/{name}",  # 公告图片本体（文件名即内容哈希）
    "/api/credits",  # 版权与开源组件清单
}

# 只读但**必须登录**的写入口（Markdown 渲染 / 图片上传：不给匿名当免费渲染服务 / 图床）
LOGGED_IN_WRITES = {
    ("POST", "/api/md/preview"),
    ("POST", "/api/media"),
    ("POST", "/api/event/info/preview"),
}


def _dep_names(dependant) -> set[str]:
    """依赖树里的全部依赖函数名（含嵌套依赖）。"""
    names: set[str] = set()
    for dep in getattr(dependant, "dependencies", None) or []:
        names.add(getattr(dep.call, "__name__", ""))
        names |= _dep_names(dep)
    return names


def _collect() -> list[tuple[tuple[str, str], set[str]]]:
    """全部路由 → ``((方法, 路径), 依赖名集合)``。

    ``include_router`` 在这个 FastAPI 版本里是**嵌套路由对象**（``_IncludedRouter``），
    不会摊平到 ``app.routes`` 里，所以要递归进 ``effective_candidates()`` 才看得到。
    """
    found: list[tuple[tuple[str, str], set[str]]] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if type(route).__name__ == "_IncludedRouter":
            stack.extend(route.effective_candidates())
            continue
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        names = _dep_names(getattr(route, "dependant", None))
        for method in methods:
            found.append(((method, getattr(route, "path", "")), names))
    return found


ROUTES = _collect()


def test_route_collection_is_complete():
    """守卫测试本身别失效：至少要有几十条路由，而且确实能看到 router 里的接口。"""
    assert len(ROUTES) > 60
    paths = {path for _key, _names in ROUTES for path in [_key[1]]}
    assert "/api/members" in paths, "没递归进 router，等于什么都没检查"
    assert "/api/bot/manifest" in paths


def test_every_write_route_has_a_guard():
    """写接口要么带已知闸门，要么在「有意公开」清单里。"""
    unguarded = []
    for (method, path), names in ROUTES:
        if method in ("GET", "HEAD", "OPTIONS"):
            continue
        if (method, path) in PUBLIC_WRITES:
            continue
        if not (names & GUARDS):
            unguarded.append(f"{method} {path}")
    assert not unguarded, "以下写接口没有任何权限闸门：\n" + "\n".join(sorted(unguarded))


def test_server_only_writes_stay_server_only():
    """必须只给服务器管理员的写接口，不许降级。"""
    by_key = {key: names for key, names in ROUTES}
    wrong = []
    for key in sorted(SERVER_ONLY_WRITES):
        names = by_key.get(key)
        assert names is not None, f"路由不见了：{key[0]} {key[1]}（改名了？请同步本测试）"
        if "require_server" not in names:
            wrong.append(f"{key[0]} {key[1]} -> {sorted(names) or '无权限'}")
    assert not wrong, "以下接口不再是「仅服务器管理员」：\n" + "\n".join(wrong)


def test_sensitive_reads_are_not_public():
    """含隐私 / 凭据的读取接口不能裸奔。"""
    exposed = []
    for (method, path), names in ROUTES:
        if method != "GET" or path not in SENSITIVE_READS:
            continue
        if not (names & GUARDS):
            exposed.append(path)
    missing = SENSITIVE_READS - {path for (_m, path), _n in ROUTES}
    assert not missing, f"这些接口路径变了：{sorted(missing)}"
    assert not exposed, f"以下读取接口没有任何鉴权：{sorted(exposed)}"


def test_public_reads_stay_public():
    """有意公开的读取接口（首页数据、健康检查）不该被顺手收紧。"""
    public = {path for (method, path), names in ROUTES if method == "GET" and not (names & GUARDS)}
    assert PUBLIC_READS <= public, f"这些本应公开：{sorted(PUBLIC_READS - public)}"


def test_media_server_settings_are_server_only():
    """``/api/config`` 里的**直播配置整块**只有服务器管理员能改。

    这条闸门是**按补丁内容**判的（同一个接口还要给赛事管理员改赛制与文案），静态列表
    看不出来，所以在这里单钉一条。

    注意这里**没有例外键**：直播早年有个「启用直播」的开关（当时赛事管理员能拨），
    那个开关已经整体移除（只要有赛事就允许直播），于是赛事管理员这边一个可改的
    直播字段都不剩——漏掉任何一个键都等于给了一条改站点级配置的旁路。
    """
    event_admin = SimpleNamespace(is_server=False)
    for key in (
        "provider",
        "baseUrl",
        "apiBase",
        "apiUser",
        "apiPass",
        "apiPassClear",
        "hlsBase",
        "streamKey",
        "pushToken",
        "mode",
        "verifyTls",
        "whipPush",
        "poster",
        "title",
        "note",
        "enabled",
    ):
        with pytest.raises(HTTPException) as err:
            _apply_stream_patch({key: "x"}, event_admin)
        assert err.value.status_code == 403, f"{key} 没被拦住"
    # 服务器管理员放行
    _apply_stream_patch({"title": "场馆直播"}, SimpleNamespace(is_server=True))


def test_ui_config_is_server_only():
    """界面配置（主题色 / 分享图 / 展示开关）是**站点级外观**：赛事管理员碰它一律 403。

    和直播配置同一条思路：表单从赛事管理页搬走了，但真正把关的是这里——
    前端藏起来不算数。
    """
    event_admin = SimpleNamespace(is_server=False)
    with pytest.raises(HTTPException) as err:
        _apply_ui_patch({"accent": "lime"}, event_admin)
    assert err.value.status_code == 403
    _apply_ui_patch({"accent": "lime"}, SimpleNamespace(is_server=True))  # 服务器管理员放行


def test_markdown_media_writes_need_login_only():
    """渲染 / 上传要求登录，但不该收紧成「仅服务器管理员」——赛事管理员要发通知。

    这两处是「登录即可」的典型：不拦会给匿名当免费渲染服务 / 图床，
    拦太死则等于把发通知的功能锁进服务器管理员手里。"""
    by_key = {key: names for key, names in ROUTES}
    problems = []
    for key in sorted(LOGGED_IN_WRITES):
        names = by_key.get(key)
        assert names is not None, f"路由不见了：{key[0]} {key[1]}（改名了？请同步本测试）"
        if "require_event" not in names:
            problems.append(f"{key[0]} {key[1]} -> {sorted(names) or '无权限'}")
    assert not problems, "以下接口的闸门变了：\n" + "\n".join(problems)
