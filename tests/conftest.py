"""测试公共设施。

**首要任务是隔离数据**：整套测试跑在一个临时数据目录里（``NTE_DATA_DIR``），
绝不会碰到仓库里的 ``config/nte.sqlite3``。环境变量必须在导入 ``app.*`` 之前
设好，所以这段代码放在 conftest 顶层——pytest 保证它先于任何测试模块执行。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("NTE_DATA_DIR", tempfile.mkdtemp(prefix="nte-test-"))
# 命令行输出的彩色一律关掉：断言要读的是文字，转义码混进去只会让
# 正则（例如从 ``--transfer-admin`` 的输出里抠密钥）把颜色码一起吞掉。
# 用 setdefault：谁真想看彩色（`NO_COLOR= pytest`）也能自己开。
os.environ.setdefault("NO_COLOR", "1")

import httpx
import pytest

from app import db
from app.auth import auth
from app.defaults import default_config
from app.main import app
from app.models import Config, Round, SetScore, Side, Team
from app.store import store


@pytest.fixture
def make_config():
    """造一份「两队 / 一场」的最小配置，赛制参数逐项可覆盖。

    默认是锦标赛制、自然数 + 数值高胜、单场小组赛——这样单个测试只声明它关心的
    那一项，其余保持出厂值，改动影响面一眼可见。
    """

    def build(
        *,
        value_type: str = "integer",
        value_label: str = "",
        better: str = "high",
        sets=(),
        fmt: str = "tournament",
        teams: int = 2,
    ) -> Config:
        base = default_config()
        cfg = Config.model_validate(
            {
                **base,
                "rules": {
                    **base["rules"],
                    "format": fmt,
                    # 走一遍校验器：三件套与旧口径 metric 会一起规整成一致状态
                    "valueType": value_type,
                    "valueLabel": value_label,
                    "better": better,
                    "metric": "time" if value_type == "time" else "score",
                },
            }
        )
        cfg.teams = [Team(id=f"t{i}", label=f"{i} 队", group="A") for i in range(1, teams + 1)]
        sides = [
            Side(key=chr(64 + i), label=f"{i} 队", team_id=f"t{i}") for i in range(1, teams + 1)
        ]
        rnd = Round(code="G-1", label="第 1 场", stage="group", sides=sides)
        rnd.sets = [SetScore(a=a, b=b) for a, b in sets]
        cfg.rounds = [rnd]
        return cfg

    return build


@pytest.fixture
async def client():
    """不带身份的客户端（走 ASGI，不启端口）。"""
    db.init_db(store._db_path)  # 正常由启动流程触发；测试里补一次，保证表都在
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nte.test") as c:
        yield c


@pytest.fixture
async def admin_client():
    """带服务器管理员会话的客户端（``X-NTE-Token``）。"""
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    session = auth.issue("test-admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://nte.test",
        headers={"X-NTE-Token": session.token},
    ) as c:
        yield c
