"""命令行入口：启动服务与维护工具。

不传参数即启动服务；``--reset-key`` 用于忘记管理 KEY 时在本机重置
（凭据就是「能访问服务器上的数据库文件」，这是自托管应用的常规做法）。

::

    uv run python -m app                     启动服务
    uv run python -m app --reset-key         重置管理 KEY（随机生成）
    uv run python -m app --reset-key 新KEY    重置管理 KEY（指定值）
    uv run python -m app --reset-key -e e002 指定某一届
"""

from __future__ import annotations

import os
import secrets
import socket
import sys

import uvicorn

from . import db
from .auth import sha256_hex
from .defaults import DEFAULT_ADMIN_KEY
from .logging_conf import get_logger, setup_logging
from .store import DB_PATH

log = get_logger("cli")
boot_log = get_logger("boot")

# 去掉易混淆的 I / O / 0 / 1
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_KEY_LEN = 6

USAGE = """NTE 比赛 · 命令行

  python -m app                          启动服务
  python -m app --reset-key [新KEY]       重置管理 KEY（省略则随机生成）
  python -m app --reset-key -e e002       重置指定届次的管理 KEY
  python -m app --help                   显示本帮助

忘记管理 KEY 时：先停止服务 → 执行 --reset-key → 用打印出的新 KEY 登录。
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
def serve() -> None:
    setup_logging()
    host = os.getenv("NTE_HOST", "0.0.0.0")
    port = int(os.getenv("NTE_PORT", "8000"))
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
# 重置管理 KEY
# --------------------------------------------------------------------------- #
def reset_admin_key(new_key: str | None = None, event_id: str | None = None) -> int:
    """把某一届的管理 KEY 重置为 sha256 存储的新值，返回进程退出码。"""
    if not DB_PATH.exists():
        print(f"未找到数据库: {DB_PATH}")
        print("请先执行 `uv run python -m app` 启动一次服务，让它初始化数据。")
        return 1

    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        events = db.list_events(conn)
        if not events:
            print("数据库中还没有任何届次，请先启动一次服务。")
            return 1

        target = (event_id or "").strip() or db.get_meta(conn, db.CURRENT_KEY) or events[0]["id"]
        entry = next((e for e in events if e["id"] == target), None)
        if entry is None:
            print(f"届次 {target} 不存在。现有届次: {', '.join(e['id'] for e in events)}")
            return 1

        key = (new_key or "").strip()
        generated = not key
        if generated:
            key = generate_key()
        if len(key) < MIN_KEY_LEN:
            print(f"管理 KEY 至少 {MIN_KEY_LEN} 位，请重新执行。")
            return 1

        conn.execute(
            "INSERT INTO event_admin (event_id, key, key_sha256) VALUES (?, '', ?) "
            "ON CONFLICT(event_id) DO UPDATE SET key = '', key_sha256 = excluded.key_sha256",
            (target, sha256_hex(key)),
        )

    log.warning("管理 KEY 已通过命令行重置 | 届=%s", target)
    line = "=" * 62
    print()
    print(line)
    print(f"  届次 {target}（{entry.get('name') or '未命名'}）的管理 KEY 已重置")
    print(f"  新 KEY（{'随机生成' if generated else '指定'}）：{key}")
    print("  存储方式：sha256 —— 数据库里不再保留明文")
    print(line)
    if _port_in_use(int(os.getenv("NTE_PORT", "8000"))):
        print("  ⚠ 检测到服务仍在运行：请先停止它再重新启动，")
        print("    否则内存里的旧配置可能在下一次改动时覆盖本次重置。")
    print("  下一步：重启服务 → 管理端 → 用上面的 KEY 登录")
    print(f"  安全提示：出厂 KEY 是 {DEFAULT_ADMIN_KEY}，请尽快改成自己的。")
    print(line)
    return 0


# --------------------------------------------------------------------------- #
# 参数分发
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    if args and args[0] == "--reset-key":
        rest = args[1:]
        event_id: str | None = None
        if "-e" in rest:
            pos = rest.index("-e")
            if pos + 1 < len(rest):
                event_id = rest[pos + 1]
            rest = rest[:pos] + rest[pos + 2 :]
        if rest and rest[0].startswith("-"):
            print(f"无法识别的参数: {' '.join(rest)}")
            print(USAGE)
            return 2
        return reset_admin_key(rest[0] if rest else None, event_id)

    if args:
        print(f"无法识别的参数: {' '.join(args)}")
        print(USAGE)
        return 2

    serve()
    return 0
