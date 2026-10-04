"""命令行入口：启动服务与维护工具。

不传参数即启动服务；``--reset-key`` / ``--transfer-admin`` 是两条**只能在本机做**的
维护路径（凭据就是「能访问服务器上的数据库文件」，这是自托管应用的常规做法）：

* ``--reset-key``：服务器管理员**忘了密钥**时重置；
* ``--transfer-admin``：**换人**——把服务器管理员交给另一位成员，旧管理员删除或降级，
  并给接任者派发新密钥（界面上做不到：既不能提升别人，也不能降级唯一的管理员）。

::

    uv run python -m app                     启动服务
    uv run python -m app --reset-key         重置服务器管理员密钥（随机生成）
    uv run python -m app --reset-key 新密钥   重置服务器管理员密钥（指定值）
    uv run python -m app --transfer-admin    交接服务器管理员（交互式；也可带目标与开关）
"""

from __future__ import annotations

import os
import secrets
import socket
import sys

import uvicorn

from . import db
from .auth import hash_secret
from .logging_conf import get_logger, setup_logging
from .store import DB_PATH, now_iso

log = get_logger("cli")
boot_log = get_logger("boot")

# 去掉易混淆的 I / O / 0 / 1
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_KEY_LEN = 6

USAGE = """NTE 比赛 · 命令行

  python -m app                            启动服务（默认 0.0.0.0:8000）
  python -m app --port 8123                换个端口启动
  python -m app --host 127.0.0.1 -p 8123   同时指定监听地址
  python -m app --reset-key [新密钥]        重置服务器管理员的登录密钥（省略则随机生成）
  python -m app --transfer-admin [目标]     交接服务器管理员：把「目标」设为管理员，
                                           旧管理员删除或降级，并给新管理员派发新密钥
                                           目标可写 uid / QQ / 名字片段；不给就交互式选择
                                            --delete-old 删掉旧账号 / --keep-old 降级（默认）
                                            --key 新密钥 指定新密钥
  python -m app --help                     显示本帮助

端口与监听地址也可以走环境变量 NTE_PORT / NTE_HOST，命令行参数优先。
忘记服务器管理员密钥时：先停止服务 → 执行 --reset-key → 用打印出的新密钥登录。
要换人时：先停止服务 → 执行 --transfer-admin → 用新管理员的密钥登录。
"""


def generate_key(groups: int = 3, size: int = 4) -> str:
    """生成形如 ``ABCD-EFGH-JKLM`` 的易读随机 KEY。"""
    return "-".join("".join(secrets.choice(_KEY_ALPHABET) for _ in range(size)) for _ in range(groups))


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """粗略判断服务是否仍在运行（避免重置被内存中的旧配置覆盖）。"""
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 启动服务
# --------------------------------------------------------------------------- #
def serve(host: str | None = None, port: int | None = None) -> None:
    setup_logging()
    # 命令行参数优先，其次环境变量，最后默认值
    host = host or os.getenv("NTE_HOST", "0.0.0.0")
    port = int(port if port is not None else os.getenv("NTE_PORT", "8000"))
    reload_enabled = os.getenv("NTE_RELOAD", "0").lower() in {"1", "true", "yes", "on"}
    log_level = (os.getenv("NTE_LOG_LEVEL") or "info").lower()
    workers = 1 if reload_enabled else int(os.getenv("NTE_WORKERS", "1"))

    boot_log.info(
        "启动参数 | host=%s | port=%d | reload=%s | workers=%d", host, port, reload_enabled, workers
    )
    if workers > 1:
        boot_log.warning("多进程模式下 WebSocket 广播不跨进程，建议使用单进程或外置消息总线")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        workers=workers,
        log_level=log_level,
        access_log=False,
        # 状态推送对压缩敏感（跨境链路），显式开启 WebSocket 压缩
        ws_per_message_deflate=True,
        ws_ping_interval=25.0,
        ws_ping_timeout=25.0,
        timeout_keep_alive=30,
        server_header=False,
        date_header=False,
    )


# --------------------------------------------------------------------------- #
# 重置服务器管理员密钥
# --------------------------------------------------------------------------- #
def reset_admin_key(new_key: str | None = None) -> int:
    """重置「服务器管理员」这位成员的登录密钥，返回进程退出码。

    为什么必须留着它：登录**只认成员密钥**，而全站只有一位服务器管理员——他把密钥
    弄丢就再没人能进管理端补发密钥。所以留一条「能碰到数据库文件的人可以重置」的路，
    这是自托管应用的常规做法（凭据 = 对数据库文件的访问权）。

    重置的是**成员凭据**，与届次无关：主管理 KEY 那套（按届存储、全局生效）已经退休。
    """
    if not DB_PATH.exists():
        print(f"未找到数据库: {DB_PATH}")
        print("请先执行 `uv run python -m app` 启动一次服务，让它初始化数据。")
        return 1

    key = _clean(new_key)
    generated = not key
    if generated:
        key = generate_key()
    if len(key) < MIN_KEY_LEN:
        print(f"密钥至少 {MIN_KEY_LEN} 位，请重新执行。")
        return 1

    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        members = db.list_members(conn)
        admin = next((m for m in members if m["permission"] == "server_admin"), None)
        if admin is None:
            print("数据库里还没有服务器管理员成员。")
            print("请先执行 `uv run python -m app` 启动一次服务（启动时会自动创建并打印密钥）。")
            return 1
        # 与「成员管理 → 轮换密钥」写同样的格式：加盐哈希，并清掉历史无盐列（不留两套）
        conn.execute(
            "UPDATE members SET key_hash = ?, key_sha256 = '', updated_at = ? WHERE uid = ?",
            (hash_secret(key), now_iso(), admin["uid"]),
        )
        name = admin["name"] or "服务器管理员"

    log.warning("服务器管理员密钥已通过命令行重置 | uid=%s", admin["uid"])
    line = "=" * 62
    print()
    print(line)
    print(f"  服务器管理员「{name}」的登录密钥已重置")
    print(f"  新密钥（{'随机生成' if generated else '指定'}）：{key}")
    print("  存储方式：加盐 PBKDF2 —— 数据库里不再保留明文")
    print(line)
    if _port_in_use(int(os.getenv("NTE_PORT", "8000"))):
        print("  ⚠ 检测到服务仍在运行：请先停止它再重新启动，")
        print("    否则内存里的旧成员数据可能在下一次改动时覆盖本次重置。")
    print("  下一步：重启服务 → 用上面的密钥登录（/admin 或 /user）")
    print("  登录后可在「服务器 → 成员管理」里轮换密钥 / 令牌。")
    print(line)
    return 0


# --------------------------------------------------------------------------- #
# 交接服务器管理员
# --------------------------------------------------------------------------- #
_PERMISSION_LABEL = {"server_admin": "服务器管理员", "event_admin": "赛事管理员", "member": "成员"}


def _clean(text: str) -> str:
    """去掉首尾空白与 BOM。

    为什么要管 BOM：把答案**管道喂给** ``python -m app``（脚本 / ``echo`` / PowerShell
    的 ``|``）时，Windows 会在开头塞一个 ``\\ufeff``，``"2"`` 会变成 ``"﻿2"``——
    不清理的话「选第 2 位」会被判成「没找到成员」。
    """
    return (text or "").replace("\ufeff", "").strip()


def _match_members(members: list[dict], token: str) -> list[dict]:
    """按 uid / QQ / 名字片段 / 列表序号找成员，返回全部命中（唯一性交给调用方判断）。"""
    raw = _clean(token)
    if not raw:
        return []
    if raw.isdigit():  # 交互列表里打印的序号
        index = int(raw)
        if 1 <= index <= len(members):
            return [members[index - 1]]
    low = raw.lower()
    for key in ("uid", "qq"):
        hits = [m for m in members if str(m.get(key) or "").strip().lower() == low]
        if hits:
            return hits
    return [m for m in members if low in str(m.get("name") or "").lower()]


def _print_member_list(members: list[dict]) -> None:
    """编号 + 名字 + 权限 + QQ + uid（编号可直接用于下一步选择）。"""
    for index, member in enumerate(members, 1):
        label = _PERMISSION_LABEL.get(str(member.get("permission")), str(member.get("permission")))
        marks = []
        if member.get("permission") == "server_admin":
            marks.append("当前管理员")
        if not member.get("active", True):
            marks.append("已停用")
        tail = f"  ← {' · '.join(marks)}" if marks else ""
        print(
            f"  {index:>2}. {member.get('name') or '(未命名)'}  [{label}]  "
            f"QQ={member.get('qq') or '—'}  uid={member['uid']}{tail}"
        )


def transfer_admin(target: str = "", *, mode: str = "", new_key: str | None = None, ask=input) -> int:
    """把「服务器管理员」交给另一位成员：旧管理员**删除或降级**，并给接任者派发新密钥。

    为什么只能在本机做：全站有且只有一个服务器管理员，界面上既不能把别人提升上来、
    也不能把唯一的管理员降级 / 删除（见 ``app/members.py`` 里那三处 400）。所以「换人」
    这条路留在命令行：**凭据 = 能碰到数据库文件**，和 ``--reset-key`` 同一档权限。

    * ``target``：接任者（uid / QQ / 名字片段 / 列表序号）；留空则交互选择；
    * ``mode``：``demote``（旧管理员降级为赛事管理员）/ ``delete``（删掉旧账号）；留空则交互询问；
    * ``new_key``：指定新密钥（默认随机生成）；
    * ``ask``：问答用的函数（测试里注入脚本化输入）。
    """
    if mode and mode not in {"demote", "delete"}:
        print(f"未知的处置方式: {mode}（只能是 demote / delete）")
        return 2
    if not DB_PATH.exists():
        print(f"未找到数据库: {DB_PATH}")
        print("请先执行 `uv run python -m app` 启动一次服务，让它初始化数据。")
        return 1

    interactive = not (target and mode)
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        members = db.list_members(conn)
    if not members:
        print("数据库里还没有成员。请先启动一次服务（会自动创建服务器管理员）。")
        return 1

    old = next((m for m in members if m.get("permission") == "server_admin"), None)

    if not target:
        print()
        print("现有成员：")
        _print_member_list(members)
        print()
        target = _clean(ask("把哪一位设为新的服务器管理员？（填序号 / uid / QQ / 名字）："))

    hits = _match_members(members, target)
    if not hits:
        print(f"没找到成员「{target}」：可以填序号、uid、QQ 或名字的一部分。")
        return 1
    if len(hits) > 1:
        print(f"「{target}」匹配到 {len(hits)} 位，请写得更具体：")
        _print_member_list(hits)
        return 1
    new_admin = hits[0]
    old_name = (old or {}).get("name") or "（当前没有管理员）"
    if old is not None and new_admin["uid"] == old["uid"]:
        print(f"「{new_admin.get('name') or new_admin['uid']}」已经是服务器管理员了，无需交接。")
        return 1

    if not mode:
        print()
        print(f"当前服务器管理员：{old_name}")
        print("他之后怎么处理？")
        print("  [1] 降级为赛事管理员（保留账号，默认）")
        print("  [2] 删除账号")
        answer = _clean(ask("请选择 [1/2]：")).lower()
        mode = "delete" if answer in {"2", "delete", "删除"} else "demote"

    if interactive:
        action = "删除账号" if mode == "delete" else "降级为赛事管理员"
        confirm = _clean(
            ask(
                f"确认：把「{new_admin.get('name') or new_admin['uid']}」设为服务器管理员，"
                f"并把「{old_name}」{action}？[y/N]："
            )
        ).lower()
        if confirm not in {"y", "yes", "是", "确认"}:
            print("已取消。")
            return 1

    key = _clean(new_key)
    generated = not key
    if generated:
        key = generate_key()
    if len(key) < MIN_KEY_LEN:
        print(f"密钥至少 {MIN_KEY_LEN} 位，请重新执行。")
        return 1

    stamp = now_iso()
    with db.connect(DB_PATH) as conn:
        if old is not None:
            if mode == "delete":
                conn.execute("DELETE FROM members WHERE uid = ?", (old["uid"],))
            else:
                conn.execute(
                    "UPDATE members SET permission = 'event_admin', updated_at = ? WHERE uid = ?",
                    (stamp, old["uid"]),
                )
        # 接任者：升为管理员、一并启用（停用的账号不启用就登不进去）、换掉密钥
        conn.execute(
            "UPDATE members SET permission = 'server_admin', active = 1,"
            " key_hash = ?, key_sha256 = '', updated_at = ? WHERE uid = ?",
            (hash_secret(key), stamp, new_admin["uid"]),
        )
    log.warning(
        "服务器管理员已交接 | 新=%s | 旧=%s(%s)",
        new_admin["uid"],
        (old or {}).get("uid") or "无",
        mode,
    )

    line = "=" * 62
    print()
    print(line)
    print(f"  服务器管理员已交给「{new_admin.get('name') or new_admin['uid']}」")
    print(f"  新登录密钥（{'随机生成' if generated else '指定'}）：{key}")
    print("  存储方式：加盐哈希 —— 数据库里不再保留明文")
    if old is not None:
        if mode == "delete":
            print(f"  旧管理员「{old_name}」的账号已删除")
            print("  （他创办的届会变成「无主」，由服务器管理员接管；比赛内容不受影响）")
        else:
            print(f"  旧管理员「{old_name}」已降级为赛事管理员（他创办的届仍归他管）")
    print(line)
    if _port_in_use(int(os.getenv("NTE_PORT", "8000"))):
        print("  ⚠ 检测到服务仍在运行：请先停止它再重新启动，")
        print("    否则内存里的旧成员数据可能在下一次改动时覆盖本次交接。")
    print("  下一步：重启服务 → 用上面的新密钥登录（/admin 或 /user）")
    print("  重启后所有在线会话失效——旧管理员 / 被删成员的登录状态一并作废。")
    print(line)
    return 0


# --------------------------------------------------------------------------- #
# 参数分发
# --------------------------------------------------------------------------- #
def _take_option(args: list[str], *names: str) -> tuple[str | None, list[str]]:
    """取出 ``--name 值`` / ``--name=值`` 形式的选项，返回（值, 剩余参数）。

    位置上随意（放在最后也行）；没出现则返回 ``None``。
    """
    rest: list[str] = []
    found: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        hit = next((n for n in names if arg == n or arg.startswith(f"{n}=")), None)
        if hit is None:
            rest.append(arg)
            index += 1
            continue
        if "=" in arg:
            found = arg.split("=", 1)[1]
            index += 1
        else:
            found = args[index + 1] if index + 1 < len(args) else ""
            index += 2
    return found, rest


def _drop_flag(args: list[str], *names: str) -> tuple[bool, list[str]]:
    """去掉布尔开关（``--xxx`` 这种不带值的），返回（是否出现过, 剩余参数）。"""
    seen = False
    rest: list[str] = []
    for arg in args:
        if arg in names:
            seen = True
            continue
        rest.append(arg)
    return seen, rest


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    port_raw, args = _take_option(args, "--port", "-p")
    host_raw, args = _take_option(args, "--host")
    port: int | None = None
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            print(f"端口不合法: {port_raw}（应为一个 1~65535 的整数）")
            return 2
        if not 0 < port < 65536:
            print(f"端口超出范围: {port}")
            return 2

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    if args and args[0] == "--reset-key":
        rest = args[1:]
        if "-e" in rest:
            # 以前可以「重置某一届的管理 KEY」；主管理 KEY 退休后这个参数没有意义了，
            # 明确报错比默默忽略好——否则用户会以为重置没生效。
            print("--reset-key 现在重置的是「服务器管理员」成员的登录密钥，与届次无关，")
            print("不再接受 -e <届次> 参数。直接执行 `python -m app --reset-key` 即可。")
            return 2
        if rest and rest[0].startswith("-"):
            print(f"无法识别的参数: {' '.join(rest)}")
            print(USAGE)
            return 2
        return reset_admin_key(rest[0] if rest else None)

    if args and args[0] == "--transfer-admin":
        rest = args[1:]
        key_raw, rest = _take_option(rest, "--key")
        delete_old, rest = _drop_flag(rest, "--delete-old")
        keep_old, rest = _drop_flag(rest, "--keep-old")
        if delete_old and keep_old:
            print("--delete-old 与 --keep-old 只能给一个。")
            return 2
        target = ""
        if rest and not rest[0].startswith("-"):
            target = rest.pop(0)
        if rest:
            print(f"无法识别的参数: {' '.join(rest)}")
            print(USAGE)
            return 2
        mode = "delete" if delete_old else ("demote" if keep_old else "")
        return transfer_admin(target, mode=mode, new_key=key_raw)

    if args:
        print(f"无法识别的参数: {' '.join(args)}")
        print(USAGE)
        return 2

    serve(host=host_raw, port=port)
    return 0
