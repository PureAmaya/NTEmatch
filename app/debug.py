"""调试模式（``uv run main.py --debug``）的演示数据与模拟直播。

调试时最麻烦的两件事：**库里没有数据**（所有页面都是空状态，看不出对错）和
**没有媒体服务器**（直播页 / 导播台永远是「没有人直播」，多画面根本没得调）。
这个模块各解决一半：

* :func:`ensure_demo`：库还是空的时候写一届演示赛事——8 位选手、5 局轮换赛程、
  两局已出结果、一局「进行中」，于是总览 / 赛程 / 排名 / 直播 / 导播台都有内容。
* :func:`active`：把「谁在推流」当成全部选手都在推（见 app/live.py 的
  ``streaming_player_ids``）；配合 app/logic.py 打的 ``mock`` 标记，
  前端用本地生成的画面代替真实 WebRTC（见 static/js/live.js 的 ``playMock``）。

**只在 ``NTE_DEBUG=1`` 时生效**，且 :func:`ensure_demo` 只在「一个选手都没有」时写入，
绝不会覆盖已有数据。
"""

from __future__ import annotations

import os
from typing import Any

from .logging_conf import get_logger

log = get_logger("debug")

#: 演示赛事的届名：一眼能看出这是调试数据，别和真实赛事混在一起。
DEMO_EVENT_NAME = "调试演示赛"

#: 演示选手：8 人正好让 2v2 轮换有轮空，导播台四格也填得满。
_DEMO_PLAYERS: tuple[tuple[str, str], ...] = (
    ("d01", "夜见铃"),
    ("d02", "白鹭昕"),
    ("d03", "沉舟"),
    ("d04", "苏黎"),
    ("d05", "阿澈"),
    ("d06", "江晚"),
    ("d07", "陆铭"),
    ("d08", "星野遥"),
)

#: 演示轮次与「已经打到哪一步」：第 1 局进行中（导播台跟这一场），第 2、3 局已出结果。
_DEMO_TOTAL_ROUNDS = 5
_DEMO_DONE_SCORES: tuple[tuple[int, int, str], ...] = (
    # (A 方得分, B 方得分, 胜方)
    (12, 10, "A"),
    (9, 12, "B"),
)


def active() -> bool:
    """调试模式开着吗。

    全站只认 ``NTE_DEBUG`` 这一个开关（``--debug`` 由 CLI 转成它，见 app/cli.py），
    这样「命令行参数 / 环境变量 / 各模块」三处不会各说各话。
    """
    return os.getenv("NTE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def demo_players() -> list[dict[str, Any]]:
    """演示选手的原始字典（camelCase，直接喂给 ``Config.model_validate``）。

    每人都配了流名：没有流名就没有机位，直播页与导播台会一直是空的。
    """
    return [
        {
            "id": pid,
            "name": name,
            "streamKey": f"demo-{pid}",
            "active": True,
        }
        for pid, name in _DEMO_PLAYERS
    ]


def demo_rounds() -> list[dict[str, Any]]:
    """演示赛程：轮换生成的 5 局，第 1 局标成进行中，第 2、3 局填上比分。

    故意让第 1 局处于「进行中」：导播台按「进行中的那一场」排机位，
    没有这一局的话四宫格永远是空的，多画面就没得调。
    """
    from . import league
    from .models import Config

    cfg = Config.model_validate(
        {
            "players": demo_players(),
            "rules": {
                "format": "league",
                "teamSize": 2,
                "totalRounds": _DEMO_TOTAL_ROUNDS,
                "fairRotation": True,
            },
        }
    )
    rounds, _ = league.generate_rotation_rounds(
        cfg.players, 2, _DEMO_TOTAL_ROUNDS, seed=20261003, fair=True
    )
    # 第 1 局进行中：有双方阵容、没有比分，正是「正在打」的样子
    rounds[0].status = "live"
    for index, (score_a, score_b, winner) in enumerate(_DEMO_DONE_SCORES, start=1):
        rnd = rounds[index]
        rnd.status = "done"
        rnd.winner = winner
        rnd.sides[0].score = score_a
        rnd.sides[1].score = score_b
    return [rnd.dump() for rnd in rounds]


def demo_patch() -> dict[str, Any]:
    """整份演示数据的 patch（交给 ``store.update`` 深合并）。"""
    players = demo_players()
    return {
        "event": {"name": DEMO_EVENT_NAME, "status": "active"},
        "players": players,
        "participants": [p["id"] for p in players],
        "participantsSet": True,
        "rules": {
            "format": "league",
            "teamSize": 2,
            "totalRounds": _DEMO_TOTAL_ROUNDS,
            "fairRotation": True,
        },
        "rounds": demo_rounds(),
    }


async def ensure_demo(store) -> bool:
    """库还是空的时候写一份演示数据；返回是否真的写了。

    ``store`` 从外面传进来而不是 import：``app.store`` 会反向 import ``app.logic``，
    这里再 import 它就成环了。

    **只在「一个选手都没有」时动手**：有一点点真实数据就完全不碰——调试模式
    绝不该悄悄改掉别人正在用的库。
    """
    if not active():
        return False
    cfg = store.snapshot()
    if cfg.players or cfg.rounds:
        log.info("调试模式：库里已有数据，跳过演示数据")
        return False
    await store.update(demo_patch(), actor="debug:demo")
    fresh = store.snapshot()
    log.warning(
        "调试模式：已写入演示赛事 | 届=%s | 选手=%d | 局数=%d | 进行中=%d",
        fresh.event.name,
        len(fresh.players),
        len(fresh.rounds),
        sum(1 for r in fresh.rounds if r.status == "live"),
    )
    return True
