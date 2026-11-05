"""比赛卡片：内容跟着赛制走、渲染只做一次、地址只认签发过的。

图片是「推送能不能看」的那一环，也是唯一会吃 CPU 的一环，所以两边都得钉住：

* **内容**：摘要里的赛制 / 人数 / 同场队数 / 晋级全由 ``logic.rulebook`` 现算——
  改了赛制，卡片指纹就必须变（否则群里会一直发着一张写着旧规则的图）；
* **渲染**：同一份内容只渲染一次（内容定址 + 落盘），没有出图侧车时安静退回文本；
* **地址**：``/api/cards/<hash>.png`` 只认本站**签发过**的哈希（猜不出来，也拿不到）。
"""

from __future__ import annotations

import pytest

from app import card, logic
from app.store import store


async def _card(cfg, event_id: str = "e001"):
    return await card.card_for_event(cfg, event_id, site="http://nte.test")


# --------------------------------------------------------------------------- #
# 内容（纯函数，不需要渲染器）
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
def test_digest_follows_the_renderer(make_config, monkeypatch):
    """指纹要跟着**渲染器**（协议版本 / 构建标记 / 实际字体）走。

    换二进制、换字体之后，同一份内容必须算出新的哈希——否则缓存里那张旧图
    （比如中文是方块的那一版）会被一直发出去。
    """
    payload = card.payload_for(make_config(teams=2), "e001")
    before = card.digest(payload)
    monkeypatch.setattr(card.renderer, "signature", lambda: "r9|other|/font/b.ttc")
    assert card.digest(payload) != before


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


def test_payload_shows_the_brief_and_keeps_its_line_breaks(make_config):
    """卡片上那一块（以前叫「赛事信息」、放规则文本）现在放**比赛简介**，且保留换行。"""
    cfg = make_config(teams=2)
    cfg.event.brief = "地图：白尾树盘山公路\n规则：禁用改装\n提前 10 分钟到场"
    cfg.event.rules_text = "**注意**：这段规则不该出现在这一块里"
    payload = card.payload_for(cfg, "e001")
    assert payload["noteTitle"] == "比赛简介"
    assert "白尾树盘山公路" in payload["note"] and "禁用改装" in payload["note"]
    assert "\n" in payload["note"], "换行要留着（渲染时按原有换行折行）"
    assert "注意" not in payload["note"], "这一块不再放规则文本"
def test_brief_keeps_line_breaks_but_drops_blank_lines():
    """简介：**保留换行**、行内合并空白、丢掉空行（最多 6 行 / 200 字）。"""
    from app.models import MAX_BRIEF_LINES, EventInfo

    evt = EventInfo(brief="地图：白尾树盘山公路\n\n  规则：禁用改装  \n\n\n提前 10 分钟到场")
    assert evt.brief == "地图：白尾树盘山公路\n规则：禁用改装\n提前 10 分钟到场"
    long = EventInfo(brief="\n".join(f"第 {i} 行" for i in range(1, 12)))
    assert len(long.brief.split("\n")) == MAX_BRIEF_LINES
    assert len(EventInfo(brief="字" * 500).brief) == 200


# --------------------------------------------------------------------------- #
# 渲染与缓存
# --------------------------------------------------------------------------- #
#: 出图跑在 Go 侧车里（go/cmd/nterender）。没有二进制时这些用例跳过——
#: 它们验证的是「真画出来了吗」，没有渲染器就无从验证（构建：
#: ``cd go && go build -o bin/nterender ./cmd/nterender``）。
needs_render = pytest.mark.skipif(
    not card.available(), reason="出图侧车不可用（go/bin/nterender 没构建）"
)


@needs_render
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


@needs_render
async def test_card_is_a_real_png(make_config, image_size):
    """渲染出来的得是**能打开的 PNG**（不是空文件、也不是超长画布）。"""
    info = await _card(make_config(teams=4))
    assert info is not None
    path = card.resolve(f"{info['hash']}.png")
    assert path is not None
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = image_size(raw)
    # 画布宽度按内容挑（go/internal/render 的 Widths），只会是那两档之一
    assert width in (760, 1080)
    assert 400 < height < 6000


async def test_no_renderer_falls_back_to_text(make_config, monkeypatch):
    """没有出图侧车：不报错、也不留文件，推送照走文本（信息一条不少）。"""
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


@needs_render
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


# --------------------------------------------------------------------------- #
# 比赛结果卡片：小组赛逐场 + 淘汰赛树状图
#
# 「比赛结果」不再是一屏文字：每一场小组赛比分 + 每一场淘汰赛比分与晋级走向都在一张图里，
# 淘汰赛按树状图画（与站点「对阵总览」同一套父子关系：以 srcA / srcB 为准）。
# --------------------------------------------------------------------------- #
def _played_tournament(*, teams: int = 8, team_size: int = 1, loser_bracket: bool = False):
    """造一届**已经打完**的锦标赛（小组赛 + 淘汰赛都有结果），返回 ``Config``。"""
    from app import tournament
    from app.defaults import default_config
    from app.models import Config, Player

    base = default_config()
    cfg = Config.model_validate(
        {
            **base,
            "rules": {
                **base["rules"],
                "format": "tournament",
                "teamSize": team_size,
                "teamsPerMatch": 2,
                "loserBracket": loser_bracket,
            },
        }
    )
    cfg.event.name = "结果图测试赛"
    cfg.players = [
        Player(id=f"p{i}", name=f"选手{i:02d}") for i in range(1, teams * team_size + 1)
    ]
    formed, _ = tournament.auto_form_teams(cfg.players, team_size, seed=11)
    tournament.assign_groups(formed, max(1, teams // 4))
    cfg.teams = formed
    cfg.rounds, _warnings, _summary = tournament.build_tournament(formed, cfg.rules)

    def play() -> None:
        for rnd in cfg.rounds:
            if rnd.status == "done":
                continue
            if rnd.stage == "group":
                for index, side in enumerate(rnd.sides):
                    side.score = len(rnd.sides) - index
            elif all(side.team_id for side in rnd.sides):
                rnd.sides[0].score = 3
                rnd.sides[1].score = 1
            else:
                continue
            winner = tournament.judge_round(rnd, scoring=cfg.rules.scoring)
            if winner:
                rnd.winner = winner
                rnd.status = "done"

    for _ in range(4):
        cfg.rounds = tournament.resolve_tournament(formed, cfg.rounds, cfg.rules.scoring)
        play()
    cfg.rounds = tournament.resolve_tournament(formed, cfg.rounds, cfg.rules.scoring)
    return cfg


def test_result_payload_covers_every_played_match():
    """每一场小组赛、每一场淘汰赛都要进图——少一场，读者就得回站点翻。"""
    cfg = _played_tournament()
    payload = card.payload_for_result(cfg, "e001", logic.build_state(cfg))
    group_rounds = [r for r in cfg.rounds if r.stage == "group"]
    assert sum(len(group["matches"]) for group in payload["groups"]) == len(group_rounds)
    knockout = [r for r in cfg.rounds if r.stage in ("wb", "lb", "gf")]
    assert {node["code"] for node in payload["tree"]["nodes"]} == {r.code for r in knockout}
    assert payload["caption"] and "结果" in payload["caption"]


def test_result_tree_links_follow_src_references():
    """树状图的连线照 ``srcA`` / ``srcB`` 连：上游对局在左、下游在右。"""
    cfg = _played_tournament()
    tree = card.payload_for_result(cfg, "e001", logic.build_state(cfg))["tree"]
    by_code = {node["code"]: node for node in tree["nodes"]}
    knockout = [rnd for rnd in cfg.rounds if rnd.stage in ("wb", "lb", "gf")]
    expected = sum(
        1
        for rnd in knockout
        for ref in (rnd.src_a, rnd.src_b)
        if str(ref).split(":")[0] in by_code
    )
    assert len(tree["links"]) == expected
    assert all(link["x2"] > link["x1"] for link in tree["links"]), "连线要从上游指向下游"


@needs_render
async def test_result_card_is_a_real_image(make_config, image_size):
    """结果图要是能打开的 PNG，宽度**至少容得下树状图**（树排下来有多宽就得有多宽）。"""
    cfg = _played_tournament(loser_bracket=True)
    info = await card.card_for_event(
        cfg, "e001", logic.build_state(cfg), site="http://nte.test", kind="result"
    )
    assert info is not None
    path = card.resolve(f"{info['hash']}.png")
    assert path is not None

    tree = card.payload_for_result(cfg, "e001", logic.build_state(cfg))["tree"]
    width, height = image_size(path.read_bytes())
    # 至少容得下树状图（树排下来有多宽就得有多宽；两侧各留 64px 安全边距）
    assert width >= tree["width"]
    assert 400 < height < 6000


def test_group_stage_not_done_hides_advancing():
    """小组赛没打完：**不显示谁晋级、也不显示谁淘汰**。

    「晋级 #N」「最终排名」都是打完才谈得上的结论；提前给出来就是误导（名次还在变，
    而且没人知道最后谁出线）。网站总览与机器人推送共用 ``groupStageDone`` 这一个标记，
    这里盯住推送那一侧（结果图的内容）。
    """
    import json

    from app import tournament

    cfg = _played_tournament()
    group = next(rnd for rnd in cfg.rounds if rnd.stage == "group")
    tournament.reset_round_result(group)  # 把小组赛退回「还没打完」
    state = logic.build_state(cfg)
    assert state["format"]["groupStageDone"] is False
    assert all(row["advanced"] is False for row in state["ranking"])

    payload = card.payload_for_result(cfg, "e001", state)
    # 每一行都没有「晋级 #N」徽标，组说明里也不再写「前 N 名晋级」
    assert all(row["seed"] == 0 for group_rows in payload["groups"] for row in group_rows["rows"])
    assert all("晋级" not in (group_rows["note"] or "") for group_rows in payload["groups"])
    assert "最终排名" not in json.dumps(payload, ensure_ascii=False)


def test_result_card_and_info_card_do_not_share_a_cache_entry(make_config):
    """同一届的「信息卡」与「结果卡」内容不同 → 指纹必须不同（否则发出去的是错的那张）。"""
    cfg = _played_tournament()
    state = logic.build_state(cfg)
    assert card.digest(card.payload_for(cfg, "e001", state)) != card.digest(
        card.payload_for_result(cfg, "e001", state)
    )
