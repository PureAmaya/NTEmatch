"""届次的「举办者」：列表里要能直接显示**名字**。

存储层只存 ``ownerUid``，那串随机字符对用户说明不了任何问题——所以
``store.list_events()`` 负责配上成员显示名。这里把两种边界都钉住：
正常解析出名字；历史无主 / 成员已被删时留空（前端据此显示「服务器管理员」）。
"""

from __future__ import annotations

from app.models import Member
from app.store import ConfigStore


async def _store_with_admin(tmp_path) -> ConfigStore:
    """起一个干净的数据目录（自带那位唯一的服务器管理员）。"""
    st = ConfigStore(tmp_path / "events.sqlite")
    await st.start()
    return st


async def test_owner_name_is_resolved(tmp_path):
    st = await _store_with_admin(tmp_path)
    admin = st.server_admin()
    assert admin is not None  # 启动自检保证存在

    _, key, _ = await st.save_member(
        Member(uid="u_owner", name="小队长", permission="event_admin")
    )
    assert key  # 新成员会生成密钥（本次调用才拿得到）

    cfg = await st.create_event("小队长杯", owner_uid="u_owner")
    assert cfg.event.owner_uid == "u_owner"

    events = await st.list_events()
    hit = next(e for e in events if e["id"] == st.current_id)
    assert hit["ownerName"] == "小队长"


async def test_owner_name_is_blank_for_ownerless_event(tmp_path):
    """历史数据（无主）与「成员被删」都留空，前端据此显示服务器管理员。"""
    st = await _store_with_admin(tmp_path)
    # 起始那一届是启动自动建的，没有归属
    first = (await st.list_events())[0]
    assert first["ownerUid"] == ""
    assert first["ownerName"] == ""

    # 指向一个不存在的成员：同样留空，而不是抛异常
    await st.create_event("孤儿届", owner_uid="u_已删除的人")
    hit = next(e for e in await st.list_events() if e["name"] == "孤儿届")
    assert hit["ownerUid"] == "u_已删除的人"
    assert hit["ownerName"] == ""
