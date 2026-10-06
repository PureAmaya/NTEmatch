"""图片推送：发图、退回纯文本、以及「有图时正文只留一行」。

这一组盯的是**推送这条链路本身**（以前没有用例覆盖 ``/api/qqbot/push``），
而图片是这条链路上最脆的一环：AstrBot 各版本对图片段的字段名不一样、
没装 Pillow 时压根画不出图——两种情况下群里的消息都**不能少东西**。

所以两条底线：

* 图发得出去 → 图 + 一行说明（信息与规则都在图里，不必再刷一屏文字）；
* 图发不出去 → **完整文本**（连比赛规则摘要一起），只是排版朴素一点。
"""

from __future__ import annotations

import pytest

from app import db, qqbot
from app.auth import hash_secret
from app.main import app
from app.store import store

BOT_TOKEN = "nte_test_bot_token"


def _image_settings() -> dict:
    return {
        "enabled": True,
        "baseUrl": "http://astrbot.test",
        "apiKey": "abk_test",
        "umo": "aiocqhttp:GroupMessage:123456",
        "path": "/api/v1/im/message",
        "timeout": 5,
    }


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self) -> dict:
        return {"detail": "bad shape"}

    @property
    def text(self) -> str:
        return ""


class _FakeClient:
    """假的 ``httpx.AsyncClient``：记下每个请求体，按预设状态码序列回应。"""

    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.bodies: list[dict] = []

    async def post(self, url, headers=None, json=None):
        self.bodies.append(json or {})
        status = self.statuses.pop(0) if self.statuses else 200
        return _FakeResponse(status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_httpx(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(qqbot.httpx, "AsyncClient", lambda **kwargs: client)


# --------------------------------------------------------------------------- #
# send_image：字段名逐个试
# --------------------------------------------------------------------------- #
async def test_send_image_tries_each_field_name(monkeypatch):
    """第一个字段名被拒（400 = 格式不对）就换下一个——AstrBot 各版本叫法不一。"""
    client = _FakeClient([400, 200])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image("https://nte.test/api/cards/abc.png", settings=_image_settings())
    assert result["ok"] is True
    assert result["shape"] == "url", "第二个候选是 url"
    assert len(client.bodies) == 2
    assert client.bodies[0]["message"][0]["file"] == "https://nte.test/api/cards/abc.png"
    assert client.bodies[1]["message"][0]["url"] == "https://nte.test/api/cards/abc.png"
    assert client.bodies[0]["umo"] == "aiocqhttp:GroupMessage:123456"


async def test_send_image_gives_up_on_a_real_error(monkeypatch):
    """497 / 500 这类不是「字段名不对」，别把三种形态都盲试一遍。"""
    client = _FakeClient([500])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image("https://nte.test/x.png", settings=_image_settings())
    assert result["ok"] is False
    assert len(client.bodies) == 1
    assert "HTTP 500" in result["detail"]


@pytest.mark.parametrize(
    ("patch", "expect"),
    [
        ({"enabled": False}, "未启用"),
        ({"apiKey": ""}, "API Key"),
        ({"baseUrl": ""}, "AstrBot 地址"),
        ({"umo": ""}, "目标会话"),
    ],
)
async def test_send_image_preconditions(monkeypatch, patch, expect):
    """四道前置检查各自给出人话（与 send_text 同一套顺序）。"""
    client = _FakeClient([200])
    _fake_httpx(monkeypatch, client)
    settings = {**_image_settings(), **patch}
    if patch.get("umo") == "":
        settings["umo"] = ""
    result = await qqbot.send_image("https://nte.test/x.png", settings=settings)
    assert result["ok"] is False
    assert expect in result["detail"]
    assert client.bodies == [], "前置检查没过就不该发请求"


async def test_send_image_refuses_empty_url():
    result = await qqbot.send_image("", settings=_image_settings())
    assert result["ok"] is False and "图片地址" in result["detail"]


# --------------------------------------------------------------------------- #
# card_parts：有图时正文只留一行
# --------------------------------------------------------------------------- #
def test_card_parts_keeps_detail_text_but_trims_event():
    """``比赛信息`` 的文字全在卡片里了，只留一行；``详情`` 还有进度 / 结果，必须保留。"""
    parts = ["很长的比赛信息正文", "第二段"]
    card = {"caption": "【秋季赛】10 月 5 日 · 完整赛制与规则见图"}
    assert qqbot.card_parts("event", card, parts) == [card["caption"]]
    assert qqbot.card_parts("detail", card, parts) == parts
    assert qqbot.card_parts("event", None, parts) == parts
    assert qqbot.card_parts("event", {"caption": ""}, parts) == parts


# --------------------------------------------------------------------------- #
# 接口：推送与预览
# --------------------------------------------------------------------------- #
@pytest.fixture
async def bot_ready():
    """配好推送（启用 / Key / 目标会话）并保证有一届赛事。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    await store.set_qqbot(
        {
            "botApiTokenHash": hash_secret(BOT_TOKEN),
            "enabled": True,
            "baseUrl": "http://astrbot.test",
            "apiKey": "abk_test",
            "umo": "123456",
        },
        actor="test",
        internal=True,
    )
    yield
    await store.set_qqbot({"enabled": False}, actor="test", internal=True)


@pytest.fixture
def pushed(monkeypatch):
    """把「真的发消息」换成记录器，并让限流永远放行（测的是内容，不是额度）。"""
    calls: dict[str, list] = {"text": [], "image": []}

    async def fake_text(text, *, settings=None, umo=""):
        calls["text"].append(text)
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    async def fake_image(url, *, settings=None, umo=""):
        calls["image"].append(url)
        # 与真 send_image 同一份契约：成功时回报「用了哪个字段名」
        return {"ok": True, "status": 200, "detail": "", "umo": umo, "shape": "url"}

    async def allow(settings=None):
        return True, "", 0

    monkeypatch.setattr(qqbot, "send_text", fake_text)
    monkeypatch.setattr(qqbot, "send_image", fake_image)
    monkeypatch.setattr(qqbot.limiter, "acquire", allow)
    return calls


async def test_push_event_sends_the_card_then_one_line(admin_client, bot_ready, pushed):
    """比赛信息推送：先发卡片图，正文只留一行说明。"""
    res = await admin_client.post("/api/qqbot/push", json={"kind": "event"})
    assert res.status_code == 200, res.text
    assert res.json()["image"] is True
    assert len(pushed["image"]) == 1
    assert "/api/cards/" in pushed["image"][0] and pushed["image"][0].startswith("http")
    assert len(pushed["text"]) == 1, "有图时不该再补一屏文字"
    assert "完整赛制与规则见图" in pushed["text"][0]


async def test_push_event_falls_back_to_the_full_text(admin_client, bot_ready, pushed, monkeypatch):
    """图发不出去：**完整文本**顶上（连比赛规则摘要一起），信息一条不少。"""

    async def fail(url, *, settings=None, umo=""):
        return {"ok": False, "status": 415, "detail": "no image support", "umo": umo}

    monkeypatch.setattr(qqbot, "send_image", fail)
    res = await admin_client.post("/api/qqbot/push", json={"kind": "event"})
    assert res.status_code == 200, res.text
    assert res.json()["image"] is False
    text = "\n".join(pushed["text"])
    assert "赛制" in text and "参赛" in text
    assert "比赛规则" in text, "退回文本时也必须带规则（这正是这次改动的目的）"


async def test_push_progress_never_generates_a_card(admin_client, bot_ready, pushed):
    """进度 / 结果这类逐场次的东西不画图（画成图反而看不快）。"""
    res = await admin_client.post("/api/qqbot/push", json={"kind": "progress"})
    assert res.status_code == 200, res.text
    assert res.json()["image"] is False
    assert pushed["image"] == []


async def test_preview_shows_the_card_and_both_texts(admin_client, bot_ready, pushed):
    """预览要把图和「图发出去时配的那几行」都给出来，管理员先看再决定。"""
    res = await admin_client.get("/api/qqbot/preview", params={"kind": "event"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["card"] and data["card"]["url"].startswith("http://nte.test/api/cards/")
    assert data["imageAvailable"] is True
    assert data["parts"] and data["cardText"] == [data["card"]["caption"]]
    assert pushed == {"text": [], "image": []}, "预览绝不能真的发出去"


async def test_image_switch_off_skips_the_card(admin_client, bot_ready, pushed):
    """关掉「图片推送」：连图都不画，直接发完整文字（信息一条不少）。"""
    await store.set_qqbot({"imageCards": False}, actor="test", internal=True)
    preview = (await admin_client.get("/api/qqbot/preview", params={"kind": "event"})).json()
    assert preview["card"] is None
    assert preview["cardText"] == preview["parts"], "没图时「要发的那几段」就是完整文本"

    res = await admin_client.post("/api/qqbot/push", json={"kind": "event"})
    assert res.status_code == 200, res.text
    assert res.json()["image"] is False
    assert pushed["image"] == [], "开关关着时不该尝试发图"
    assert any("比赛规则" in part for part in pushed["text"])


async def test_test_endpoint_can_send_an_image(admin_client, bot_ready, pushed):
    """「测试发图」：验证图片通道，并把实际生效的字段名回报给管理员。"""
    res = await admin_client.post("/api/qqbot/test", json={"image": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["image"] is True
    assert body["shape"], "要回报图片段用的是哪个字段名（各版本不一样）"
    assert pushed["image"] and "/api/cards/" in pushed["image"][0]


async def test_test_endpoint_says_why_when_it_cannot_draw(admin_client, bot_ready, pushed):
    """画不出卡片（没装 Pillow）时说清原因，而不是回一句「发送失败」。"""
    from app import card

    async def no_card(*args, **kwargs):
        return None

    original = card.card_for_event
    card.card_for_event = no_card
    try:
        res = await admin_client.post("/api/qqbot/test", json={"image": True})
    finally:
        card.card_for_event = original
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is False
    assert "Pillow" in res.json()["detail"]


async def test_bot_query_event_returns_the_card_for_the_plugin(bot_ready, pushed):
    """群命令那条路：``/api/bot/query`` 也要带上卡片地址，插件才发得出图。"""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    ) as c:
        event = (await c.get("/api/bot/query", params={"kind": "event"})).json()
        progress = (await c.get("/api/bot/query", params={"kind": "progress"})).json()

    assert event["card"] and event["card"]["url"].startswith("http://nte.test/api/cards/")
    assert event["parts"] == [event["card"]["caption"]]
    assert progress.get("card") is None
    assert progress["parts"], "没有卡片的类型照旧给文本"
