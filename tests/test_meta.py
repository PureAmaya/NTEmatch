"""服务端注入的头部：分享卡片（标题 / og）与**首页的缓存策略**。

**为什么值得单独钉**：前端也会改 ``document.title``，所以浏览器里看着一切正常；
但分享到 QQ / 微信时抓的是**服务端那份原始 HTML**——标题不对只有别人贴链接时才发现。

这里同时也是两条回归测试：

* 「独立页分支顺序」：独立页的段名（events / admin / user / channels / developer）
  长得和届次 ID 一样，一旦把届次判定放在前面，下面那张表就永远轮不到，所有独立页
  都会退回首页文案（这个顺序 bug 修过一次）；
* 「首页不许进缓存」：静态资源版本号是**注入在 HTML 里**的，而带版号的 CSS / JS 是
  immutable 一年。HTML 一旦被 CDN / 反代留住，就会把旧样式钉死一整年，
  改完 CSS 刷新还是旧样子（本地强刷也救不了，它绕不过 CDN）。
"""

from __future__ import annotations

import re

import httpx

from app import db
from app.main import app
from app.store import store

OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
#: 独立页 → 分享卡片标题的前缀
STANDALONE = (
    ("/developer", "开发者"),
    ("/events", "全部赛事"),
    ("/admin", "服务器管理"),
    ("/user", "我的"),
    ("/channels", "频道"),
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    )


def _title(body: str) -> str:
    found = OG_TITLE.search(body)
    return found.group(1) if found else ""


async def test_standalone_pages_have_their_own_share_card():
    """独立页各有各的标题，而不是清一色首页那句。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    async with _client() as c:
        for path, label in STANDALONE:
            body = (await c.get(path)).text
            title = _title(body)
            assert title.startswith(f"{label} | "), f"{path} 的分享标题不对：{title!r}"


async def test_event_page_share_card_uses_the_event_name():
    """届次页仍然用「届名 | 站点名」——顺序调整别把届次那条路挤掉。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    name = store.snapshot().event.name or store.current_id
    async with _client() as c:
        body = (await c.get(f"/{store.current_id}")).text
    assert _title(body).startswith(f"{name} | "), f"届次页分享标题不对：{_title(body)!r}"


async def test_home_share_card_is_the_site_itself():
    """首页保持默认那句（站名 + 总入口说明），别被上面的分支顺手改掉。"""
    db.init_db(store._db_path)
    site = store.site_name()
    async with _client() as c:
        body = (await c.get("/")).text
    assert _title(body) == site, f"首页分享标题不对：{_title(body)!r}"


async def test_html_is_never_cacheable():
    """首页**必须**明确 no-store：它带着资源版本号，缓存住等于钉死旧 CSS。"""
    db.init_db(store._db_path)
    async with _client() as c:
        res = await c.get("/")
        static = await c.get("/static/css/nte.css")
    cache = res.headers.get("cache-control", "")
    assert "no-store" in cache, f"首页的 Cache-Control 不对：{cache!r}"
    assert "max-age=0" in cache and "must-revalidate" in cache, f"首页的 Cache-Control 不对：{cache!r}"
    assert res.headers.get("pragma") == "no-cache"
    # 未带版本号的静态路径：可以 304，但不许直接用旧的（否则改完样式看不到）
    assert static.headers.get("cache-control") == "no-cache"
