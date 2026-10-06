"""比赛卡片：内容跟着赛制走、渲染只做一次、地址只认签发过的。

图片是「推送能不能看」的那一环，也是唯一会吃 CPU 的一环，所以两边都得钉住：

* **内容**：摘要里的赛制 / 人数 / 同场队数 / 晋级全由 ``logic.rulebook`` 现算——
  改了赛制，卡片指纹就必须变（否则群里会一直发着一张写着旧规则的图）；
* **渲染**：同一份内容只渲染一次（内容定址 + 落盘），没装 Pillow 时安静退回文本；
* **地址**：``/api/cards/<hash>.png`` 只认本站**签发过**的哈希（猜不出来，也拿不到）。
"""

from __future__ import annotations

import pytest

from app import card, logic
from app.store import store


async def _card(cfg, event_id: str = "e001"):
    return await card.card_for_event(cfg, event_id, site="http://nte.test")


def _cjk_font():
    """这台机器上实际用来画卡片的 CJK 字体（没装就跳过字体检查）。"""
    from app import fonts

    if fonts.resolve("cjk") is None:
        pytest.skip("这台机器上没装 CJK 字体（Linux 上 apt install fonts-noto-cjk 即可）")
    return fonts.load("cjk", 26)


# --------------------------------------------------------------------------- #
# 内容（纯函数，不需要 Pillow）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("teams", "expect"),
    [(2, "组 vs 组"), (3, "3 队同场"), (4, "4 队同场")],
)
def test_payload_adapts_to_every_heat_size(make_config, teams, expect):
    """组 vs 组 / 组 vs 组 vs 组 / 组 vs 组 vs 组 vs 组——三种同场规模都要说对。

    这是规则里最容易写歪的一处（写成「两队对打」就等于把 3 队同场那场说错了）。
    """
    cfg = make_config(teams=teams)
    payload = card.payload_for(cfg, "e001")
    assert dict(payload["meta"])["每场"] == expect
    rules = " ".join(item for section in payload["sections"] for item in section["items"])
    assert expect in rules


def test_card_text_has_no_missing_glyphs(make_config):
    """卡片上的**每一段文字**都要能被实际用的字体画出来——缺字形就是图里的空心方块。

    真踩过：副标题那行（``e902 · 筹备中 · 第 1 届…``）用的是等宽**西文**字体，
    而它含中文，于是一整行都成了方块。这条用例把「内容 ↔ 字体」对一遍，
    以后谁把中文塞给西文字体，这里就会红。
    """
    from app import fonts

    font = _cjk_font()
    payload = card.payload_for(make_config(teams=4), "e001")
    texts = [
        str(payload.get("title") or ""),
        str(payload.get("id") or ""),
        str(payload.get("status") or ""),
        str(payload.get("headline") or ""),
        str(payload.get("note") or ""),
        str(payload.get("noteTitle") or ""),
        str(payload.get("caption") or ""),
    ]
    texts += [f"{key}{value}" for key, value in (payload.get("meta") or [])]
    for section in payload.get("sections") or []:
        texts.append(str(section.get("title") or ""))
        texts += [str(item) for item in (section.get("items") or [])]
    missing = sorted({ch for text in texts for ch in text if not fonts.has_glyph(font, ch)})
    assert not missing, f"这些字符画不出来（会变成方块）：{missing}"


def test_latin_font_cannot_draw_chinese():
    """顺带把「为什么会有方块」钉住：西文字体没有中文字形，所以含中文的行必须走 CJK。"""
    from app import fonts

    if fonts.resolve("latin") in (None, fonts.resolve("cjk")):
        pytest.skip("这台机器上没有独立的西文字体（latin 兜到了 CJK，测不出差别）")
    latin = fonts.load("latin", 24)
    assert fonts.has_glyph(latin, "A")
    assert not fonts.has_glyph(latin, "筹"), "西文字体居然有中文字形？那这条判断的前提变了"
    assert fonts.has_glyph(_cjk_font(), "筹")


def test_digest_changes_when_the_font_changes(tmp_path, monkeypatch):
    """换字体 → 指纹必须变。

    不然会一直发着用旧字体画的那张图（真踩过：中文是一排方块的那版；
    换上中文字体后，内容没变、指纹也没变，群里照旧是方块图）。
    """
    from app import fonts

    payload = {"title": "同一份内容"}
    before = card.digest(payload)
    other = tmp_path / "SomeOtherFont.ttf"
    other.write_bytes(b"x")
    monkeypatch.setattr(fonts, "CJK_PATHS", (str(other),))
    fonts.reset()
    try:
        assert card.digest(payload) != before
    finally:
        fonts.reset()


def test_payload_carries_the_generated_rules(make_config):
    """卡片必须带上**完整规则**（赛制概览 / 小组赛 / 淘汰赛），而不只是信息栏。"""
    payload = card.payload_for(make_config(teams=4), "e001")
    titles = [section["title"] for section in payload["sections"]]
    assert "赛制概览" in titles
    assert "小组赛" in titles
    assert "淘汰赛" in titles
    heat = next(s for s in payload["sections"] if s["title"] == "小组赛")
    assert any("出线" in item or "名次分" in item for item in heat["items"])


def test_payload_lists_groups_and_key_facts(make_config):
    cfg = make_config(teams=4)
    payload = card.payload_for(cfg, "e001")
    assert payload["title"]
    assert payload["caption"] and "完整赛制与规则见图" in payload["caption"]
    keys = dict(payload["meta"])
    assert keys["赛制"] == "锦标赛制"
    assert keys["每队"] == f"{cfg.rules.team_size} 人"
    assert "组" in keys["分组"]
    assert payload["groups"] and payload["groups"][0][0] == "A 组"


def test_digest_is_stable_but_follows_the_rules(make_config):
    """同样的内容指纹必须一样（否则缓存白做）；改了赛制必须不一样（否则图是旧的）。"""
    same_a = card.digest(card.payload_for(make_config(teams=2), "e001"))
    same_b = card.digest(card.payload_for(make_config(teams=2), "e001"))
    other = card.digest(card.payload_for(make_config(teams=4), "e001"))
    assert same_a == same_b
    assert same_a != other


def test_payload_includes_the_event_note(make_config):
    """「赛事信息」（Markdown）也进卡片，且是**纯文本**（图里没有 Markdown）。"""
    cfg = make_config(teams=2)
    cfg.event.rules_text = "**注意**：请提前十分钟到场"
    payload = card.payload_for(cfg, "e001")
    assert "注意" in payload["note"]
    assert "**" not in payload["note"]


# --------------------------------------------------------------------------- #
# 渲染与缓存
# --------------------------------------------------------------------------- #
async def test_card_is_rendered_once_and_reused(make_config, monkeypatch):
    """同一份内容只渲染一次：第二次直接吃缓存（这是「别把服务搞卡」的关键一条）。"""
    calls: list[int] = []
    real = card.render

    def counted(payload, *, site=""):
        calls.append(1)
        return real(payload, site=site)

    monkeypatch.setattr(card, "render", counted)
    cfg = make_config(teams=2)
    first = await _card(cfg)
    assert first and first["bytes"], "第一次应当真的渲染出字节"
    second = await _card(cfg)
    assert second and second["hash"] == first["hash"]
    assert len(calls) == 1, "同一份内容不该再渲染一次"
    assert first["url"] == f"/api/cards/{first['hash']}.png"


async def test_card_is_a_real_png(make_config):
    """渲染出来的得是**能打开的 PNG**（不是空文件、也不是超长画布）。"""
    info = await _card(make_config(teams=4))
    assert info is not None
    path = card.resolve(f"{info['hash']}.png")
    assert path is not None
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    with Image.open(path) as img:
        assert img.width == card.WIDTH
        assert 400 < img.height < card.MAX_HEIGHT


async def test_no_pillow_falls_back_to_text(make_config, monkeypatch):
    """没装 Pillow：不报错、也不留文件，推送照走文本（信息一条不少）。"""
    monkeypatch.setattr(card, "available", lambda: False)
    assert await card.card_for_event(make_config(teams=2), "e001") is None


async def test_render_failure_falls_back_to_text(make_config, monkeypatch):
    """渲染炸了（字体缺失之类）也只当作「这次没图」，不能把推送带下水。"""

    def boom(payload, *, site=""):
        raise RuntimeError("画不出来")

    monkeypatch.setattr(card, "render", boom)
    assert await card.card_for_event(make_config(teams=2), "e002") is None


# --------------------------------------------------------------------------- #
# 地址：只认签发过的
# --------------------------------------------------------------------------- #
def test_resolve_refuses_anything_not_issued():
    """内容哈希也挡不住有人「凑一个链接」——所以只认本站签发过的哈希。"""
    assert card.resolve("") is None
    assert card.resolve("a" * 32 + ".png") is None
    assert card.resolve("../../config/nte.sqlite3") is None
    assert card.resolve("nte.sqlite3") is None
    assert card.resolve("a" * 32 + ".jpg") is None


async def test_issue_then_serve_then_404(client, make_config):
    """签发过的能拉走（图要发到群里），没签发的 404。"""
    cfg = make_config(teams=2)
    info = await card.card_for_event(cfg, store.current_id or "e001", site="http://nte.test")
    assert info is not None
    ok = await client.get(f"/api/cards/{info['hash']}.png")
    miss = await client.get("/api/cards/" + "b" * 32 + ".png")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert ok.headers["cache-control"].startswith("public")
    assert miss.status_code == 404


# --------------------------------------------------------------------------- #
# 规则摘要（纯文本推送那条路）
# --------------------------------------------------------------------------- #
def test_rules_digest_is_plain_and_points_at_the_full_rules(make_config):
    """文本推送里的规则摘要：不带 Markdown 记号，末尾指向完整规则。"""
    from app import qqbot

    text = qqbot.rules_digest(make_config(teams=3))
    assert "比赛规则" in text
    assert "3 队同场" in text
    assert "**" not in text and "`" not in text
    assert "完整规则" in text


def test_rules_digest_shrinks_to_the_limit(make_config):
    """摘要**真的会截断**：规则全塞进群消息就是刷屏（推送本身还有段数上限）。"""
    from app import qqbot

    short = qqbot.rules_digest(make_config(teams=4), limit=3)
    body = [line for line in short.splitlines() if line.startswith("· ")]
    assert len(body) == 3


def test_rulebook_is_the_single_source(make_config):
    """摘要与卡片用的是**同一份**规则（``logic.rulebook``）——不能各写一套。"""
    cfg = make_config(teams=2)
    rulebook = logic.rulebook(cfg)
    payload = card.payload_for(cfg, "e001")
    assert [s["title"] for s in payload["sections"]] == [
        s["title"] for s in rulebook["sections"]
    ]
