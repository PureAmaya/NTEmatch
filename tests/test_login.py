"""登录与权限：只有「成员密钥」这一条路。

背景：主管理 KEY 已经退休——它是**按届存储**的，换来的却是**全局**服务器管理员会话，
于是任何一届遗留的出厂值（``NTE-ADMIN``）都等于一把万能钥匙。现在登录只认成员密钥，
能做什么完全由登录后的身份决定：

* 赛事管理员：增减赛事，管**自己办的**那几届；
* 服务器管理员：增减赛事，管**全部**届。

下面把这几条钉成测试，免得哪天又长出一把「谁的届都能开」的钥匙。
"""

from __future__ import annotations

import httpx
import pytest

from app import cli, db
from app.auth import hash_secret, verify_secret
from app.defaults import default_config
from app.main import app
from app.models import Config, Member
from app.store import ConfigStore, store


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    )


def _detail(body: dict) -> str:
    """接口统一把 HTTPException 包成 {ok, error, status}；两种字段名都认。"""
    return str(body.get("detail") or body.get("error") or "")


async def _ready() -> None:
    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()


async def test_member_key_login_carries_identity():
    """成员密钥登录：会话带上 uid 与权限，前端据此决定显示哪些入口。"""
    await _ready()
    saved, key, _bearer = await store.save_member(
        Member(name="测试赛事管理员", permission="event_admin")
    )
    try:
        async with _client() as c:
            res = await c.post("/api/auth", json={"key": key})
        assert res.status_code == 200
        body = res.json()
        assert body["uid"] == saved.uid
        assert body["permission"] == "event_admin"
        assert body["token"]
    finally:
        await store.delete_member(saved.uid)


async def test_server_admin_logs_in_with_its_own_member_key():
    """服务器管理员也只是「权限最高的成员」：用自己的成员密钥登录，而不是什么主 KEY。"""
    await _ready()
    admin = store.server_admin()
    assert admin is not None, "启动自检应保证服务器管理员成员存在"
    _saved, key, _bearer = await store.save_member(admin, new_key=True)

    async with _client() as c:
        res = await c.post("/api/auth", json={"key": key})
    assert res.status_code == 200
    body = res.json()
    assert body["permission"] == "server_admin"
    assert body["uid"] == admin.uid


@pytest.mark.parametrize("legacy", ["NTE-ADMIN", " my-own-old-admin-key "])
async def test_retired_admin_key_is_rejected(legacy):
    """出厂值与自定义值都不再是凭据——整套主管理 KEY 都退休了。

    这一条是本次改动的核心保证：老文档里那把 ``NTE-ADMIN`` 从此对任何库都无效，
    哪怕库里还留着它的哈希（旧库的 ``event_admin`` 表启动时会被删掉）。
    """
    await _ready()
    async with _client() as c:
        res = await c.post("/api/auth", json={"key": legacy})
    assert res.status_code == 401
    assert "成员密钥" in _detail(res.json())


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_key_never_logs_in(blank):
    """空密钥不能因为「配置里也是空」而蒙对——那是所有人闭着眼睛都能过的门。"""
    await _ready()
    async with _client() as c:
        res = await c.post("/api/auth", json={"key": blank})
    assert res.status_code == 401


async def test_admin_key_endpoint_answers_clearly_after_retirement():
    """老前端 / 老脚本调 ``/api/admin/key``：要得到「已移除，用成员密钥」这句答复，
    而不是一个让人以为地址写错了的 404。"""
    async with _client() as c:
        res = await c.post("/api/admin/key", json={"key": "whatever"})
    assert res.status_code == 410
    assert "成员" in _detail(res.json())


async def test_cli_reset_key_rotates_the_server_admin_member(tmp_path, monkeypatch):
    """忘记密钥时的找回路径：``--reset-key`` 轮换服务器管理员成员的密钥。

    它必须是「能碰到数据库文件就能恢复」——否则唯一那位管理员丢了密钥就再也进不去。
    """
    path = tmp_path / "reset.sqlite"
    db.init_db(path)
    st = ConfigStore(path)
    await st.start()  # 启动自检会创建服务器管理员
    admin = st.server_admin()
    assert admin is not None

    # 先把密钥改成一个已知的旧值，才能验证「旧值确实失效了」
    with db.connect(path) as conn:
        conn.execute(
            "UPDATE members SET key_hash = ?, key_sha256 = 'legacy-sha256' WHERE uid = ?",
            (hash_secret("old-key-123"), admin.uid),
        )
        conn.commit()
    await st.stop()

    monkeypatch.setattr(cli, "DB_PATH", path)
    assert cli.reset_admin_key("new-key-456") == 0

    with db.connect(path) as conn:
        row = next(m for m in db.list_members(conn) if m["uid"] == admin.uid)
    assert verify_secret("new-key-456", row["keyHash"]) is True, "新密钥应当能登录"
    assert verify_secret("old-key-123", row["keyHash"]) is False, "旧密钥必须失效"
    assert row["keySha256"] == "", "历史无盐列要一并清掉，避免两套并存"


def test_cli_rejects_the_removed_event_option(capsys):
    """``--reset-key -e e002``（按届重置）已经不存在：明确报错，不要默默忽略。"""
    assert cli.main(["--reset-key", "-e", "e002"]) == 2
    assert "届次无关" in capsys.readouterr().out


def test_config_no_longer_carries_an_admin_key():
    """配置模型里没有 ``admin`` 字段了：读回旧库也不会再冒出一把「主 KEY」。

    这条挡住的是「删了接口、忘了删字段」——那会让老 KEY 悄悄留在导出与备份里。
    """
    cfg = Config.model_validate(default_config())
    assert "admin" not in cfg.dump()


async def test_legacy_database_drops_the_admin_key_table(tmp_path):
    """旧库升级：``event_admin`` 表在启动时被删掉。

    真实部署里这张表是存在的（还可能写着出厂的 ``NTE-ADMIN``）；留着它没有任何代码
    会读，唯一的风险是将来被谁误读回去。所以启动时直接删表。
    """
    path = tmp_path / "legacy.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute(
            "CREATE TABLE event_admin ("
            " event_id TEXT PRIMARY KEY, key TEXT NOT NULL DEFAULT '',"
            " key_sha256 TEXT NOT NULL DEFAULT '', key_hash TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO event_admin VALUES ('e001', 'NTE-ADMIN', '', '')")
        conn.commit()

    db.init_db(path)  # = 服务启动时走的那条路

    with db.connect(path) as conn:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "event_admin" not in tables
