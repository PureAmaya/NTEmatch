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
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"detail": "bad shape"}

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        import json

        return json.dumps(self._payload, ensure_ascii=False)


class _FakeClient:
    """假的 ``httpx.AsyncClient``：按预设状态码 / 响应体序列回应，并记下请求。

    ``bodies`` 只记 JSON 请求（发消息），``uploads`` 只记 multipart（上传附件）——
    两条路分开放，断言时一眼能看出走的是哪条。
    """

    def __init__(self, statuses: list[int], payloads: list[dict | None] | None = None):
        self.statuses = list(statuses)
        self.payloads = list(payloads or [])
        self.bodies: list[dict] = []
        self.uploads: list[dict] = []
        self.urls: list[str] = []

    async def post(self, url, headers=None, json=None, files=None):
        self.urls.append(url)
        if files is not None:
            self.uploads.append(files)
        else:
            self.bodies.append(json or {})
        status = self.statuses.pop(0) if self.statuses else 200
        payload = self.payloads.pop(0) if self.payloads else None
        return _FakeResponse(status, payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_httpx(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(qqbot.httpx, "AsyncClient", lambda **kwargs: client)


@pytest.fixture(autouse=True)
def _forget_shape_memory():
    """形态记忆是**进程内**的（图片段 / at 段各一份），用例之间必须隔离，
    否则请求顺序会跟着上一条变。"""
    qqbot._IMAGE_SHAPE_HIT = None
    qqbot._remember_at_mode("")
    yield
    qqbot._IMAGE_SHAPE_HIT = None
    qqbot._remember_at_mode("")


# --------------------------------------------------------------------------- #
# 真 @：消息段（AstrBot 有的版本收、有的不收，只能试出来）
# --------------------------------------------------------------------------- #
async def test_send_at_parts_tries_each_shape(monkeypatch):
    """两种段写法逐个试：平铺的 ``qq`` 被拒（400）就换 OneBot 风格的 ``data.qq``。"""
    client = _FakeClient([400, 200])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_at_parts("正文", mentions=["10001"], settings=_image_settings())
    assert result["ok"] is True, result["detail"]
    assert result["shape"] == "data"
    assert client.bodies[0]["message"][0] == {"type": "at", "qq": "10001"}
    assert client.bodies[1]["message"][0] == {"type": "at", "data": {"qq": "10001"}}
    assert client.bodies[1]["message"][-1] == {"type": "plain", "text": "正文"}
    assert qqbot.at_mode() == "data", "试出来的写法要记住，下次直接用"


async def test_send_at_parts_remembers_that_at_is_unsupported(monkeypatch):
    """两种写法都被拒 → 记住「这台 AstrBot 不收 at 段」，之后一个请求都不发。"""
    client = _FakeClient([400, 400])
    _fake_httpx(monkeypatch, client)
    first = await qqbot.send_at_parts("正文", mentions=["10001"], settings=_image_settings())
    assert first["ok"] is False
    assert qqbot.at_mode() == "unsupported"
    assert len(client.bodies) == 2

    second = await qqbot.send_at_parts("正文", mentions=["10001"], settings=_image_settings())
    assert second["ok"] is False and "at 消息段" in second["detail"]
    assert len(client.bodies) == 2, "已经知道不支持了，别再白撞一遍"


async def test_send_parts_uses_the_real_at_and_drops_the_text_form(monkeypatch):
    """真 @ 成功：@ 行从正文里摘掉——否则群里既真 @ 了一行、又跟着一串 CQ 码。"""
    seen: list[dict] = []

    async def fake_at(text, *, mentions, settings=None, umo=""):
        seen.append({"text": text, "mentions": list(mentions)})
        return {"ok": True, "status": 200, "detail": "", "umo": umo, "shape": "qq"}

    async def never(text, *, settings=None, umo=""):  # pragma: no cover - 走到就是错
        raise AssertionError("真 @ 成功后不该再发文本")

    monkeypatch.setattr(qqbot, "send_at_parts", fake_at)
    monkeypatch.setattr(qqbot, "send_text", never)
    result = await qqbot.send_parts(
        ["[CQ:at,qq=10001]\n集合啦！"],
        settings=_image_settings(),
        mentions=["10001"],
        mention_text="[CQ:at,qq=10001]",
    )
    assert result["ok"] is True and result["at"] == "qq"
    assert seen == [{"text": "集合啦！", "mentions": ["10001"]}]


async def test_send_parts_falls_back_to_the_text_form(monkeypatch):
    """这个 AstrBot 不收 at 段：@ 行**原样拼回第一段**，消息照旧送达。

    「@ 不到人」只是少了个提醒，绝不能让整条召集发不出去——所以退回必须可靠，
    而且不能把 @ 行拼两遍（群里出现两串 CQ 码比不 @ 更糟）。
    """

    async def reject(text, *, mentions, settings=None, umo=""):
        return {
            "ok": False,
            "status": 400,
            "detail": "unsupported message part type: at",
            "umo": umo,
            "shape": "",
        }

    sent: list[str] = []

    async def fake_text(text, *, settings=None, umo=""):
        sent.append(text)
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    monkeypatch.setattr(qqbot, "send_at_parts", reject)
    monkeypatch.setattr(qqbot, "send_text", fake_text)
    result = await qqbot.send_parts(
        ["[CQ:at,qq=10001]\n集合啦！"],
        settings=_image_settings(),
        mentions=["10001"],
        mention_text="[CQ:at,qq=10001]",
    )
    assert result["ok"] is True and result["at"] == ""
    assert sent == ["[CQ:at,qq=10001]\n集合啦！"]


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
# send_image：新版 AstrBot 只认 attachment_id（先上传，再发引用）
# --------------------------------------------------------------------------- #
_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-card-bytes"


async def test_send_image_uploads_when_the_api_demands_attachment_id(monkeypatch):
    """老形态全被拒（``400 image part missing attachment_id``）→ 上传换 id 再发。

    这是新版 AstrBot 的规矩：图片段只认 ``attachment_id``，而它必须先上传才拿得到。
    """
    client = _FakeClient(
        [400, 400, 400, 200, 200],
        payloads=[None, None, None, {"data": {"attachment_id": "att-1"}}, None],
    )
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image(
        "https://nte.test/api/cards/abc.png", settings=_image_settings(), blob=_PNG
    )
    assert result["ok"] is True, result["detail"]
    assert result["shape"] == "attachment_id"
    assert len(client.bodies) == 4, "3 次老形态 + 1 次带 id 的发送"
    assert len(client.uploads) == 1
    assert client.urls[3] == "http://astrbot.test/api/v1/file", "上传要打到 /api/v1/file"
    assert "file" in client.uploads[0], "multipart 字段名先试 file"
    assert client.bodies[-1]["message"] == [{"type": "image", "attachment_id": "att-1"}]
    assert client.bodies[-1]["umo"] == "aiocqhttp:GroupMessage:123456"


async def test_send_image_remembers_the_path_that_worked(monkeypatch):
    """第一条试出来是「上传」那条路，第二条就别再白撞三次 400 了。"""
    client = _FakeClient(
        [400, 400, 400, 200, 200, 200, 200],
        payloads=[
            None,
            None,
            None,
            {"attachment_id": "att-2"},
            None,
            {"attachment_id": "att-3"},
            None,
        ],
    )
    _fake_httpx(monkeypatch, client)
    settings = _image_settings()
    first = await qqbot.send_image("https://nte.test/a.png", settings=settings, blob=_PNG)
    assert first["ok"] is True
    assert len(client.bodies) == 4 and len(client.uploads) == 1

    second = await qqbot.send_image("https://nte.test/b.png", settings=settings, blob=_PNG)
    assert second["ok"] is True
    assert len(client.bodies) == 5, "第二次只多发一条消息，不再有老形态那三次"
    assert len(client.uploads) == 2
    assert client.bodies[-1]["message"] == [{"type": "image", "attachment_id": "att-3"}]


@pytest.mark.parametrize(
    "payload",
    [{"attachment_id": "flat"}, {"data": {"attachment_id": "nested"}}, {"id": "loose"}],
)
def test_attachment_id_is_found_wherever_it_is(payload):
    """返回结构没有公开契约：平铺 / 嵌套 / 干脆就叫 id，都要认出来。"""
    assert qqbot._find_attachment_id(payload)


async def test_upload_without_file_scope_says_so(monkeypatch):
    """API Key 没勾 file 权限时，403 要说清「去哪儿勾」，而不是甩一句英文了事。"""
    client = _FakeClient([400, 400, 400, 403])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image(
        "https://nte.test/a.png", settings=_image_settings(), blob=_PNG
    )
    assert result["ok"] is False
    assert "file" in result["detail"] and "权限" in result["detail"]


async def test_upload_result_without_attachment_id_reports_the_body(monkeypatch):
    """传上去了却没拿到 id：把 AstrBot 回的原文带出来，别让人只看到「发送失败」。"""
    client = _FakeClient([400, 400, 400, 200], payloads=[None, None, None, {"ok": True}])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image(
        "https://nte.test/a.png", settings=_image_settings(), blob=_PNG
    )
    assert result["ok"] is False
    assert "attachment_id" in result["detail"] and "ok" in result["detail"]


async def test_direct_url_still_wins_when_the_server_accepts_it(monkeypatch):
    """老版本 AstrBot 直接吃地址：那就别多此一举去上传（老部署的行为与开销不变）。"""
    client = _FakeClient([200])
    _fake_httpx(monkeypatch, client)
    result = await qqbot.send_image(
        "https://nte.test/a.png", settings=_image_settings(), blob=_PNG
    )
    assert result["ok"] is True and result["shape"] == "file"
    assert client.uploads == [], "地址能用就不该上传"


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
    calls: dict[str, list] = {"text": [], "image": [], "blob": []}

    async def fake_text(text, *, settings=None, umo=""):
        calls["text"].append(text)
        return {"ok": True, "status": 200, "detail": "", "umo": umo}

    async def fake_image(url, *, settings=None, umo="", blob=None, filename="card.png"):
        calls["image"].append(url)
        calls["blob"].append(blob)
        # 与真 send_image 同一份契约：成功时回报「用了哪条路」
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
    # 图**字节**也要一起交给 send_image：新版 AstrBot 得先上传换 attachment_id
    assert pushed["blob"] and pushed["blob"][0], "推送时要把卡片字节带上（缓存命中就从磁盘读）"
    assert len(pushed["text"]) == 1, "有图时不该再补一屏文字"
    assert "完整赛制与规则见图" in pushed["text"][0]


async def test_push_event_falls_back_to_the_full_text(admin_client, bot_ready, pushed, monkeypatch):
    """图发不出去：**完整文本**顶上（连比赛规则摘要一起），信息一条不少。"""

    async def fail(url, *, settings=None, umo="", blob=None, filename="card.png"):
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
    assert pushed == {"text": [], "image": [], "blob": []}, "预览绝不能真的发出去"


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


# --------------------------------------------------------------------------- #
# 召集：真 @ 走不走得通，以及走不通时的退回
# --------------------------------------------------------------------------- #
async def _with_one_participant() -> dict:
    """把本届名单换成一个「有 QQ 的选手」，返回恢复用的快照。"""
    saved = store.snapshot().dump()
    await store.update(
        {"players": [{"id": "p01", "name": "甲", "qq": "10001"}], "participants": []}
    )
    return saved


async def _restore_roster(saved: dict) -> None:
    await store.update({"players": saved["players"], "participants": saved["participants"]})


async def test_push_call_tries_the_real_at(admin_client, bot_ready, pushed, monkeypatch):
    """召集**先试真 @**：要 @ 的就是参与名单里能对上的 QQ；成功时不再发一遍 CQ 码。"""
    calls: list[dict] = []

    async def fake_at(text, *, mentions, settings=None, umo=""):
        calls.append({"text": text, "mentions": list(mentions)})
        return {"ok": True, "status": 200, "detail": "", "umo": umo, "shape": "qq"}

    monkeypatch.setattr(qqbot, "send_at_parts", fake_at)
    saved = await _with_one_participant()
    try:
        res = await admin_client.post("/api/qqbot/push", json={"kind": "call"})
    finally:
        await _restore_roster(saved)

    assert res.status_code == 200, res.text
    assert res.json()["at"] == "qq"
    assert calls and calls[0]["mentions"] == ["10001"]
    assert calls[0]["text"].startswith("【NTE 比赛】"), "真 @ 发的是正文（@ 行已摘掉）"
    assert pushed["text"] == [], "真 @ 成功时不该再发文本"


async def test_push_call_falls_back_to_the_text_form(admin_client, bot_ready, pushed, monkeypatch):
    """真 @ 发不出去：退回文本写法，@ 行照样发出去（消息不会因为 @ 丢了）。"""

    async def reject(text, *, mentions, settings=None, umo=""):
        return {"ok": False, "status": 400, "detail": "unsupported", "umo": umo, "shape": ""}

    monkeypatch.setattr(qqbot, "send_at_parts", reject)
    saved = await _with_one_participant()
    try:
        res = await admin_client.post("/api/qqbot/push", json={"kind": "call"})
    finally:
        await _restore_roster(saved)

    assert res.status_code == 200, res.text
    assert res.json()["at"] == ""
    assert pushed["text"] and pushed["text"][0].startswith("[CQ:at,qq=10001]")


async def test_preview_explains_the_real_at(admin_client, bot_ready, pushed):
    """预览要说清「会先试真 @」，并给出**退回时**发出去的样子（管理员先看再决定）。"""
    saved = await _with_one_participant()
    try:
        res = await admin_client.get("/api/qqbot/preview", params={"kind": "call"})
    finally:
        await _restore_roster(saved)

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["mentions"] == ["10001"]
    assert data["mentionText"] == "[CQ:at,qq=10001]"
    assert data["parts"] and data["parts"][0].startswith("[CQ:at,qq=10001]")


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
