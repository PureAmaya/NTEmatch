"""复制一届（``POST /api/events/{id}/copy``）。

用户要的是两句话：「**原封不动**复制过来」+「复制出来的那届是**筹备中**」。
这两句合起来就决定了这个功能的形状——

* 「原封不动」= 赛制 / 名单 / 队伍 / 赛程骨架 / 展示信息一个不落（不然还得重录一遍）；
* 「筹备中」= **成绩必须清空**：带着上一届的比分复制，只有两种下场——要么一出生就被
  「打完自动结束本届」判成已结束，要么顶着「筹备中」却已经有冠军，两种都自相矛盾。

所以这一组盯「照抄什么、抹掉什么」，外加三件容易忽略的事：
**源届一个字节都不许动**（复制是只读操作）、**新届要能立刻接着打**（不是只读的）、
以及复制出来的届**不能再自助报名**（它已经有队伍与赛程，报名闸门会拦住——这是刻意的）。
"""

from __future__ import annotations

import httpx
import pytest

from app import db
from app.auth import auth
from app.main import app
from app.models import Member, metrics
from app.store import signup_blocked, store

OWNER_UID = "u_copy_owner"
OTHER_QQ = "30001"

#: 源届的样子：**打过的、已结束的、锁定的、有替补的**——所有「会被复制带错」的东西都齐了。
_SOURCE = {
    "event": {
        "name": "复制用例·源届",
        "title": "复制用例·源届",  # 与届名一致 = 新建时自动填的那种标题（应当跟着新届名走）
        "sport": "racing",
        "ranked": False,
        "brief": "简介第一行\n第二行",
        "venue": "上海",
        "organizer": "小队长",
        "subtitle": "NEVERNESS · COPY",
        "startTime": "2026-01-01T10:00:00",
        "endTime": "2026-01-02T10:00:00",
        "locked": True,
        "lockedAt": "2026-01-01T10:00:00",
        "rulesText": "规则文案",
    },
    "rules": {"format": "league", "valueType": "time", "valueLabel": "用时", "better": "low"},
    "players": [
        {"id": "p01", "name": "甲", "uuid": "U-1", "qq": "10001", "memberUid": "u_a"},
        {"id": "p02", "name": "乙", "uuid": "U-2", "qq": "10002", "memberUid": "u_b"},
    ],
    "teams": [
        {"id": "t1", "name": "甲队", "playerIds": ["p01"]},
        {"id": "t2", "name": "乙队", "playerIds": ["p02"]},
    ],
    "participants": ["p01", "p02"],
    "participantsSet": True,
    "rounds": [
        {
            "index": 1,
            "code": "G-1",
            "stage": "group",
            "bracketRound": 1,
            "slot": 1,
            "label": "第 1 场",
            "status": "done",
            "winner": "A",
            "note": "场次备注要留着",
            "sets": [{"a": 8000, "b": 9000}, {"a": 7000, "b": 9500}],
            "durationMinutes": 42,
            "live": True,
            "liveNote": "本场直播",
            "scheduledAt": "2026-01-01T10:00:00",
            "startedAt": "2026-01-01T10:05:00",
            "finishedAt": "2026-01-01T10:47:00",
            "locked": True,
            "srcA": "seed:1",
            "srcB": "seed:2",
            "sides": [
                {
                    "key": "A",
                    "playerIds": ["p01"],
                    "teamId": "t1",
                    "label": "甲队",
                    "score": 15000,
                    "points": 7,
                    "rank": 1,
                    "forfeit": True,
                    "source": "A 组第 1",
                },
                {
                    "key": "A",
                    "playerIds": ["p02"],
                    "teamId": "t2",
                    "label": "乙队",
                    "score": 18500,
                    "points": 3,
                    "rank": 2,
                },
            ],
        },
        {  # 还没打的那一场：复制过来还是「未开始」，没有可比性变化
            "index": 2,
            "code": "G-2",
            "stage": "group",
            "bracketRound": 1,
            "slot": 2,
            "label": "第 2 场",
            "status": "pending",
            "sides": [
                {"key": "A", "playerIds": ["p01"], "teamId": "t1", "label": "甲队"},
                {"key": "A", "playerIds": ["p02"], "teamId": "t2", "label": "乙队"},
            ],
        },
    ],
    "substitutions": [
        {"id": "s1", "fromId": "p01", "toId": "p02", "scope": "round", "anchor": "G-1"},
    ],
}


@pytest.fixture(autouse=True)
async def _clean_member():
    """复制接口的权限用例会建一个普通成员，跑完清掉（成员是全局的，会串到别的用例）。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()

    async def purge() -> None:
        for member in [m for m in store.members() if m.qq == OTHER_QQ]:
            await store.delete_member(member.uid, actor="test")

    await purge()
    yield
    await purge()


@pytest.fixture
async def source():
    """造一届「打过的、已结束的」源届；用完把这一轮**新建出来的届**全删掉。

    清理按「入场时的届次集合」差集来删，而不是只删自己造的那一届——用例里还会经接口
    复制出新的届，那些也得收干净（否则会留到别的用例的「全部届次」断言里）。
    """
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    previous = store.current_id
    before = {e["id"] for e in await store.list_events()}

    await store.create_event("复制用例·源届", owner_uid=OWNER_UID)
    src = store.current_id
    await store.update({**_SOURCE, "event": {**_SOURCE["event"], "name": "复制用例·源届"}})
    await store.update_event_meta(src, {"status": "closed"})
    try:
        yield src
    finally:
        for entry in await store.list_events():
            if entry["id"] in before:
                continue
            if store.current_id == entry["id"] and previous:
                await store.switch_event(previous)
            await store.delete_event(entry["id"])
        if previous and store.current_id != previous:
            await store.switch_event(previous)


async def _member_session(permission: str = "member"):
    """造一个成员并给它一把会话（用来测「不是赛事管理员就复制不了」）。

    ``auth.issue()`` 的默认身份是**服务器管理员**，所以必须把成员的身份一起传进去——
    否则「普通成员被判 403」这种用例会假通过（它拿的其实是管理员会话）。
    """
    member, _key, _bearer = await store.save_member(
        Member(uid="", name="路人", qq=OTHER_QQ, permission=permission)
    )
    return member, auth.issue(member.uid, uid=member.uid, name=member.name, permission=permission)


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nte.test",
        headers={"X-NTE-Token": token},
    )


# --------------------------------------------------------------------------- #
# 复制本身：照抄什么、抹掉什么
# --------------------------------------------------------------------------- #
async def test_copy_keeps_everything_but_the_results(source):
    """「原封不动」的意思是：**除了成绩与时间，逐项一模一样**。

    这条不逐字段列清单，而是把两份配置的每个顶层键比一遍——新增字段时也不会漏测。
    允许不同的只有 ``event`` / ``rounds`` / ``substitutions`` 与版本戳（下面两条各查各的）。
    """
    await store.duplicate_event(source, "复制用例·副本", owner_uid="u_operator")
    copy_id = store.current_id
    assert copy_id != source

    src = (await store.read_event(source)).dump()
    new = (await store.read_event(copy_id)).dump()
    untouched = {"revision", "updatedAt", "event", "rounds", "substitutions"}
    for key in sorted(set(src) | set(new)):
        if key in untouched:
            continue
        assert src.get(key) == new.get(key), f"顶层 {key} 没有照抄：{src.get(key)!r} → {new.get(key)!r}"

    assert new["rules"]["format"] == "league" and new["rules"]["valueType"] == "time"
    assert [p["id"] for p in new["players"]] == ["p01", "p02"]
    assert new["participants"] == ["p01", "p02"] and new["participantsSet"] is True
    assert [t["name"] for t in new["teams"]] == ["甲队", "乙队"]


async def test_copy_resets_scores_status_and_dates(source):
    """抹掉的是「打过的证据」：成绩 / 胜者 / 用时 / 时间戳 / 锁定 / 替补，一场都不留。

    留的是**赛程骨架**——对阵、席位来源、每场的人与队伍，一个字都没动（下面逐项断言）。
    """
    await store.duplicate_event(source, "复制用例·副本", owner_uid="u_operator")
    cfg = await store.read_event(store.current_id)

    # ① 届本身：筹备中，日期与「开赛锁定」都清掉
    assert cfg.event.status == "draft", "复制出来的届必须是筹备中（用户明确要求的）"
    assert cfg.event.name == "复制用例·副本"
    assert (cfg.event.start_time, cfg.event.end_time) == ("", "")
    assert cfg.event.locked is False and cfg.event.locked_at == "", "不在筹备中的届改不了东西"
    assert cfg.event.owner_uid == "u_operator", "复制出来的届归操作者（与新建一致）"
    assert cfg.event.hidden is False
    # 展示信息照抄（用户要的「原封不动」主要就是这些）
    assert (cfg.event.sport, cfg.event.ranked) == ("racing", False)
    assert (cfg.event.venue, cfg.event.organizer) == ("上海", "小队长")
    assert cfg.event.brief == "简介第一行\n第二行"
    assert cfg.event.subtitle == "NEVERNESS · COPY"
    assert cfg.event.rules_text == "规则文案"
    assert cfg.event.title == "复制用例·副本", "标题是跟着届名自动填的，就该跟着新届名走"

    # ② 替补登记不带走：它按对局编号生效，新届一场都没打
    assert cfg.substitutions == []

    # ③ 每一场回到「未开始」
    done, pending = cfg.rounds
    assert len(cfg.rounds) == 2, "场次本身要照抄（赛程骨架），不然还得重新排一遍"
    assert [r.code for r in cfg.rounds] == ["G-1", "G-2"]
    assert done.status == "pending" and pending.status == "pending"
    assert done.winner == "" and done.sets == []
    assert (done.duration_minutes, done.started_at, done.finished_at) == (0, "", "")
    assert done.scheduled_at == "", "上一届的日程不能带过来（新届的日期还没定）"
    assert done.locked is False
    assert done.note == "场次备注要留着", "场次备注是赛程安排的一部分，照抄"
    assert (done.src_a, done.src_b) == ("seed:1", "seed:2"), "淘汰赛席位来源要照抄"
    assert [p.player_ids for p in done.sides] == [["p01"], ["p02"]]
    assert [s.team_id for s in done.sides] == ["t1", "t2"]
    assert [s.source for s in done.sides] == ["A 组第 1", ""]
    for side in done.sides:
        assert side.score == metrics.MISSING, "成绩要回「没有成绩」，不是 0（0 是合法读数）"
        assert (side.points, side.rank, side.forfeit) == (0, 0, False)


async def test_copy_does_not_touch_the_source(source):
    """复制是**只读**源届：它的成绩、状态、锁定、替补一个字都不许变。

    这是「复制」与「切换 / 编辑」最容易混起来的地方——复制完源届还是原样，随时能回看。
    """
    before = (await store.read_event(source)).dump()
    await store.duplicate_event(source, "复制用例·副本", owner_uid="u_operator")
    after = (await store.read_event(source)).dump()

    assert after == before, "源届被改动了：复制必须是只读操作"
    assert after["event"]["status"] == "closed" and after["event"]["locked"] is True
    assert after["rounds"][0]["winner"] == "A" and after["rounds"][0]["sides"][0]["score"] == 15000


async def test_copy_becomes_the_current_event_and_is_playable(source):
    """复制完**立刻切过去**，而且新届是能接着用的：录一场分就该正常计上。

    只切过去还不够——如果新届带着上一届的「已完成」痕迹，一录分就会被各种闸门拦住
    （「已结束只读」「这一场已经打完」），用户看到的是「复制过来的届是坏的」。
    """
    await store.duplicate_event(source, "复制用例·副本", owner_uid="u_operator")
    copy_id = store.current_id
    assert copy_id != source, "复制完要切到新届，不然用户还得自己去列表里找"

    entry = next(e for e in await store.list_events() if e["id"] == copy_id)
    assert entry["status"] == "draft"
    assert entry["rounds"] == 2 and entry["played"] == 0 and entry["champion"] == ""
    assert entry["ownerUid"] == "u_operator"

    # 复制出来的届不能再自助报名：它已经有队伍与赛程（报名只对「还没组队」的筹备届开放）
    assert signup_blocked(await store.read_event(copy_id)), "已有队伍/赛程的届不该放开自助报名"


async def test_copied_round_can_take_a_fresh_result(source, admin_client):
    """新届的场次能正常录分：说明「清空成绩」清对了（不会被当成已结算的场次）。

    走接口而不是直接改模型：录分写的是**当前届**，也就是刚复制出来的这一届，
    这条路径顺带证明「复制完切过去了」以及「新届不是只读的」。
    """
    await store.duplicate_event(source, "复制用例·副本", owner_uid="u_operator")
    copy_id = store.current_id

    res = await admin_client.post(
        "/api/rounds/G-1/result",
        json={"sets": [], "sides": [{"key": "A", "score": 3}, {"key": "B", "score": 1}]},
    )
    assert res.status_code == 200, res.text
    after = await store.read_event(copy_id)
    played = [r for r in after.rounds if r.status == "done"]
    assert [r.code for r in played] == ["G-1"], "复制过来的场次应当能被正常结算"
    # 源届是「时间型 + 数值低胜」，复制过来的规则照旧生效：3 比 1 大，所以赢的是 B
    assert after.rounds[0].winner == "B", "规则也是照抄来的，复制后照样按它判胜负"


# --------------------------------------------------------------------------- #
# 接口：权限、隐藏届、命名
# --------------------------------------------------------------------------- #
async def test_copy_endpoint_needs_event_permission(source):
    """复制是写操作：匿名 401、普通成员 403——不能谁都能凭空造出一届来。"""
    _member, session = await _member_session("member")
    async with _client(session.token) as c:
        res = await c.post(f"/api/events/{source}/copy", json={"name": "偷偷复制"})
    assert res.status_code == 403, res.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    ) as anon:
        res = await anon.post(f"/api/events/{source}/copy", json={"name": "匿名复制"})
    assert res.status_code == 401, res.text


async def test_copy_endpoint_works_for_a_plain_admin_and_names_the_copy(source, admin_client):
    """赛事管理员能复制**别人的**届（源届本来就人人可看），不传名字时自动叫「… 副本」。"""
    _member, session = await _member_session("event_admin")
    async with _client(session.token) as c:
        res = await c.post(f"/api/events/{source}/copy", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "复制用例·源届 副本"
    assert body["eventId"] and body["eventId"] != source
    assert body["rounds"] == 2 and body["players"] == 2

    entry = next(e for e in (await admin_client.get("/api/events")).json()["events"] if e["id"] == body["eventId"])
    assert entry["status"] == "draft"
    assert entry["ownerUid"] == session.uid, "复制出来的届归操作者（不是源届的创建者）"
    assert store.current_id == body["eventId"], "复制完要切过去"


async def test_hidden_source_is_server_only(source, admin_client):
    """隐藏的届对别人等于不存在：赛事管理员复制它会 404，服务器管理员照常。"""
    await store.update_event_meta(source, {"hidden": True})

    _member, session = await _member_session("event_admin")
    async with _client(session.token) as c:
        res = await c.post(f"/api/events/{source}/copy", json={"name": "复制隐藏届"})
    assert res.status_code == 404, res.text

    res = await admin_client.post(f"/api/events/{source}/copy", json={"name": "复制隐藏届"})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "复制隐藏届"

    # 复制出来的是新届，**不带「隐藏」**：它是给人看的（要隐藏再点一下）
    entry = next(
        e for e in (await admin_client.get("/api/events")).json()["events"] if e["id"] == res.json()["eventId"]
    )
    assert entry["hidden"] is False


async def test_copy_missing_source_is_404(admin_client):
    """不存在的源届：404 说清是哪一届，而不是悄悄造一个空届出来。"""
    res = await admin_client.post("/api/events/e999/copy", json={})
    assert res.status_code == 404, res.text
    assert "e999" in res.text
