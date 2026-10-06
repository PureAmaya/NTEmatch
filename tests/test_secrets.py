"""敏感字段不外泄：匿名能拿到的响应里，不该出现任何凭据。

这类漏洞的表现是「一切都正常，只是谁都能看到密码」，所以两条都要钉住：

1. **白名单**比黑名单安全：对外结构只放行明确列出的字段（``public_stream_config``），
   以后给配置模型加字段不会「自动跟着漏出去」；
2. 真把令牌写进配置，再去搜匿名接口的**原始响应文本**——不是「看着像不会漏」。

第二条是关键：mypy 不会告诉你某个字段被顺手加进了公开状态，
只有拿真实字符串去搜才查得出来。
"""

from __future__ import annotations

import httpx

from app import db, live, logic
from app.auth import hash_secret
from app.main import app
from app.models import StreamConfig
from app.store import store

PUSH_TOKEN = "push-token-SECRET-a1b2c3d4"
API_USER = "mediamtx-user-SECRET-a1b2c3d4"
API_PASS = "mediamtx-pass-SECRET-a1b2c3d4"
FALLBACK_KEY = "fallback-stream-SECRET-a1b2c3d4"
BOT_TOKEN = "nte_bot_SECRET_a1b2c3d4"

#: 匿名就能读的接口（含首页 HTML 与版权清单）
ANONYMOUS_PATHS = (
    "/api/state",
    "/api/events",
    "/api/live/info",
    "/api/live/health",
    "/api/health",
    "/api/credits",
    "/",
)


def test_public_stream_config_is_a_whitelist():
    """直播配置对外只放行白名单字段——凭据即使有值也不会跟出去。"""
    stream = StreamConfig(
        push_token=PUSH_TOKEN,
        api_user=API_USER,
        api_pass=API_PASS,
        stream_key=FALLBACK_KEY,
    )
    public = logic.public_stream_config(stream)
    for field in ("pushToken", "apiPass", "apiUser", "apiBase", "hlsBase"):
        assert field not in public, f"{field} 不该出现在对外直播配置里"
    # `baseUrl`（WebRTC 根地址）是**有意**公开的：观众要靠它拼出观看地址
    assert public.get("baseUrl") is not None
    # 直播没有总开关了（只要有赛事就允许直播），这个键不该再出现在对外配置里
    assert "enabled" not in public


def test_main_room_stream_key_is_public_by_design():
    """主直播间的流名是公开的——**观看地址里本来就有它**，所以它不算凭据。

    写下来是为了避免「以后有人看到 `key` 就当成泄露顺手删掉」：
    删了前端就拼不出播放地址。真正必须保密的是推送令牌与 MediaMTX 账密
    （见下一个用例）。顺带提醒：未设推流令牌时，光有流名就能推（README 已写明），
    公网部署应当设一个令牌。
    """
    view = live.stream_endpoints()
    assert view["key"], "主直播间流名不该被剥掉（前端要用它拼观看地址）"
    assert not any(token in str(view) for token in (PUSH_TOKEN, API_PASS, API_USER))


async def test_anonymous_responses_never_leak_credentials():
    """把凭据真的写进配置，再逐条搜匿名响应：一个都不许出现。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    before = store.snapshot().stream.dump()
    try:
        await store.update(
            {
                "stream": {
                    "pushToken": PUSH_TOKEN,
                    "apiUser": API_USER,
                    "apiPass": API_PASS,
                }
            },
            actor="test",
        )
        await store.set_qqbot(
            {"botApiTokenHash": hash_secret(BOT_TOKEN)}, actor="test", internal=True
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
            for path in ANONYMOUS_PATHS:
                text = (await c.get(path)).text
                for label, value in (
                    ("推流令牌", PUSH_TOKEN),
                    ("MediaMTX 密码", API_PASS),
                    ("MediaMTX 用户名", API_USER),
                    ("机器人令牌", BOT_TOKEN),
                ):
                    assert value not in text, f"{path} 里泄露了{label}"
    finally:
        await store.update({"stream": before}, actor="test")
        await store.set_qqbot({"botApiTokenHash": ""}, actor="test", internal=True)
