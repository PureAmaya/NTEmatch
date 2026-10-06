"""热更新：**不中断服务**地换上新的代码（后端 + 前端一起）。

比赛正在打的时候，任何一次重启都是事故——重启会踢掉所有在线会话、断开正在看比分的
WebSocket。所以这里把「更新」拆成两件互不干扰的事：

1. **会话落库**（:mod:`app.auth`）：重启不再等于「全部登出」。这是热更新的前提——
   不然换代码这件事本身就会把所有管理员踢下线，所谓「业务不中断」也就无从谈起；
2. **父子进程 + 套接字交接**（:mod:`app.hotrun`）：父进程只持监听套接字，业务跑在子进程里。
   换代时**先起新子进程**，等它真能服务了，再让旧子进程收尾退出——两个子进程短暂地
   同时 ``accept`` 同一个套接字，所以**连一秒的中断都没有**；新版本起不来
   （语法错 / 依赖没装）就直接丢掉它，旧版本继续跑，站点照常。
3. **前端**：静态资源地址本来就带内容版本号（``/static/v/<哈希>/``，见
   :func:`app.main._compute_asset_version`），所以 ``git pull`` 之后重新打开页面
   拿到的就是新代码；已经在页面里的人由前端自己探到版本变了并提示「点此刷新」
   （不强制刷新——他可能正在录入比分）。

这个模块只放**父子共用、且必须轻到不引入任何站点依赖**的东西：路径、套接字、状态文件。
父进程会长期持有它，所以这里除标准库什么都不导入——**改了业务代码不该影响
正在跑的那个父进程**，这是「换代时家长还在」的前提。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

# 刻意用标准库的 logging 而不是站点的 logging_conf：**父进程要长期持有这个模块**，
# 它不该被业务侧的导入链拖进来（子进程那边照旧由站点的日志配置统一格式化）。
log = logging.getLogger("nte.hot")

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
#: 数据根目录的判据与 :mod:`app.store` 完全一致（那边也读这个环境变量）。
#: 这里**刻意重复**这几行而不是 ``from .store import DATA_ROOT``：父进程要能长期
#: 活着，不能被业务模块的导入链拖进来（也免得换代时父进程跟着换了代码）。
#: 两条规则是否真的一致由 ``tests/test_hot.py`` 里的一条断言盯着。
_DATA_ENV = os.getenv("NTE_DATA_DIR", "").strip()


def data_root() -> Path:
    """数据根目录（与 :data:`app.store.DATA_ROOT` 同一规则，见上面的说明）。"""
    if _DATA_ENV:
        return Path(_DATA_ENV).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """代码仓库根目录（``git pull`` 在这里执行）。"""
    return Path(__file__).resolve().parent.parent


#: 控制文件目录：状态、更新请求、就绪标记、Windows 下的套接字移交件都放这儿。
#: 放在**数据目录**里（而不是仓库里）：容器 / 只读代码目录下也能写。
HOT_DIR = data_root() / "hot"
STATUS_FILE = "status.json"
TRIGGER_FILE = "reload.request"
SHARE_FILE = "socket.share"
#: 应用侧体检报告（由**业务进程**写，见 :func:`write_app_report`）。
#: 单独一个文件而不是塞进状态文件：状态文件是父进程的，两个进程读-改-写同一个 JSON
#: 迟早会互相覆盖。
APP_REPORT_FILE = "app.json"

#: 就绪文件的**环境变量名**：父进程给子进程设它，子进程启动完就写这个文件。
READY_ENV = "NTE_HOT_READY"
#: 套接字移交：POSIX 传 fd 号；Windows 传移交文件路径（见 :func:`adopt_socket`）。
FD_ENV = "NTE_HOT_FD"
SHARE_ENV = "NTE_HOT_SHARE"
#: 每个子进程一枚随机口令：父进程用它确认「这个端口上答话的确实是那一代」（见 :func:`ping`）。
#: 口令只在本机父子进程之间走环境变量，不进任何日志 / 响应。
TOKEN_ENV = "NTE_HOT_TOKEN"
#: 自查请求的请求头名（与 :func:`app.hot_api.api_hot_ping` 对齐）
PING_HEADER = "X-NTE-Hot"

#: 新子进程最多等多久算「起不来」（改依赖后首次启动会慢一些，给足时间）
READY_TIMEOUT = 90.0
#: 换代时等旧子进程收尾的上限：超过就强杀（正常情况下它的 in-flight 请求几秒内就完了）
DRAIN_TIMEOUT = 25.0

#: 被监视的目录：这些地方变了才值得换代（``data/`` 是运行期产物，不算）
WATCH_DIRS = ("app", "static", "integrations", "tools")
#: 被监视的根文件（依赖变了要顺带同步虚拟环境）
WATCH_FILES = ("pyproject.toml", "uv.lock", "Dockerfile", "docker-compose.yml")
#: 运行期会自己写的、**不能**算作「代码变了」的文件（否则一起步就自己触发换代，死循环）
WATCH_IGNORE = {
    "static/help.jpg",
    "static/help.jpg.src.sha256",
}
_IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def hot_dir() -> Path:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    return HOT_DIR


def status_path() -> Path:
    return hot_dir() / STATUS_FILE


def trigger_path() -> Path:
    return hot_dir() / TRIGGER_FILE


def ready_path(token: str) -> Path:
    """子进程的就绪标记（每次换代用一个新名字：**不要**复用旧文件）。"""
    return hot_dir() / f"ready-{token}"


def read_ready(path: Path) -> tuple[int, float]:
    """读就绪标记 ``(真实 pid, 就绪时刻)``；读不出来回 ``(0, 0.0)``。"""
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return 0, 0.0
    pid = int(lines[0]) if lines and lines[0].strip().isdigit() else 0
    stamp = float(lines[1]) if len(lines) > 1 else 0.0
    return pid, stamp


def share_path() -> Path:
    return hot_dir() / SHARE_FILE


def handshake_path() -> Path:
    """（Windows）子进程在这里报上自己的**真实 pid**，父进程据此移交套接字。"""
    return hot_dir() / "socket.pid"


def write_handshake() -> bool:
    """子进程：先自报真实 pid，再等套接字（Windows 的启动器会让 Popen 的 pid 对不上）。"""
    try:
        handshake_path().write_text(str(os.getpid()), "utf-8")
    except OSError:
        return False
    return True


def read_handshake() -> int:
    """父进程：读子进程自报的真实 pid（没读到回 0）。"""
    try:
        raw = handshake_path().read_text("utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


# --------------------------------------------------------------------------- #
# 状态文件：父进程写，HTTP 进程读
# --------------------------------------------------------------------------- #
def write_status(**fields: Any) -> dict[str, Any]:
    """写状态（读-改-写：父进程按需补字段，每次不必给全）。"""
    current = read_status()
    current.update(fields)
    current["at"] = time.time()
    path = status_path()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)  # 原子替换：读的人不会看到半份 JSON
    except OSError:  # 磁盘满了 / 没权限：状态是锦上添花，不能让它把服务搞挂
        pass
    return current


def read_status() -> dict[str, Any]:
    try:
        raw = status_path().read_text("utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# 应用侧体检报告：业务进程写，管理端读
#
# 为什么分两个文件：状态文件是**父进程**的（它按需读-改-写），业务进程再插一脚
# 迟早会互相覆盖。而体检里的这些事（会话有没有落库、事件循环是哪种）只有业务进程
# 知道——让写的人各写各的，读的人合并。
# --------------------------------------------------------------------------- #
def app_report_path() -> Path:
    return hot_dir() / APP_REPORT_FILE


def write_app_report(data: dict[str, Any]) -> None:
    """业务进程启动时写一份「我这边的事实」（供管理端展示）。"""
    try:
        path = app_report_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    except OSError:  # 报告写不了不影响服务
        pass


def read_app_report() -> dict[str, Any]:
    try:
        data = json.loads(app_report_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_status() -> None:
    try:
        status_path().unlink()
    except OSError:
        pass


def request(mode: str, actor: str = "") -> dict[str, Any]:
    """**请求一次更新**（由 HTTP 进程调用，父进程看到文件就动手）。

    父进程是个纯文件驱动的循环：这样 HTTP 进程不需要知道父进程的 pid、不需要额外的
    内网端口、也不存在「谁能调这个端口」的鉴权问题——能写这个文件的只有跑本站代码的
    进程，而写它这件事本身已经在 HTTP 那一侧过了权限（服务器管理员）。
    """
    payload = {"mode": str(mode or "reload"), "actor": str(actor or ""), "at": time.time()}
    path = trigger_path()
    tmp = path.with_suffix(".request.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    tmp.replace(path)
    return payload


def pending_request() -> dict[str, Any] | None:
    """看一眼有没有人在请求更新（**不消费**）。"""
    try:
        data = json.loads(trigger_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def take_request() -> dict[str, Any] | None:
    """取出并消费更新请求（父进程用）。"""
    data = pending_request()
    if data is None:
        return None
    try:
        trigger_path().unlink()
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------- #
# 就绪标记：子进程写，父进程等
# --------------------------------------------------------------------------- #
def supervised() -> bool:
    """当前进程是不是被热更新守护管着（决定「能不能热更新」这类提示）。"""
    return bool(os.environ.get(READY_ENV, "").strip())


def notify_ready() -> bool:
    """告诉父进程「新代码已经能服务了」（由 lifespan 在**真正开始接受连接前**调用）。

    时机很关键：调它的那一刻，数据库已打开、后台任务已拉起、WebSocket 广播中心已就绪，
    所以父进程拿到这个文件就能安全地停掉旧进程——这正是「零中断」的全部秘密。
    """
    raw = os.environ.get(READY_ENV, "").strip()
    if not raw:
        return False
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 第一行是**真实 pid**：父进程用它核对「答话的确实是这一代」
        # （Windows 上 Popen 的 pid 可能只是个启动器，见 :func:`_adopt_shared`）
        path.write_text(f"{os.getpid()}\n{time.time()}\n", "utf-8")
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 监听套接字：父进程建，子进程接管
# --------------------------------------------------------------------------- #
def bind_socket(host: str, port: int, backlog: int = 512) -> socket.socket:
    """建监听套接字（父进程专用；子进程只继承，谁都别再 bind 一次）。

    ``SO_REUSEADDR`` 只为「上一任刚退出、端口还在 TIME_WAIT」这种情况；
    真正的和平交接靠的是**同一个套接字**被两个子进程同时 accept——
    内核会把新连接分给其中一个，所以中间没有空窗。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host or "0.0.0.0", int(port)))
    sock.listen(backlog)
    # 刻意**不**设 set_inheritable：POSIX 上 subprocess 的 pass_fds 会自己处理该标志，
    # Windows 上走 socket.share（按 pid 移交），都不需要它。而 Windows 上多标一个
    # 「可继承」会改变句柄语义，实测会让子进程拿到一个「看着在监听却收不到请求」的
    # 套接字——这种坑不值得为了省一行去踩。
    return sock


def adopt_socket() -> socket.socket | None:
    """子进程侧：接管父进程递过来的监听套接字；没有（普通启动）就回 ``None``。

    * **POSIX**：父进程 ``pass_fds`` 把 fd 递进来，这里 ``fromfd`` 复制一份；
    * **Windows**：fd 不能跨进程继承，走 ``socket.share``——父进程要按 pid 移交，
      所以只能**先 spawn、再 share**，于是这里得等那个移交文件出现（几毫秒）。
    """
    fd_raw = os.environ.get(FD_ENV, "").strip()
    share_raw = os.environ.get(SHARE_ENV, "").strip()
    if os.name == "nt":
        return _adopt_shared(Path(share_raw)) if share_raw else None
    if not fd_raw.isdigit():
        return None
    try:
        return socket.fromfd(int(fd_raw), socket.AF_INET, socket.SOCK_STREAM)
    except OSError:  # pragma: no cover - 环境异常时退回普通启动
        return None


def _adopt_shared(path: Path, timeout: float = 30.0) -> socket.socket | None:
    """Windows：**先报上自己的真实 pid**，再等父进程把套接字写进来。

    为什么不能由父进程直接按 ``Popen`` 的 pid 移交：Windows 上 venv 的 ``python.exe``
    往往只是一个**启动器**（实测 Popen 拿到 48184 而真实进程是 31532）。父进程按启动器
    的 pid 移交，接手的却是另一个进程——那样拿到的套接字「看着能用」（连接会进内核
    backlog，``getsockname`` 也对），但**一个连接都 accept 不到**，服务等于挂了。
    所以这里反过来：子进程先自报家门，父进程再移交。
    """
    if not write_handshake():
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            blob = path.read_bytes()
        except OSError:
            blob = b""
        if blob:
            try:
                return socket.fromshare(blob)
            except OSError:  # pragma: no cover - 移交件坏了：退回普通启动
                return None
        time.sleep(0.05)
    return None


def use_selector_loop() -> bool:
    """**Windows 专用**：把事件循环策略换成 selector 循环，成不成回一个布尔。

    Windows 的默认循环是 proactor：它靠 IOCP 收连接，而 ``CreateIoCompletionPort``
    **不认「从别的进程继承 / 移交过来的」套接字**（实测直接
    ``OSError: [WinError 87] 参数错误``）。selector 循环用 ``select()``，对套接字
    来源没有这个要求。

    只影响 Windows（本机开发）；Linux 生产上不存在这个问题，uvloop 照常用。
    """
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:  # pragma: no cover - 非 Windows
        return False
    asyncio.set_event_loop_policy(policy())
    return True


def probe_host(host: str) -> str:
    """把监听地址换成能连的地址：``0.0.0.0`` / ``::`` 这类「所有网卡」连自己是回环。"""
    clean = str(host or "").strip()
    if not clean or clean in ("0.0.0.0", "::", "[::]", "*"):
        return "127.0.0.1"
    return clean


def ping(host: str, port: int, token: str, timeout: float = 1.5) -> int:
    """问一句「这个端口上答话的是哪一代？」——回它的 pid，问不到 / 对不上回 ``0``。

    这是**「就绪」与「真能收连接」之间的那道关**：就绪文件只说明应用起来了
    （数据库开好、后台任务拉起），万一它接不了这个套接字（平台差异，见
    :func:`selector_loop_factory`），父进程会在这一关发现并**放弃新进程**，
    让旧版本继续服务——而不是把服务交给一个收不到请求的进程。
    """
    if not token:
        return 0
    conn = http.client.HTTPConnection(probe_host(host), int(port), timeout=timeout)
    try:
        conn.request("GET", "/api/hot/ping", headers={PING_HEADER: token})
        resp = conn.getresponse()
        if resp.status != 200:
            return 0
        data = json.loads(resp.read().decode("utf-8") or "{}")
        return int(data.get("pid") or 0)
    except (OSError, ValueError, http.client.HTTPException):
        return 0
    finally:
        try:
            conn.close()
        except OSError:  # pragma: no cover
            pass


def hot_token() -> str:
    """本进程被移交的那枚口令（空串 = 不是被守护启动的）。"""
    return os.environ.get(TOKEN_ENV, "").strip()


def share_socket(sock: socket.socket, pid: int) -> bool:
    """父进程侧（Windows）把监听套接字移交给某个子进程。"""
    if os.name != "nt" or not hasattr(sock, "share"):
        return False
    try:
        blob = sock.share(pid)
    except OSError:
        return False
    try:
        path = share_path()
        tmp = path.with_suffix(".share.tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
    except OSError:
        return False
    return True


def socket_env(sock: socket.socket) -> dict[str, str]:
    """给子进程的套接字移交环境变量（两套平台各一条路）。"""
    if os.name == "nt":
        return {SHARE_ENV: str(share_path())}
    return {FD_ENV: str(sock.fileno())}


# --------------------------------------------------------------------------- #
# 变化检测
# --------------------------------------------------------------------------- #
def tree_stamp(root: Path | None = None) -> str:
    """把「代码现在长什么样」压成一个字符串（文件数 / 最新 mtime / 总大小）。

    不逐文件算哈希：这里只需要「变没变」，2 秒一次地扫目录也算哈希会把磁盘读醒。
    **不算**运行期产物（见 :data:`WATCH_IGNORE`），否则帮助图一生成就自己触发换代。
    """
    base = root or repo_root()
    count = 0
    newest = 0
    total = 0
    paths: list[Path] = []
    for name in WATCH_DIRS:
        paths.append(base / name)
    for name in WATCH_FILES:
        paths.append(base / name)
    for path in paths:
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            newest = max(newest, stat.st_mtime_ns)
            total += stat.st_size
            continue
        for item in _walk(path):
            rel = item.relative_to(base).as_posix()
            if rel in WATCH_IGNORE:
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            count += 1
            newest = max(newest, stat.st_mtime_ns)
            total += stat.st_size
    return f"{count}:{newest}:{total}"


def _walk(root: Path):
    """遍历目录，跳过缓存目录（手写递归是为了跳过整棵子树）。"""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name in _IGNORE_DIRS:
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file():
                    yield Path(entry.path)
            except OSError:  # 遍历途中文件被删了：跳过
                continue


def head_commit(repo: Path | None = None) -> str:
    """当前 HEAD 的提交号（**读文件**，不 fork git 进程）。

    只读 ``.git/HEAD``：它要么是 ``ref: refs/heads/x``（再去读那个 ref 文件），
    要么是游离头指针里的 sha 本身。``.git`` 也可能是 worktree 里那种
    「指向真正的 git 目录」的一行文本文件，一并处理。
    读不出来就回空串（调用方不显示提交号即可，不该因此报错）。
    """
    base = repo or repo_root()
    git_dir = base / ".git"
    try:
        if git_dir.is_file():
            line = git_dir.read_text("utf-8").strip()
            if not line.startswith("gitdir:"):
                return ""
            git_dir = (base / line.split(":", 1)[1].strip()).resolve()
        head = (git_dir / "HEAD").read_text("utf-8").strip()
    except (OSError, ValueError):
        return ""
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        try:
            return (git_dir / ref).read_text("utf-8").strip()
        except OSError:
            # 打包过的仓库会把 ref 收进 packed-refs
            try:
                for line in (git_dir / "packed-refs").read_text("utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0].strip()
            except OSError:
                return ""
        return ""
    return head


def short(commit: str, length: int = 8) -> str:
    """提交号的短形态（界面 / 日志里显示用）。"""
    clean = str(commit or "").strip()
    return clean[:length] if clean else ""


def public_status() -> dict[str, Any]:
    """给管理端看的一份状态：守护进程写的 + 我这边能判断的。"""
    status = read_status()
    out: dict[str, Any] = {
        "supervised": supervised(),
        "child": int(os.getpid()) if supervised() else 0,
        "attached": bool(status),
    }
    for key in (
        "phase",
        "message",
        "commit",
        "reloads",
        "since",
        "startedAt",
        "actor",
        "mode",
        # 换代语义与诊断：管理端要据此显示「零中断交接」还是「顺序换代」，
        # 出问题时那段日志尾巴（tail）也得能看到
        "holdSocket",
        "port",
        "watch",
        "tail",
        # 启动自检清单与「上次换代」的摘要：管理端要能一眼看到「能不能热更新、上次换得怎么样」
        "check",
        "lastReload",
    ):
        if key in status:
            out[key] = status[key]
    # 业务进程那边的事实（会话落库、事件循环、卡片渲染…）：谁写的谁负责，这里只合并
    report = read_app_report()
    if report:
        out["app"] = report
    if status.get("commit"):
        out["short"] = short(str(status["commit"]))
    out["head"] = short(head_commit())
    out["pending"] = bool(pending_request())
    if status.get("at"):
        out["at"] = status["at"]
    return out


def started() -> bool:
    """有没有守护进程在跑（状态文件够新 = 它还活着）。"""
    status = read_status()
    at = float(status.get("at") or 0)
    return bool(at) and (time.time() - at) < 60.0


def python() -> str:
    """当前解释器路径（写进日志，排查「换了个 python」这类问题用）。"""
    return sys.executable or "python"


#: 父进程的心跳「过期」判据（秒）：超过这么久没有任何心跳，就认为守护已经没了。
#: 取得比较宽（默认检测间隔 2 秒 → 20 次落空）是故意的：宁可多等一会儿，
#: 也别因为一次磁盘卡顿把正在服务的进程误判成孤儿。
PARENT_GONE_AFTER = 45.0


def _parent_gone_after() -> float:
    """心跳过期判据（默认 :data:`PARENT_GONE_AFTER`，可用环境变量调小——测试要用）。"""
    raw = os.environ.get("NTE_HOT_PARENT_GONE", "").strip()
    if not raw:
        return PARENT_GONE_AFTER
    try:
        value = float(raw)
    except ValueError:
        return PARENT_GONE_AFTER
    return value if value > 0 else PARENT_GONE_AFTER


def parent_alive() -> bool:
    """守护（父进程）还在吗？—— 看它在状态文件里的**心跳**。

    为什么需要它：父进程被 ``kill -9`` / Windows 上 ``terminate()`` 硬杀时，
    它的清理代码根本没机会跑，业务子进程就会变成**孤儿**——还占着端口，
    于是下一次启动绑不上，看起来像「服务莫名起不来」。子进程自己盯着心跳，
    父进程一没就跟它一起收摊。

    用状态文件的 ``alive`` 字段而不是「给 pid 发信号」：跨平台一致（Windows 上
    ``os.kill(pid, 0)`` 并不通用），也顺带覆盖「父进程还在但已经卡死」这种情况。
    父进程从没写过心跳（例如刚启动那两秒）时不判定为死——见函数里的说明。
    """
    status = read_status()
    alive = float(status.get("alive") or 0)
    if not alive:
        # 还没看到过心跳：要么守护刚起来（正常），要么状态文件是别的进程留的
        # （那时也没有 alive 字段）。**不判定为死**——误杀正在服务的进程代价更大。
        return True
    return (time.time() - alive) < _parent_gone_after()


async def watch_parent(interval: float = 5.0) -> None:
    """业务进程侧：守护没了就自己收摊（优雅退出，别赖着占端口）。

    由 :func:`app.main.lifespan` 起成后台任务；只在「被守护启动」时有意义。
    """
    if not supervised():
        return
    while True:
        await asyncio.sleep(interval)
        if not parent_alive():
            log.warning("热更新守护已退出（心跳停了），子进程跟着收摊，把端口让出来")
            import os as _os
            import signal as _signal

            try:
                _os.kill(_os.getpid(), _signal.SIGTERM)  # 走 uvicorn 的优雅退出
            except (OSError, AttributeError):  # pragma: no cover - Windows 上偶发
                pass
            await asyncio.sleep(5)
            return
