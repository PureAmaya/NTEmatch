"""比赛规则的**文案质量**：只讲实际赛制、不自相矛盾、不带解释性旁白。

这些不是「文案好不好看」的问题，而是**对错**的问题：

* 配置说 4 队同场、排出来全是 3 队同场时，规则必须说 3——两个数字并列摆着，
  看的人只会觉得规则自己都没想清楚（这也是用户实际报上来的 bug）；
* 单败的届里写着「落败者进入败者组」，就是一句假话；
* 「（完全确定，无随机）」「（不需要人工点胜负）」这类旁白是写给开发者的，
  不该出现在用户读的规则里。

网页那份用服务端渲染的**行内 Markdown**（``**加粗**`` 真的会加粗），
纯文本那份（群消息摘要 / 卡片）必须不带任何记号。
"""

from __future__ import annotations

import pytest

from app import logic


def _plain(rb: dict) -> str:
    """把规则里所有纯文本条目拼起来（就是群消息 / 卡片读的那份）。"""
    return "\n".join(item for section in rb.get("sections") or [] for item in section["items"])


def _html(rb: dict) -> str:
    return "\n".join(item for section in rb.get("sections") or [] for item in section["itemsHtml"])


def test_heat_size_follows_the_schedule_not_the_config(make_config):
    """配置 4 队同场、实际排出 3 队同场时：只说 3，不说 4（用户报的那个 bug）。"""
    cfg = make_config(teams=3)
    cfg.rules.teams_per_match = 4  # 配置里的上限
    text = _plain(logic.rulebook(cfg))
    assert "3 队同场" in text, "实际排出来的场次规模必须写对"
    assert "4 队同场" not in text, "不能同时出现配置里那个数（两个数字打架）"


def test_two_team_heats_say_head_to_head(make_config):
    """2 队同场的届要写「组 vs 组」，而不是「2 队同场」。"""
    cfg = make_config(teams=2)
    cfg.rules.teams_per_match = 2
    text = _plain(logic.rulebook(cfg))
    assert "组 vs 组" in text
    assert "2 队同场" not in text


def test_no_dev_notes_in_the_rules(make_config):
    """规则里不该有解释性旁白（那些是写给开发者的）。"""
    cfg = make_config(teams=4)
    text = _plain(logic.rulebook(cfg))
    for bad in ("完全确定，无随机", "不需要人工点胜负", "每个组的人数可调", "可调"):
        assert bad not in text, f"规则里不该出现「{bad}」"


def test_single_vs_double_elimination_wording(make_config):
    """单败 / 双败的用词必须跟着配置走——单败的届里不能写败者组。"""
    cfg = make_config(teams=4)
    cfg.rules.loser_bracket = False
    text = _plain(logic.rulebook(cfg))
    assert "单败" in text
    assert "双败" not in text
    # 「没有败者组」这句是对的（它说的正是单败）；但**不能描述败者组的打法**
    for bad in ("掉进败者组", "进入败者组", "败者组冠军", "在败者组再输一场"):
        assert bad not in text, f"单败的届里不该出现「{bad}」"

    cfg.rules.loser_bracket = True
    text = _plain(logic.rulebook(cfg))
    assert "双败" in text
    assert "掉进败者组" in text
    assert "败者组冠军" in text


def test_group_count_wording_says_manual_or_auto(make_config):
    """手动设了组数就说「手动」；没设就说「按队伍数自动划分」。"""
    cfg = make_config(teams=6)
    cfg.rules.group_count = 3
    assert "手动设定" in _plain(logic.rulebook(cfg))
    cfg.rules.group_count = 0
    auto = _plain(logic.rulebook(cfg))
    assert "按队伍数自动划分" in auto
    assert "手动设定" not in auto


def test_auto_group_count_grows_with_the_field(make_config):
    """人越多，自动分出来的组越多（这是「人多多分几个小组」那条要求）。"""
    from app.tournament import group_count_for

    numbers = [group_count_for(n, 2) for n in (8, 12, 16, 24, 32)]
    assert numbers == sorted(numbers), f"组数应当随人数单调不减：{numbers}"
    assert numbers[0] == 2 and numbers[-1] >= 8, numbers
    # 每组至少 3 队（两人一组等于一场定胜负，没有「小组」的意义）
    for teams in (7, 8, 12, 16, 24):
        groups = group_count_for(teams, 2)
        assert teams // groups >= 3, f"{teams} 队分 {groups} 组时每组只剩 {teams // groups} 队"


def test_bold_is_rendered_in_html_but_plain_stays_plain(make_config):
    """网页那份把 ``**加粗**`` 渲染成真标签；纯文本那份一个记号都不留。"""
    cfg = make_config(teams=3)
    rb = logic.rulebook(cfg)
    html = _html(rb)
    assert "<strong>" in html, "该加粗的地方要真的加粗"
    assert "**" not in html, "渲染过的 HTML 里不该再留 Markdown 记号"
    plain = _plain(rb)
    assert "**" not in plain
    assert "<" not in plain, "纯文本那份不能带标签（群里发出去会原样显示）"


def test_html_is_escaped(make_config):
    """规则里拼进去的名字可能是用户输入，必须转义（先转义再渲染白名单）。"""
    from app import markdown

    assert markdown.inline("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    rendered = markdown.inline("**A** 与 <b>x</b>")
    assert "<strong>A</strong>" in rendered
    assert "&lt;b&gt;" in rendered


# --------------------------------------------------------------------------- #
# 发给 QQ 的文案：纯文本
# --------------------------------------------------------------------------- #
BOT_TOKEN = "nte_test_bot_token"


@pytest.fixture
async def bot_client():
    """配一个机器人令牌，拿到「插件那一边」的客户端（用完把令牌撤掉）。"""
    import httpx

    from app import db
    from app.auth import hash_secret
    from app.main import app
    from app.store import store

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    await store.set_qqbot(
        {"botApiTokenHash": hash_secret(BOT_TOKEN)}, actor="test", internal=True
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    ) as client:
        yield client
    await store.set_qqbot({"botApiTokenHash": ""}, actor="test", internal=True)


async def test_qq_facing_texts_have_no_markdown(bot_client):
    """下发给插件的文案是**纯文本**：``**加粗**`` 在 QQ 里就是两个星号。

    这些文字是插件照着发进群 / 私聊的（命令清单、参数说明、帮助），
    群窗口不渲染 Markdown——星号、反引号只会显得莫名其妙。
    卡片那条路（``card_payload``）另有 ``markdown.to_text`` 兜着，这里是**接口层**。
    """
    res = await bot_client.get("/api/bot/manifest")
    assert res.status_code == 200, res.text
    data = res.json()
    texts: list[str] = []
    for item in data.get("kinds") or []:
        texts += [str(item.get("label") or ""), str(item.get("hint") or "")]
    for item in data.get("commands") or []:
        texts += [
            str(item.get("command") or ""),
            str(item.get("args") or ""),
            str(item.get("note") or ""),
        ]
    texts += [str(value) for value in (data.get("params") or {}).values()]
    assert len(texts) > 10, "清单不该是空的（否则这条用例什么都没验到）"
    for text in texts:
        for mark in ("**", "`", "~~"):
            assert mark not in text, f"发给 QQ 的文案里不该有 Markdown 记号 {mark!r}：{text}"


def test_plain_text_strips_marks():
    """保险丝本身：Markdown → 纯文本时，强调 / 代码 / 删除线的记号都要摘掉。"""
    from app import markdown

    plain = markdown.to_text("**加粗** 与 *斜体* 和 `代码` 和 ~~删除~~")
    for mark in ("**", "*", "`", "~~"):
        assert mark not in plain, f"{mark!r} 没被摘掉：{plain}"
    assert "加粗" in plain and "斜体" in plain and "代码" in plain
