"""QQ 机器人推送侧的检查。

两条主线：

1. **出厂默认值里不许出现任何具体域名**——预填别人的地址会让人「看着配好了、
   其实消息（连同 API Key）发到别人的机器人上」。这类问题不报错，只会在某天
   被群里的人问「为什么机器人没反应」时才发现。
2. 推送的失败必须是**一句话说清楚**，而不是 500：地址留空时 httpx 抛的是
   ``UnsupportedProtocol``，它不是 ``HTTPError``，漏接就变成 500。
"""

from __future__ import annotations

import json

from app import qqbot
from app.defaults import default_config
from app.models import StreamConfig


def test_defaults_have_no_personal_hosts():
    """出厂配置（含模型兜底值）里不能出现任何具体域名。"""
    blob = json.dumps(
        {
            "config": default_config(),
            "qqbot": qqbot.DEFAULT_SETTINGS,
            "stream": StreamConfig().dump(),
        },
        ensure_ascii=False,
    )
    assert "shiyora" not in blob
    assert StreamConfig().base_url == ""
    assert StreamConfig().hls_base == ""
    assert qqbot.DEFAULT_SETTINGS["baseUrl"] == ""


def test_http_error_text_names_the_exception_type():
    """请求 AstrBot 失败时，原因里必须**带上异常类型**，不能只剩一个冒号。

    这条是补的：httpx 的 ``ReadTimeout()`` / ``ConnectError()`` 的 ``str()`` 是**空串**，
    于是日志里只剩「卡片推送失败，退回纯文本 | 请求 AstrBot 失败：」——用户把这条贴过来，
    谁也看不出到底是超时、还是连不上（真实发生过，排查等于从零开始）。
    """
    import httpx

    for exc, expected in (
        (httpx.ReadTimeout(""), "ReadTimeout"),
        (httpx.ConnectTimeout(""), "ConnectTimeout"),
        (httpx.ConnectError(""), "ConnectError"),
    ):
        text = qqbot._why(exc)
        assert expected in text, text
        assert len(text) > len(expected) + 6, f"原因太短，等于没说：{text}"
    # 两类最常见的失败要顺手给出「往哪查 / 往哪调」
    assert "超时" in qqbot._why(httpx.ReadTimeout(""))
    assert "连不上" in qqbot._why(httpx.ConnectError(""))


async def test_send_text_reports_missing_base_url():
    """没填地址时给一句人话，而不是抛异常（那会变成 500）。"""
    result = await qqbot.send_text(
        "测试",
        settings={
            "enabled": True,
            "apiKey": "abk_x",
            "baseUrl": "",
            "umo": "aiocqhttp:GroupMessage:123456",
        },
    )
    assert result["ok"] is False
    assert "未配置 AstrBot 地址" in result["detail"]


async def test_send_text_refuses_when_disabled_or_without_key():
    """三道前置检查各自给出明确原因（顺序：启用 → Key → 地址 → 目标会话）。"""
    base = {"baseUrl": "https://bot.test", "umo": "aiocqhttp:GroupMessage:1"}
    off = await qqbot.send_text("x", settings={**base, "enabled": False, "apiKey": "k"})
    assert "未启用" in off["detail"]
    no_key = await qqbot.send_text("x", settings={**base, "enabled": True, "apiKey": ""})
    assert "API Key" in no_key["detail"]
    no_target = await qqbot.send_text(
        "x", settings={"enabled": True, "apiKey": "k", "baseUrl": "https://bot.test", "umo": ""}
    )
    assert "目标会话" in no_target["detail"]


def test_plain_text_conversion_is_idempotent():
    """纯文本化可以反复调用（出站前只过一次，但幂等是它的契约）。"""
    source = "**加粗** 和 [文字](https://x.test) 以及 `代码`"
    once = qqbot.to_plain_text(source)
    assert once == qqbot.to_plain_text(once)
    assert "**" not in once


# --------------------------------------------------------------------------- #
# 多队同场：每一方都要出现
#
# 真踩过：小组赛可以 3~4 队同场，而旧的「A vs B」拼法只取前两方——
# 群消息里「四个小队打，只剩两个」。这里钉住每一方都在。
# --------------------------------------------------------------------------- #
def _multi_side_round() -> dict:
    return {
        "code": "G-A-1-1",
        "label": "A 组 · 第 1 轮 · 第 1 场",
        "stage": "group",
        "stageName": "小组赛",
        "status": "live",
        "sets": [],
        "sides": [
            {"label": "甲队", "score": 4},
            {"label": "乙队", "score": 3},
            {"label": "丙队", "score": 2},
            {"label": "丁队", "score": 1},
        ],
    }


def test_round_line_lists_every_team_of_a_multi_team_heat():
    """4 队同场必须**四个队都出现**（逐队列成绩，不是只写前两队的 A:B）。"""
    line = qqbot._round_line(_multi_side_round())
    for name in ("甲队", "乙队", "丙队", "丁队"):
        assert name in line
    assert ":" not in line


def test_round_line_keeps_the_two_team_shape(make_config):
    """2 队对阵照旧写 ``甲 2:1 乙``——多队那一改动不能把常规对阵的排版改掉。"""
    rnd = {
        "code": "WB-1-1",
        "label": "半决赛 · 第 1 场",
        "stageName": "胜者组",
        "status": "done",
        "sets": [],
        "sides": [{"label": "甲队", "score": 2}, {"label": "乙队", "score": 1}],
    }
    assert qqbot._round_line(rnd) == "胜者组 · 半决赛 · 第 1 场 甲队 2:1 vs 乙队"


def test_progress_and_result_messages_keep_all_sides(make_config):
    """赛程进度 / 比赛结果的推送同样要完整：正在打的 4 队同场不能只剩两个。"""
    cfg = make_config(teams=4)
    live = _multi_side_round()
    done = {**_multi_side_round(), "status": "done"}
    for text in (
        qqbot.build_progress_message(cfg, {"rounds": [live]}),
        qqbot.build_result_message(cfg, {"rounds": [done]}),
    ):
        for name in ("甲队", "乙队", "丙队", "丁队"):
            assert name in text, text


def test_card_parts_shortens_the_result_text_when_the_image_is_sent():
    """结果图发成功时，文本只留一行说明（逐场比分都在图里，文字再抄一遍就是刷屏）。"""
    card = {"caption": "【测试赛】比赛结果 · 冠军 甲队 · 完整对阵见图"}
    assert qqbot.card_parts("result", card, ["一大段文本"]) == [card["caption"]]
    assert qqbot.card_parts("result", None, ["一大段文本"]) == ["一大段文本"]


# --------------------------------------------------------------------------- #
# 选手 UUID（纯文本清单）
# --------------------------------------------------------------------------- #
def test_uuids_message_is_one_line_per_player():
    """每行一个「名字 UUID」——要能整段复制；缺 UUID 的写「—」并说明有几个。"""
    from app.models import Config, Player

    cfg = Config.model_validate(default_config())
    cfg.event.name = "UUID 用例届"
    cfg.players = [
        Player(id="p1", name="甲", uuid="GAME-AAA"),
        Player(id="p2", name="乙"),
    ]
    cfg.participants = []
    lines = qqbot.build_uuids_message(cfg).splitlines()
    assert lines[0].startswith("【NTE 比赛】UUID 用例届 · 选手 UUID")
    body = lines[1:3]
    assert body[0] == "甲 GAME-AAA"
    assert body[1] == "乙 —"
    assert "1 人还没登记 UUID" in lines[-1]


# --------------------------------------------------------------------------- #
# 召集：可以只召集「这一场」（赛程页某一场的「召集」按钮）
# --------------------------------------------------------------------------- #
def _call_fixture():
    """两场小组赛、四位选手（每场只该 @ 到自己那几位）。"""
    from app import logic
    from app.models import Config, Player, Round, Side

    cfg = Config.model_validate(default_config())
    cfg.event.name = "轮次召集用例"
    cfg.players = [
        Player(id="p1", name="甲", qq="10001"),
        Player(id="p2", name="乙", qq="10002"),
        Player(id="p3", name="丙", qq="10003"),
        Player(id="p4", name="丁", qq="10004"),
    ]
    cfg.participants = []
    cfg.rounds = [
        Round(
            index=1,
            code="G-A-1-1",
            stage="group",
            label="A 组 · 第 1 轮 · 第 1 场",
            bracket_round=1,
            slot=1,
            sides=[Side(player_ids=["p1", "p2"]), Side(player_ids=["p3"])],
        ),
        Round(
            index=2,
            code="G-A-2-1",
            stage="group",
            label="A 组 · 第 2 轮 · 第 1 场",
            bracket_round=2,
            slot=1,
            sides=[Side(player_ids=["p4"]), Side()],
        ),
    ]
    return cfg, logic.build_state(cfg)


def test_round_call_mentions_only_this_match_s_players():
    """按场次召集：只 @ **这一场上场的人**，并把届名 + 场次信息一起说清。"""
    cfg, state = _call_fixture()
    text = qqbot.build_call_message(cfg, state, {"atMode": "cq"}, None, "G-A-1-1")
    assert "[CQ:at,qq=10001]" in text and "[CQ:at,qq=10002]" in text
    assert "[CQ:at,qq=10003]" in text
    assert "10004" not in text, "下一场的选手不该被 @ 到"
    assert "轮次召集用例 · A 组 · 第 1 轮 · 第 1 场" in text
    assert "对阵：" in text and "时间：" in text


def test_whole_event_call_still_mentions_everyone():
    """不给场次就是整届总召集（原行为不变）。"""
    cfg, state = _call_fixture()
    text = qqbot.build_call_message(cfg, state, {"atMode": "cq"})
    for qq in ("10001", "10002", "10003", "10004"):
        assert qq in text
    assert "集合啦！" in text


def test_round_call_without_known_qq_says_so():
    """这一场谁都没登记 QQ：不静默发一条空 @，要说清去哪儿补。"""
    cfg, state = _call_fixture()
    for player in cfg.players:
        player.qq = ""
    text = qqbot.build_call_message(cfg, state, {"atMode": "text"}, None, "G-A-1-1")
    assert "@" not in text.splitlines()[0]
    assert "先在成员资料里补上 QQ" in text


# --------------------------------------------------------------------------- #
# 录分那一刻自动播报的那条：**只讲这一场**
# --------------------------------------------------------------------------- #
def _result_cfg(rounds):
    from app.models import Config, Player

    cfg = Config.model_validate(default_config())
    cfg.event.name = "单场结果用例"
    cfg.players = [Player(id="p1", name="白"), Player(id="p2", name="x")]
    cfg.participants = []
    cfg.rounds = rounds
    return cfg


def test_match_result_message_covers_one_match_only():
    """届名 + 场次 + 比分 + 胜方 + 时间；**别的场次一个字都不提**。"""
    from app import logic
    from app.models import Round, Side

    played = Round(
        index=1,
        code="G-A-1-1",
        stage="group",
        label="A 组 · 第 1 轮 · 第 1 场",
        bracket_round=1,
        slot=1,
        status="done",
        winner="A",
        sides=[Side(player_ids=["p1"], score=3), Side(player_ids=["p2"], score=1)],
        started_at="2026-10-06T21:05",
        finished_at="2026-10-06T21:20",
        duration_minutes=15,
    )
    other = Round(
        index=2,
        code="G-A-1-2",
        stage="group",
        label="A 组 · 第 1 轮 · 第 2 场",
        bracket_round=1,
        slot=2,
        status="done",
        winner="B",
        sides=[Side(player_ids=["p1"], score=0), Side(player_ids=["p2"], score=5)],
    )
    cfg = _result_cfg([played, other])
    text = qqbot.build_match_result_message(cfg, logic.round_view(cfg, played))
    assert text.splitlines()[0] == "【NTE 比赛】单场结果用例 · 比赛结果"
    assert "A 组 · 第 1 轮 · 第 1 场" in text
    assert "白 3:1 vs x" in text
    assert "胜方：白" in text
    assert "时间：2026年10月6日 21:05 → 21:20（用时 15 分钟）" in text
    assert "第 2 场" not in text and "0:5" not in text, "之前打过的场次不该出现在这条里"


def test_match_result_message_lists_every_side_of_a_multi_team_heat():
    """3~4 队同场：比分逐队列出 + 名次列全（第 1 名就是胜方）。"""
    from app import logic
    from app.models import Player, Round, Side

    heat = Round(
        index=1,
        code="G-A-1-1",
        stage="group",
        label="A 组 · 第 1 轮 · 第 1 场",
        bracket_round=1,
        slot=1,
        status="done",
        winner="A",
        sides=[
            Side(player_ids=["p1"], score=3, rank=1),
            Side(player_ids=["p2"], score=2, rank=2),
            Side(player_ids=["p3"], score=1, rank=3),
            Side(player_ids=["p4"], score=0, rank=4),
        ],
    )
    cfg = _result_cfg([heat])
    cfg.players = [
        Player(id="p1", name="白"),
        Player(id="p2", name="x"),
        Player(id="p3", name="R"),
        Player(id="p4", name="M"),
    ]
    text = qqbot.build_match_result_message(cfg, logic.round_view(cfg, heat))
    assert "白 3 · x 2 · R 1 · M 0" in text
    assert "名次：第 1 白，第 2 x，第 3 R，第 4 M" in text


def test_match_result_message_says_draw():
    """平局就直说「平局」，别去猜谁赢。"""
    from app import logic
    from app.models import Round, Side

    rnd = Round(
        index=1,
        code="G-A-1-1",
        stage="group",
        label="A 组 · 第 1 轮 · 第 1 场",
        bracket_round=1,
        slot=1,
        status="done",
        winner="DRAW",
        sides=[Side(player_ids=["p1"], score=1), Side(player_ids=["p2"], score=1)],
    )
    cfg = _result_cfg([rnd])
    text = qqbot.build_match_result_message(cfg, logic.round_view(cfg, rnd))
    assert "结果：平局" in text and "胜方" not in text


def test_list_message_hint_points_to_a_real_command():
    """列表翻页提示必须让用户发**真实存在**的命令（以前写「发送「下一页」」）。"""
    events = [
        {"id": f"e{i:03d}", "name": f"第 {i} 届", "status": "active", "players": 8, "rounds": 3}
        for i in range(1, 40)
    ]
    text, pages = qqbot.build_events_message(events, page=1, per_page=8)
    assert pages > 1
    assert "发「比赛列表 2」" in text
    assert "发送「下一页」" not in text  # 「下一页」既不是命令也不是别名


def _ids_events(count: int) -> list[dict]:
    return [
        {"id": f"e{i:03d}", "name": f"第 {i} 届", "status": "active"}
        for i in range(1, count + 1)
    ]


def test_ids_message_pages_and_points_at_a_real_command():
    """届次列表（填参数用）：分页，翻页提示同样是**真实可用**的写法。"""
    text, pages = qqbot.build_ids_message(_ids_events(12), page=1)
    assert pages == 2
    assert "共 12 届（第 1 / 2 页）" in text
    assert "e001" in text and "e010" in text
    assert "e011" not in text  # 第二页的内容不该混进第一页
    assert "发「比赛届次 2」" in text

    text2, pages2 = qqbot.build_ids_message(_ids_events(12), page=2)
    assert pages2 == 2
    assert "e011" in text2 and "e012" in text2
    assert "e001 第 1 届" not in text2
    assert "最后一页" in text2


def test_ids_message_skips_hidden_events():
    """隐藏届不列：机器人解析届次用的 /api/bot/events 也看不到它们，
    列出来只会让人照着发一句「没找到这一届」。"""
    events = _ids_events(3) + [{"id": "e099", "name": "隐藏届", "status": "closed", "hidden": True}]
    text, pages = qqbot.build_ids_message(events)
    assert pages == 1
    assert "共 3 届" in text
    assert "隐藏届" not in text


def test_ids_message_mine_scope_wording():
    """「我的」视图：标题说是「你创建的届」，翻页提示也带「我的」。"""
    text, _ = qqbot.build_ids_message(_ids_events(11), page=1, scope="mine")
    assert "你创建的届" in text
    assert "发「比赛届次 我的 2」" in text
    empty, _ = qqbot.build_ids_message([], scope="mine")
    assert "还没有创建过届次" in empty


def test_dispatch_ids_uses_the_ids_builder():
    """dispatch 的 ``ids`` 走 build_ids_message，并把 scope 传下去（措辞与提示都靠它）。"""
    out = qqbot.dispatch(
        "ids", settings={"maxChars": 1200}, events=_ids_events(11), page=1, scope="mine"
    )
    text = "".join(out["parts"])
    assert out["pages"] == 2
    assert "你创建的届" in text
    assert "**" not in text  # 出站一律纯文本
