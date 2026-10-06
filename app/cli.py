"""命令行入口：启动服务与维护工具。

不传参数即启动服务；``--reset-key`` / ``--transfer-admin`` 是两条**只能在本机做**的
维护路径（凭据就是「能访问服务器上的数据库文件」，这是自托管应用的常规做法）：

* ``--reset-key``：服务器管理员**忘了密钥**时重置；
* ``--transfer-admin``：**换人**——把服务器管理员交给另一位成员，旧管理员删除或降级，
  并给接任者派发新密钥（界面上做不到：既不能提升别人，也不能降级唯一的管理员）。

另外，``--help`` 末段列出了配套脚本（生成分享图 / **QQ 机器人帮助图**、前端资源自检）——
那两个图片脚本与运行无关，Pillow 只在生成时需要。

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

from . import db, hot
from .auth import hash_secret
from .console import pad, paint, width, wrap
from .logging_conf import get_logger, setup_logging
from .store import DB_PATH, now_iso

log = get_logger("cli")
boot_log = get_logger("boot")

# 去掉易混淆的 I / O / 0 / 1
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_KEY_LEN = 6

# --------------------------------------------------------------------------- #
# 帮助（--help）
#
# 内容**结构化**（分组 + 条目），排版与上色交给 app/console。不写成一个长字符串是
# 因为：想把「命令名」高亮就得在字符串里塞 ANSI 码，而帮助恰恰是最常被
# `python -m app --help > help.txt`、贴进聊天框的东西——那样会变成一串乱码。
#
# 文字里**不出现 Markdown 记号**：终端不会把 `**加粗**` 渲染成粗体，只会原样印出
# 星号（用户直接提过这一条），所以强调靠用词，高亮靠颜色。
# --------------------------------------------------------------------------- #
_HELP: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "启动服务",
        (
            ("python -m app", "启动服务。默认就是热更新守护：改了代码或 git pull 都能不中断地换上新的"),
            ("python -m app --port 8123", "换个端口启动"),
            ("python -m app --host 127.0.0.1 -p 8123", "同时指定监听地址与端口"),
            (
                "python -m app --no-hotrun",
                "单进程启动，没有热更新能力（调试、或把进程交给外部工具守护时用；也可设 NTE_HOT=0）",
            ),
            (
                "python -m app hotrun",
                "明确指定热更新守护（默认就是它）。守护参数：--interval / --no-watch / "
                + "--hold-socket / --no-hold-socket / --no-deps-sync",
            ),
            ("python -m app --help", "显示本帮助"),
        ),
    ),
    (
        "启动前自检",
        (
            (
                "python -m app --check",
                "只导入代码、不起服务、不占端口；守护每次换代前也会自动跑一次",
            ),
        ),
    ),
    (
        "本机维护（凭据 = 能碰到服务器上的数据库文件，所以只能在本机执行）",
        (
            ("python -m app --reset-key [新密钥]", "重置服务器管理员的登录密钥；省略则随机生成并打印出来"),
            (
                "python -m app --transfer-admin [目标]",
                "交接服务器管理员。目标可填 uid / QQ / 名字片段，不给就交互式选择；"
                + "另有 --delete-old（把旧管理员删号）/ --keep-old（降级，默认）/ "
                + "--key <新密钥>（指定接任者的密钥）",
            ),
        ),
    ),
    (
        "环境变量",
        (
            ("NTE_HOST / NTE_PORT", "监听地址与端口（命令行参数优先）"),
            ("NTE_DATA_DIR", "数据目录：数据库、备份、上传的图片都在它下面"),
            ("NTE_HOT=0", "退回单进程启动（等价于 --no-hotrun）"),
            ("NTE_LOG_LEVEL", "日志级别，默认 info"),
        ),
    ),
    (
        "常见操作",
        (
            ("忘了管理员密钥", "先停止服务 → python -m app --reset-key → 用打印出来的新密钥登录"),
            ("要换人", "先停止服务 → python -m app --transfer-admin → 用新管理员的密钥登录"),
            (
                "看热更新的状态",
                "启动时会打印自检（换代方式 / 端口 / 仓库 / 依赖 / 预检）；"
                + "站点里「服务器 → 热更新」同样能看到，换代结果两处都会显示",
            ),
        ),
    ),
    (
        "配套脚本（与运行无关，按需执行；Pillow 只在生成图片时需要，不进运行依赖）",
        (
            (
                "uv run --with pillow python tools/make_help_card.py",
                "重画 QQ 机器人帮助图 → static/help.jpg。服务启动时本来就会自动重画一份，"
                + "这个脚本只是让你立刻看一眼新版式、或把图输出到别处；"
                + "图上的文字在 app/helpcard_content.py，排版在 app/helpcard.py",
            ),
            (
                "uv run --with pillow python tools/make_share_card.py",
                "重画默认分享图 → static/og.png（改了主色之后重跑一次）",
            ),
            (
                "uv run python tools/check_assets.py",
                "前端静态资源自检（CSS 括号配平 / 相对导入 / 图标名 / 引用到的资源），CI 也跑",
            ),
        ),
    ),
)

#: 帮助正文的排版宽度。**不跟随终端实际宽度**：CI / 日志里读到的列数不可信，
#: 固定宽度至少在哪儿看都是同一个版式。
_HELP_WIDTH = 104


def usage_text() -> str:
    """渲染帮助：分组标题、命令名、说明各用一种颜色，说明过长自动折行对齐。

    列宽一律按**终端显示宽度**算（``console.width``）——中文与英文混排时
    ``len()`` 会把中文算成一列，看着就是歪的。
    """
    names = [name for _, items in _HELP for name, _ in items]
    name_col = min(max(width(name) for name in names), 38)
    desc_col = name_col + 4  # 两格缩进 + 名字列 + 两格间距
    body_width = max(48, _HELP_WIDTH - desc_col)
    lines = [paint("NTE 比赛 · 命令行", "head", "bold"), ""]
    for title, items in _HELP:
        lines.append(paint(title, "head", "bold"))
        for name, desc in items:
            wrapped = wrap(desc, body_width)
            # 含中文的条目是「要做的操作」而不是可抄的命令，换个颜色区分开
            style = "accent" if any("\u4e00" <= ch <= "\u9fff" for ch in name) else "cmd"
            if width(name) <= name_col:
                lines.append(f"  {paint(pad(name, name_col), style)}  {wrapped[0]}")
            else:
                # 太长的名字（配套脚本那种）独占一行，说明缩进到同一列，别挤在一起
                lines.append(f"  {paint(name, style)}")
                lines.append(" " * desc_col + wrapped[0])
            lines.extend(" " * desc_col + part for part in wrapped[1:])
        lines.append("")
    lines.append(paint("端口与监听地址也可以走环境变量（命令行参数优先）；更细的说明见 README。", "note"))
    return "\n".join(lines).rstrip() + "\n"


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
def check_startup() -> int:
    """``python -m app --check``：**只体检、不起服务**（热更新换代前跑一次）。

    它做的事只有一件：把 :mod:`app.main` 导入起来。语法错、依赖没装、模块装配失败
    都会在这一步炸出来，而**它不碰数据库、不占端口、不写任何东西**——所以能在旧进程
    还在服务的时候安全地跑，用来把「坏版本」挡在换代之前（见 :func:`app.hotrun.Supervisor._preflight`）。
    """
    # 兜住一切：预检要的就是「能不能起来」这个答案是或否，具体是哪个异常由下面打印出来
    try:
        from .main import app  # noqa: F401  (导入即体检：语法 / 依赖 / 装配)
    except Exception as exc:  # noqa: BLE001  (预检的答案就是「起不起得来」，异常类型不重要)
        import traceback

        traceback.print_exc()
        print(paint(f"预检失败：{exc.__class__.__name__}: {exc}", "err", "bold"))
        return 1
    print(paint("预检通过", "ok", "bold"))
    return 0


def serve(host: str | None = None, port: int | None = None) -> None:
    setup_logging()
    # 命令行参数优先，其次环境变量，最后默认值
    host = host or os.getenv("NTE_HOST", "0.0.0.0")
    port = int(port if port is not None else os.getenv("NTE_PORT", "8000"))
    reload_enabled = _reload_requested()
    log_level = (os.getenv("NTE_LOG_LEVEL") or "info").lower()
    workers = 1 if reload_enabled else int(os.getenv("NTE_WORKERS", "1"))

    # 被热更新守护（app/hotrun.py）拉起来时，端口由**父进程**持有：
    # 这里接管那个已经 listen 好的套接字，不再自己 bind——换代时新老两个进程
    # 同时 accept 同一个套接字，所以中间没有空窗（见 app/hot.py 的说明）。
    adopted = hot.adopt_socket()

    boot_log.info(
        "启动参数 | host=%s | port=%d | reload=%s | workers=%d | 套接字=%s",
        host,
        port,
        reload_enabled,
        workers,
        "继承自热更新守护" if adopted is not None else "自行监听",
    )
    if workers > 1:
        boot_log.warning("多进程模式下 WebSocket 广播不跨进程，建议使用单进程或外置消息总线")

    options: dict[str, object] = {
        "log_level": log_level,
        "access_log": False,
        # 状态推送对压缩敏感（跨境链路），显式开启 WebSocket 压缩
        "ws_per_message_deflate": True,
        "ws_ping_interval": 25.0,
        "ws_ping_timeout": 25.0,
        "timeout_keep_alive": 30,
        "server_header": False,
        "date_header": False,
    }
    if adopted is not None:
        # ``Server.run(sockets=[...])``：uvicorn 直接用这个套接字服务（host/port 由父进程定）
        if os.name == "nt" and hot.use_selector_loop():
            # Windows 的默认循环（proactor）接不了**继承来的**套接字（IOCP 注册报
            # WinError 87），热更新换代时会「爬起来却收不到请求」。这里把策略换成
            # selector，并让 uvicorn 用当前策略建循环（loop="none" = 不指定工厂）。
            # 只影响 Windows 本机开发；Linux 生产上不存在这个问题。
            options["loop"] = "none"
            boot_log.info("接管套接字的进程改用 selector 事件循环（Windows 的已知限制）")
        server = uvicorn.Server(uvicorn.Config("app.main:app", **options))
        server.run(sockets=[adopted])
        return
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        workers=workers,
        **options,
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
        print(paint(f"未找到数据库：{DB_PATH}", "err"))
        print("请先执行 " + paint("uv run python -m app", "cmd") + " 启动一次服务，让它初始化数据。")
        return 1

    key = _clean(new_key)
    generated = not key
    if generated:
        key = generate_key()
    if len(key) < MIN_KEY_LEN:
        print(paint(f"密钥至少 {MIN_KEY_LEN} 位，请重新执行。", "err"))
        return 1

    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        members = db.list_members(conn)
        admin = next((m for m in members if m["permission"] == "server_admin"), None)
        if admin is None:
            print(paint("数据库里还没有服务器管理员成员。", "err"))
            print(
                "请先执行 "
                + paint("uv run python -m app", "cmd")
                + " 启动一次服务（启动时会自动创建并打印密钥）。"
            )
            return 1
        # 与「成员管理 → 轮换密钥」写同样的格式：加盐哈希，并清掉历史无盐列（不留两套）
        conn.execute(
            "UPDATE members SET key_hash = ?, key_sha256 = '', updated_at = ? WHERE uid = ?",
            (hash_secret(key), now_iso(), admin["uid"]),
        )
        name = admin["name"] or "服务器管理员"

    log.warning("服务器管理员密钥已通过命令行重置 | uid=%s", admin["uid"])
    line = paint("─" * 62, "rule")
    print()
    print(line)
    print(paint(f"  服务器管理员「{name}」的登录密钥已重置", "ok", "bold"))
    print(f"  新密钥（{'随机生成' if generated else '指定'}）：{paint(key, 'key', 'bold')}")
    print(paint("  存储方式：加盐哈希——数据库里不再保留明文", "note"))
    print(line)
    if _port_in_use(int(os.getenv("NTE_PORT", "8000"))):
        print(paint("  ⚠ 检测到服务仍在运行：请先停止它再重新启动，", "warn"))
        print(paint("    否则内存里的旧成员数据可能在下一次改动时覆盖本次重置。", "warn"))
    print("  下一步：重启服务 → 用上面的密钥登录（/admin 或 /user）")
    print(paint("  登录后可在「服务器 → 成员管理」里轮换密钥 / 令牌。", "note"))
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
    """编号 + 名字 + 权限 + QQ + uid（编号可直接用于下一步选择）。

    名字用亮色、身份信息压暗、标记用彩色：一屏名单里一眼就能挑出「当前管理员」。
    """
    for index, member in enumerate(members, 1):
        label = _PERMISSION_LABEL.get(str(member.get("permission")), str(member.get("permission")))
        marks = []
        if member.get("permission") == "server_admin":
            marks.append("当前管理员")
        if not member.get("active", True):
            marks.append("已停用")
        tail = paint(f"  ← {' · '.join(marks)}", "warn") if marks else ""
        print(
            f"  {index:>2}. {paint(member.get('name') or '(未命名)', 'bold')}  "
            f"[{paint(label, 'accent')}]  "
            + paint(f"QQ={member.get('qq') or '—'}  uid={member['uid']}", "note")
            + tail
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
        print(paint(f"未知的处置方式：{mode}（只能是 demote / delete）", "err"))
        return 2
    if not DB_PATH.exists():
        print(paint(f"未找到数据库：{DB_PATH}", "err"))
        print("请先执行 " + paint("uv run python -m app", "cmd") + " 启动一次服务，让它初始化数据。")
        return 1

    interactive = not (target and mode)
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        members = db.list_members(conn)
    if not members:
        print(paint("数据库里还没有成员。请先启动一次服务（会自动创建服务器管理员）。", "err"))
        return 1

    old = next((m for m in members if m.get("permission") == "server_admin"), None)

    if not target:
        print()
        print(paint("现有成员：", "head", "bold"))
        _print_member_list(members)
        print()
        target = _clean(ask("把哪一位设为新的服务器管理员？（填序号 / uid / QQ / 名字）："))

    hits = _match_members(members, target)
    if not hits:
        print(paint(f"没找到成员「{target}」：可以填序号、uid、QQ 或名字的一部分。", "err"))
        return 1
    if len(hits) > 1:
        print(paint(f"「{target}」匹配到 {len(hits)} 位，请写得更具体：", "warn"))
        _print_member_list(hits)
        return 1
    new_admin = hits[0]
    old_name = (old or {}).get("name") or "（当前没有管理员）"
    if old is not None and new_admin["uid"] == old["uid"]:
        print(paint(f"「{new_admin.get('name') or new_admin['uid']}」已经是服务器管理员了，无需交接。", "warn"))
        return 1

    if not mode:
        print()
        print(paint(f"当前服务器管理员：{old_name}", "head", "bold"))
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
            print(paint("已取消，什么都没改。", "warn"))
            return 1

    key = _clean(new_key)
    generated = not key
    if generated:
        key = generate_key()
    if len(key) < MIN_KEY_LEN:
        print(paint(f"密钥至少 {MIN_KEY_LEN} 位，请重新执行。", "err"))
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

    line = paint("─" * 62, "rule")
    print()
    print(line)
    print(
        paint(
            f"  服务器管理员已交给「{new_admin.get('name') or new_admin['uid']}」",
            "ok",
            "bold",
        )
    )
    print(f"  新登录密钥（{'随机生成' if generated else '指定'}）：{paint(key, 'key', 'bold')}")
    print(paint("  存储方式：加盐哈希——数据库里不再保留明文", "note"))
    if old is not None:
        if mode == "delete":
            print(f"  旧管理员「{old_name}」的账号已删除")
            print(paint("  （他创办的届会变成「无主」，由服务器管理员接管；比赛内容不受影响）", "note"))
        else:
            print(f"  旧管理员「{old_name}」已降级为赛事管理员（他创办的届仍归他管）")
    print(line)
    if _port_in_use(int(os.getenv("NTE_PORT", "8000"))):
        print(paint("  ⚠ 检测到服务仍在运行：请先停止它再重新启动，", "warn"))
        print(paint("    否则内存里的旧成员数据可能在下一次改动时覆盖本次交接。", "warn"))
    print("  下一步：重启服务 → 用上面的新密钥登录（/admin 或 /user）")
    print(paint("  重启后所有在线会话失效——旧管理员 / 被删成员的登录状态一并作废。", "note"))
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

    # 热更新守护要在**最前面**分流：它有自己的参数表（--port / --interval / --no-watch…），
    # 在这里先拆 --port 的话，`hotrun --port 8899` 里的端口会被吃掉、守护退到默认 8000。
    if args and args[0] == "hotrun":
        from .hotrun import main as hotrun_main

        return hotrun_main(args[1:])

    if args and args[0] == "--check":
        # 预检：给热更新守护用的（换代前确认新代码起得来），也可以手动跑一下放心
        return check_startup()

    port_raw, args = _take_option(args, "--port", "-p")
    host_raw, args = _take_option(args, "--host")
    port: int | None = None
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            print(paint(f"端口不合法：{port_raw}（应为一个 1~65535 的整数）", "err"))
            return 2
        if not 0 < port < 65536:
            print(paint(f"端口超出范围：{port}", "err"))
            return 2

    if args and args[0] in {"-h", "--help", "help"}:
        print(usage_text())
        return 0

    if args and args[0] == "--reset-key":
        rest = args[1:]
        if "-e" in rest:
            # 以前可以「重置某一届的管理 KEY」；主管理 KEY 退休后这个参数没有意义了，
            # 明确报错比默默忽略好——否则用户会以为重置没生效。
            print(paint("--reset-key 现在重置的是「服务器管理员」成员的登录密钥，与届次无关，", "err"))
            print(
                "不再接受 -e <届次> 参数。直接执行 "
                + paint("python -m app --reset-key", "cmd")
                + " 即可。"
            )
            return 2
        if rest and rest[0].startswith("-"):
            print(paint(f"无法识别的参数：{' '.join(rest)}", "err"))
            print(usage_text())
            return 2
        return reset_admin_key(rest[0] if rest else None)

    if args and args[0] == "--transfer-admin":
        rest = args[1:]
        key_raw, rest = _take_option(rest, "--key")
        delete_old, rest = _drop_flag(rest, "--delete-old")
        keep_old, rest = _drop_flag(rest, "--keep-old")
        if delete_old and keep_old:
            print(paint("--delete-old 与 --keep-old 只能给一个。", "err"))
            return 2
        target = ""
        if rest and not rest[0].startswith("-"):
            target = rest.pop(0)
        if rest:
            print(paint(f"无法识别的参数：{' '.join(rest)}", "err"))
            print(usage_text())
            return 2
        mode = "delete" if delete_old else ("demote" if keep_old else "")
        return transfer_admin(target, mode=mode, new_key=key_raw)

    # ``--no-hotrun`` 是「我要的就是原始那套单进程启动」：调试 / 交给外面管进程时用
    plain, args = _drop_flag(args, "--no-hotrun")
    if args:
        print(paint(f"无法识别的参数：{' '.join(args)}", "err"))
        print(usage_text())
        return 2

    host = host_raw or os.getenv("NTE_HOST", "0.0.0.0")
    resolved_port = int(port if port is not None else os.getenv("NTE_PORT", "8000"))

    # **默认就是热更新守护**：不中断服务地换代码是常态需求，不该让人每次多打一个词。
    # 三种情况退回「原始单进程启动」：① 我本身就是守护拉起来的子进程（否则无限递归）；
    # ② 显式 --no-hotrun / NTE_HOT=0；③ 开了 uvicorn 自己的 reloader（两套换代机制会抢端口）。
    supervised = hot.supervised()
    hot_off = plain or os.getenv("NTE_HOT", "").strip().lower() in ("0", "false", "no", "off")
    if supervised or hot_off or _reload_requested():
        if supervised:
            boot_log.info("由热更新守护启动：直接起服务（守护负责换代）")
        elif hot_off:
            boot_log.warning("按 --no-hotrun / NTE_HOT=0 直接起服务：没有热更新能力")
        else:
            boot_log.warning("检测到 NTE_RELOAD：用 uvicorn 自己的 reloader（不走热更新守护）")
        serve(host=host_raw, port=port)
        return 0

    from .hotrun import main as hotrun_main

    boot_log.info("默认启用热更新守护（要单进程直跑：加 --no-hotrun，或设 NTE_HOT=0）")
    return hotrun_main(["--host", host, "--port", str(resolved_port)])


def _reload_requested() -> bool:
    """uvicorn 自己的 ``--reload`` 是否开着（它与热更新守护是两套抢端口的机制）。"""
    return os.getenv("NTE_RELOAD", "0").lower() in {"1", "true", "yes", "on"}
