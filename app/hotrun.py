"""热更新守护进程：``git pull`` 之后**不中断服务**地换代。

用法
----

```bash
uv run python -m app hotrun            # 守护模式：自己盯着代码变化，变了就换代
uv run python -m app hotrun --port 8000 --interval 2
uv run python -m app hotrun --no-watch # 只留「管理端点一下才更新」这条路
```

它做什么
--------

父进程（这个文件）**只做一件事**：持有监听套接字，并在需要时换掉业务子进程。
业务永远跑在子进程里，于是「换代码」= 起一个新的、停掉旧的：

1. 新子进程**继承同一个监听套接字**（POSIX 传 fd，Windows 走 ``socket.share``），
   所以它能立刻开始接受连接——**两个子进程会短暂地同时 accept**，中间没有空窗；
2. 新子进程启动完（数据库已打开、后台任务已拉起）会写一个**就绪文件**，
   父进程等到它才动手停旧的。于是「新版本起不来」这种情况**绝不会**变成事故：
   站起来失败就把新进程丢掉，旧进程继续服务，状态里留一句为什么；
3. 旧子进程收到 ``SIGTERM`` 后是 uvicorn 的**优雅退出**：把手上正在处理的请求跑完、
   关掉 WebSocket 再退——客户端那边只是重连一下（前端有自动重连 + 指数退避）。

谁来触发换代
------------

* **文件触发**（HTTP 侧的服务端管理员点「立即更新」时写它，见 :func:`app.hot.request`）：
  这样父进程不需要开内网端口、也不需要额外的鉴权面——能写这个文件的只有跑本站代码的进程；
* **自动检测**：每 ``--interval`` 秒比一次代码指纹（文件数 / 最新 mtime / 总大小）与
  ``HEAD``，变了就换代。**开发机上改一行保存即生效**，不用手动触发；
* 两者都关（``--no-watch`` 且没人写触发文件）时它就是个「端口持有者 + 进程守护」。

``git pull`` 的两条安全线
------------------------

* 只跑 ``--ff-only``：分叉了宁可停下来报错，也不在服务器上留下一个没人看过的合并提交；
* **工作区有未提交改动就拒绝**：自动 pull 覆盖掉别人正在改的文件比「不更新」糟得多。
* ``uv.lock`` / ``pyproject.toml`` 变了会顺带 ``uv sync``（依赖不跟着换，新代码大概率起不来）。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hot
from .logging_conf import get_logger

log = get_logger("hotrun")

#: 子进程日志在内存里留多少行（换代失败时把尾巴写进状态，供管理端看）
TAIL_LINES = 120
#: 换代失败时状态里带多少行日志（太长没人看）
TAIL_REPORT = 25


@dataclass
class Child:
    """一个业务子进程（一代代码）。"""

    proc: subprocess.Popen
    ready: Path
    token: str
    #: 这一代自己的口令：父进程用它确认「答话的确实是这一代」（见 hot.ping）
    secret: str = ""
    #: 子进程**自报的真实 pid**（Windows 上 Popen 的 pid 有可能只是个启动器，见 hot.py）
    real_pid: int = 0
    started_at: float = field(default_factory=time.time)
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    exit_code: int | None = None

    @property
    def pid(self) -> int:
        """``Popen`` 拿到的 pid（**可能只是启动器**，判断「谁在服务」要用 :attr:`real_pid`）。"""
        return int(self.proc.pid or 0)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def tail_text(self, lines: int = TAIL_REPORT) -> str:
        return "\n".join(list(self.tail)[-lines:])


class Supervisor:
    """父进程：持有套接字 + 换代。**不导入任何业务模块**（见 :mod:`app.hot`）。"""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8000,
        interval: float = 2.0,
        watch: bool = True,
        ready_timeout: float = hot.READY_TIMEOUT,
        drain_timeout: float = hot.DRAIN_TIMEOUT,
        sync_deps: bool = True,
        hold_socket: bool | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.interval = max(0.5, float(interval))
        self.watch = watch
        self.ready_timeout = float(ready_timeout)
        self.drain_timeout = float(drain_timeout)
        self.sync_deps = sync_deps
        # 父进程要不要自己持有监听套接字（= 「零中断」的那条路）：
        #   * POSIX：持有。新老两个子进程同时 accept 同一个套接字，换代中间没有空窗；
        #   * Windows：不持有。跨进程移交的套接字在这里会「看着在监听却收不到请求」
        #     （详见 README 的说明），所以退一步做顺序换代——先停旧的、等端口放开再起
        #     新的，中间约 1 秒。这是本机开发环境的取舍，生产（Linux）不受影响。
        self.hold_socket = (os.name != "nt") if hold_socket is None else bool(hold_socket)
        self.sock: Any = None
        self.child: Child | None = None
        self.reloads = 0
        self.stamp = ""
        self.phase = "starting"
        self.started_at = time.time()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        hot.hot_dir()
        hot.clear_status()
        self._install_signals()
        hot.write_status(
            phase="starting",
            pid=os.getpid(),
            host=self.host,
            port=self.port,
            python=hot.python(),
            watch=self.watch,
            holdSocket=self.hold_socket,
            startedAt=self.started_at,
            commit=hot.head_commit(),
            reloads=0,
            since=self.started_at,
        )
        log.warning(
            "热更新守护启动 | %s:%d | 监视=%s | 间隔=%.1fs | 零中断=%s | 数据目录=%s",
            self.host,
            self.port,
            "开" if self.watch else "关",
            self.interval,
            "开" if self.hold_socket else "关（顺序换代）",
            hot.hot_dir(),
        )
        if self.hold_socket:
            try:
                self.sock = hot.bind_socket(self.host, self.port)
            except OSError as exc:
                log.error("监听 %s:%d 失败：%s", self.host, self.port, exc)
                hot.write_status(phase="failed", message=f"监听失败：{exc}")
                return 1
        else:
            log.warning(
                "顺序换代模式（不持有监听套接字）：更新时先停旧进程、再起新进程，"
                "中间约 1 秒空窗。Linux 上会自己持有套接字、做到零中断"
            )

        # 起服务之前先体检一遍，把结论印在控制台、也写进状态文件（管理端看得到）
        checks = self._selfcheck()
        self._print_block(
            "热更新自检",
            [
                f"[{'OK ' if item['ok'] else '注意'}] {item['name']}：{item['detail']}"
                for item in checks
            ]
            + ["（同样的清单在「服务器 → 热更新」里也能看到）"],
        )
        hot.write_status(check=checks, checkAt=time.time())

        child = self._spawn()
        if not self._wait_ready(child):
            self._fail(child, "服务启动失败（检查上面的日志）")
            return 1
        self.child = child
        self.stamp = hot.tree_stamp()
        self.phase = "running"
        log.warning("服务已就绪 | pid=%d | 端口=%d", child.real_pid or child.pid, self.port)
        hot.write_status(
            phase="running",
            commit=hot.head_commit(),
            short=hot.short(hot.head_commit()),
            pid=os.getpid(),
            child=child.real_pid or child.pid,
            since=time.time(),
            watched=self.stamp,
            message="",
        )
        try:
            self._loop()
        finally:
            self._shutdown()
        return 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            request = hot.take_request()
            if request:
                self._update(
                    mode=str(request.get("mode") or "reload"),
                    actor=str(request.get("actor") or ""),
                    reason="管理端请求",
                )
            elif self.watch:
                stamp = hot.tree_stamp()
                if stamp != self.stamp:
                    log.info("检测到代码变化，准备换代")
                    self._update(mode="reload", actor="watcher", reason="文件变化")
            # 心跳：状态文件够新就说明守护还活着（管理端据此判断）
            hot.write_status(
                phase=self.phase,
                pid=os.getpid(),
                child=(self.child.real_pid or self.child.pid) if self.child else 0,
                alive=time.time(),
                watched=self.stamp,
                port=self.port,
                watch=self.watch,
                reloads=self.reloads,
                pending=bool(hot.pending_request()),
            )
            self._stop.wait(self.interval)

    def _shutdown(self) -> None:
        """守护自己收到退出信号：把子进程**优雅**收掉再走（docker stop / systemd）。"""
        log.warning("守护收到退出信号，正在收尾")
        hot.write_status(phase="stopping", message="守护退出中")
        if self.child is not None:
            self._retire(self.child, why="守护退出")
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:  # pragma: no cover - 关闭失败无所谓
            pass
        hot.write_status(phase="stopped", child=0, message="守护已退出")

    def _install_signals(self) -> None:
        def handler(signum: int, _frame: Any) -> None:  # pragma: no cover - 信号路径
            log.warning("收到信号 %s，准备退出", signum)
            self._stop.set()

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):  # 不在主线程 / 平台不支持
                continue

    # ------------------------------------------------------------------ #
    # 换代
    # ------------------------------------------------------------------ #
    def _update(self, *, mode: str, actor: str, reason: str) -> None:
        """一次换代：``mode=pull`` 先拉代码，然后起新的、停旧的。"""
        started = time.time()
        commit_from = hot.head_commit()
        log.warning("开始换代 | 触发=%s | 方式=%s | 发起人=%s", reason, mode, actor or "-")
        self.phase = "updating"
        hot.write_status(phase="updating", mode=mode, actor=actor, message=reason)
        pulled = ""
        if mode == "pull":
            ok, pulled = self._git_pull()
            if not ok:
                self.phase = "running"
                log.error("git pull 未完成，保持现状 | %s", pulled)
                hot.write_status(phase="running", mode="", actor="", message=f"更新中止：{pulled}")
                self._report_reload(
                    ok=False,
                    mode=mode,
                    actor=actor,
                    reason=reason,
                    commit_from=commit_from,
                    commit_to=hot.head_commit(),
                    seconds=time.time() - started,
                    detail=f"git pull 没做完：{pulled}（没有换代，服务一秒钟都没停）",
                )
                self.stamp = hot.tree_stamp()  # 免得同一份改动又被检测一次
                return

        # 先给新代码做一次「能不能起来」的体检：**在停掉旧进程之前**把
        # 「语法错 / 依赖没装 / 装配失败」这类坏版本挡下来（顺序换代时尤其重要，
        # 那时候旧进程一旦停了就没有退路了）。
        if not self._preflight():
            self.phase = "running"
            self._report_reload(
                ok=False,
                mode=mode,
                actor=actor,
                reason=reason,
                commit_from=commit_from,
                commit_to=hot.head_commit(),
                seconds=time.time() - started,
                detail="预检没过（语法错 / 依赖没装之类），已中止换代；旧版本继续服务",
            )
            self.stamp = hot.tree_stamp()
            return

        old = self.child
        if self.sock is None:
            # 顺序换代（不持有套接字）：旧进程必须先让出端口，否则新进程绑不上
            if old is not None:
                self.child = None
                self._retire(old, why="顺序换代：先让出端口")
                old = None
            self._wait_port_free()
        new = self._spawn()
        if not self._wait_ready(new) and self.child is None:
            # 顺序换代（Windows）：「新的起不来」意味着**服务此刻是空的**，
            # 所以再给一次机会（端口有时要多等一会儿才真的放开），还不行就如实报出来。
            log.warning("新进程首次启动失败，2 秒后重试一次 | pid=%d", new.pid)
            self._kill(new)
            time.sleep(2.0)
            self._wait_port_free()
            new = self._spawn()
        if not self._wait_ready(new):
            self._fail(new, pulled or "新版本启动未成功")
            return
        self.child = new
        self.reloads += 1
        self.stamp = hot.tree_stamp()
        self.phase = "running"
        log.warning("换代完成 | 新 pid=%d | 累计=%d | %s", new.pid, self.reloads, pulled or reason)
        hot.write_status(
            phase="running",
            child=new.pid,
            commit=hot.head_commit(),
            short=hot.short(hot.head_commit()),
            reloads=self.reloads,
            since=time.time(),
            message=pulled or f"已重新加载（{reason}）",
            mode="",
            actor="",
            watched=self.stamp,
        )
        commit_to = hot.head_commit()
        self._report_reload(
            ok=True,
            mode=mode,
            actor=actor,
            reason=reason,
            commit_from=commit_from,
            commit_to=commit_to,
            seconds=time.time() - started,
            detail=(
                (pulled + "；" if pulled else "")
                + (
                    "新进程已接管并确认能收请求，旧进程优雅退出（服务未中断）"
                    if self.hold_socket and old is not None
                    else "新进程已接管并确认能收请求"
                    if old is None
                    else "新进程已接管；旧进程已让出端口（顺序换代，中间约 1 秒空窗）"
                )
            ),
        )
        if old is not None:
            self._retire(old, why="被新版本替换")

    def _preflight(self) -> bool:
        """跑一次 ``python -m app --check``：**只导入、不起服务**，看新代码能不能活。

        换代之前先体检，好处是「坏版本」被挡在**停掉旧进程之前**（顺序换代时尤其关键：
        那时候旧进程一旦停了就没有退路）。体检本身失败（比如超时）不拦更新——
        它只是个额外的保险，不是新的失败点。
        """
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "app", "--check"],
                cwd=str(hot.repo_root()),
                env=self._child_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("预检没能跑起来（跳过，直接换代）| %s", exc)
            return True
        if proc.returncode == 0:
            log.info("新代码预检通过")
            return True
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        reason = tail[-1].strip()[:160] if tail else "预检未通过"
        log.error("新代码预检未通过，保持现状 | %s", reason)
        hot.write_status(
            phase="running",
            mode="",
            actor="",
            message=f"更新中止：新代码起不来（{reason}）",
            tail="\n".join(tail[-25:]),
        )
        return False

    def _wait_port_free(self, timeout: float = 25.0) -> bool:
        """等端口真的放开（顺序换代时要等旧进程把端口让出来）。

        故意**不带** ``SO_REUSEADDR`` 去试绑：带上它，Windows 会因为「地址可重用」
        直接绑成功，等于什么都没测到。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((self.host, self.port))
                return True
            except OSError:
                time.sleep(0.2)
            finally:
                probe.close()
        log.warning("等端口 %d 放开超时，仍然尝试启动新进程", self.port)
        return False

    # ------------------------------------------------------------------ #
    # 自检与回显（控制台一份、状态文件一份 → 管理端「服务器 → 热更新」再显示一份）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _print_block(title: str, lines: list[str]) -> None:
        """印一段有边框的结论块：人盯控制台时一眼能找到。"""
        print(flush=True)
        print("=" * 68, flush=True)
        print(title, flush=True)
        for line in lines:
            print(f"  {line}", flush=True)
        print("=" * 68, flush=True)
        print(flush=True)

    def _port_available(self) -> tuple[bool, str]:
        """端口现在能不能绑（**不带** SO_REUSEADDR 试，带了就测不出被占用）。"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((self.host, self.port))
            return True, f"{self.host}:{self.port} 可以绑定"
        except OSError as exc:
            return False, f"{self.host}:{self.port} 绑不上（{exc}）"
        finally:
            probe.close()

    def _selfcheck(self) -> list[dict[str, Any]]:
        """启动自检：逐项查「热更新能不能用、会不会中断」，并给你「结论 + 依据」。

        结果同时进**控制台**和**状态文件**（管理端「服务器 → 热更新」看的那份）：
        热更新最怕的就是「以为它能用」，所以每一项都要说清判据，而不只给一个勾。
        """
        items: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            items.append({"name": name, "ok": bool(ok), "detail": detail})

        add(
            "换代方式",
            True,
            "零中断交接：父进程持有监听套接字，新老进程同时 accept，服务不空窗"
            if self.hold_socket
            else "顺序换代：先停旧进程、等端口放开再起新的，中间约 1 秒连不上（Windows 的取舍）",
        )
        if self.sock is not None:
            add("监听端口", True, f"{self.host}:{self.port} 已由守护持有（子进程继承）")
        else:
            ok, detail = self._port_available()
            add("监听端口", ok, detail)
        in_repo = (hot.repo_root() / ".git").exists()
        head = hot.head_commit()
        add(
            "代码仓库",
            in_repo,
            f"当前提交 {hot.short(head) or '?'}，可以 git pull"
            if in_repo
            else "不是 git 仓库：只能「改文件即换代」，git pull 那条路不成立",
        )
        if in_repo:
            ok, out = self._git(["status", "--porcelain"])
            dirty = bool(out.strip()) if ok else False
            add(
                "工作区",
                not dirty,
                "干净，可以自动 git pull"
                if not dirty
                else "有未提交改动：git pull 会被拒绝（改文件的换代照常）",
            )
        add(
            "git 命令",
            shutil.which("git") is not None,
            "可用（拉代码用）" if shutil.which("git") else "找不到 git：只能靠「改文件即换代」",
        )
        add(
            "uv 命令",
            shutil.which("uv") is not None,
            "可用（依赖变化时 uv sync --frozen）"
            if shutil.which("uv")
            else "找不到 uv：依赖变了不会自动同步，可能要手动 uv sync",
        )
        add("数据目录", os.path.isdir(hot.hot_dir()), f"{hot.hot_dir()}（状态与触发文件在这里）")
        add(
            "监控范围",
            True,
            (
                f"自动检测改动（每 {self.interval:.0f} 秒一次）：{'、'.join(hot.WATCH_DIRS)}"
                if self.watch
                else "只接受管理端触发的更新（--no-watch）"
            ),
        )
        ok = self._preflight()
        add(
            "新代码预检",
            ok,
            "python -m app --check 通过（每次换代前也会再跑一次）"
            if ok
            else "当前代码就过不了预检，换代会被拦下（旧版本继续服务）",
        )
        return items

    def _report_reload(
        self,
        *,
        ok: bool,
        mode: str,
        actor: str,
        reason: str,
        commit_from: str,
        commit_to: str,
        seconds: float,
        detail: str,
    ) -> dict[str, Any]:
        """换代结果：**控制台印一份、状态里存一份**（管理端据此显示「上次换代」）。"""
        summary: dict[str, Any] = {
            "at": time.time(),
            "ok": bool(ok),
            "mode": mode or "reload",
            "actor": actor,
            "reason": reason,
            "from": hot.short(commit_from),
            "to": hot.short(commit_to),
            "seconds": round(float(seconds), 2),
            "message": detail,
            "pid": self.child.real_pid or self.child.pid if self.child else 0,
            "reloads": self.reloads,
        }
        lines = [
            (f"提交：{summary['from'] or '?'} → {summary['to'] or '?'}"),
            f"触发：{reason}"
            + (f"（{actor}）" if actor else "")
            + f" · 方式：{summary['mode']}",
            f"耗时：{summary['seconds']}s"
            + (f" · 已换代 {self.reloads} 次" if ok else ""),
            detail,
        ]
        if not ok:
            lines.append("旧版本仍在服务" if self.child is not None else "**服务当前处于停止状态**")
        self._print_block("热更新" + ("完成" if ok else "未生效"), lines)
        hot.write_status(lastReload=summary)
        return summary

    def _fail(self, child: Child, why: str) -> None:
        """新版本没起来：丢掉它，让**旧的继续服务**，并把原因记下来。"""
        tail = child.tail_text()
        code = child.exit_code if child.exit_code is not None else child.proc.poll()
        # 顺序换代时旧进程已经让出端口，新进程又没起来 = **服务此刻是停的**：
        # 这种情况必须说清楚，而不是含糊地来一句「更新未生效」。
        down = self.child is None
        log.error(
            "新版本启动失败（%s）| %s | 退出码=%s",
            "服务当前处于停止状态" if down else "旧版本继续服务",
            why,
            code,
        )
        self._kill(child)
        self.phase = "failed" if down else "running"
        self._report_reload(
            ok=False,
            mode="",
            actor="",
            reason="启动失败",
            commit_from=hot.head_commit(),
            commit_to=hot.head_commit(),
            seconds=0.0,
            detail=f"{why}（旧版本继续服务）" if not down else f"{why}（服务已停，需手动重启守护）",
        )
        hot.write_status(
            phase=self.phase,
            child=(self.child.real_pid or self.child.pid) if self.child else 0,
            failed_at=time.time(),
            message=(
                f"更新未生效：{why}"
                + (f"（退出码 {code}）" if code is not None else "")
                + ("；旧进程已让出端口，服务当前处于停止状态，请检查代码后手动重启守护" if down else "")
            ),
            tail=tail,
            mode="",
            actor="",
            watched=hot.tree_stamp(),
        )

    def _child_env(self, *, ready: Path | None = None, secret: str = "") -> dict[str, str]:
        """子进程的环境变量（预检与真启动共用一份，免得两处各说各话）。

        ``ready`` 为空表示「预检」：**不**给套接字、不给就绪标记——预检只导入代码，
        要是把 ``NTE_HOT_SHARE`` 也塞给它，它会去等一个永远不会来的套接字。
        """
        env = dict(os.environ)
        if ready is not None:
            if self.sock is not None:
                env.update(hot.socket_env(self.sock))
            env[hot.READY_ENV] = str(ready)
            env[hot.TOKEN_ENV] = secret
        else:
            for name in (hot.FD_ENV, hot.SHARE_ENV, hot.READY_ENV, hot.TOKEN_ENV):
                env.pop(name, None)
        env["NTE_HOST"] = self.host
        env["NTE_PORT"] = str(self.port)
        return env

    def _spawn(self) -> Child:
        """起一个业务子进程，并把监听套接字交给它。"""
        token = uuid.uuid4().hex[:12]
        secret = secrets.token_urlsafe(16)
        ready = hot.ready_path(token)
        try:
            ready.unlink()
        except OSError:
            pass
        env = self._child_env(ready=ready, secret=secret)
        # 换代由**这里**负责，别让 uvicorn 自己也起一个 reloader（会变成两套机制抢同一端口）
        env.pop("NTE_RELOAD", None)
        env["NTE_WORKERS"] = "1"
        # POSIX：把监听套接字的 fd 递给子进程（`pass_fds` 会替我们处理可继承标志）。
        # Windows 走 `socket.share`，没有这一步。
        pass_fds: tuple[int, ...] = ()
        if self.sock is not None and os.name != "nt":
            pass_fds = (self.sock.fileno(),)
        # 固定用**当前解释器**跑本站代码（同一个虚拟环境），不做任何 shell 展开。
        # POSIX：另起进程组，收尾时能给整组发信号（子进程自己再 fork 的也一起收掉）。
        proc = subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=str(hot.repo_root()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=(os.name != "nt"),
            pass_fds=pass_fds,
        )
        child = Child(proc=proc, ready=ready, token=token, secret=secret)
        threading.Thread(target=self._pump, args=(child,), name=f"log-{token}", daemon=True).start()
        log.info("已启动子进程 | pid=%d | token=%s", child.pid, token)
        return child

    def _pump(self, child: Child) -> None:
        """把子进程的日志转发到自己的输出（容器 / systemd 里能看到），并留一份尾巴。"""
        stream = child.proc.stdout
        if stream is None:  # pragma: no cover - 只在我们没要管道时发生
            return
        try:
            for line in stream:
                child.tail.append(line.rstrip("\n"))
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
        except (OSError, ValueError):  # pragma: no cover - 管道关闭
            return

    def _handover(self, child: Child) -> bool:
        """（Windows）等子进程报上**真实 pid**，再把监听套接字移交给它。

        只有「父进程持有套接字」（POSIX）时才需要它。Windows 是顺序换代：子进程自己
        绑端口，没有套接字可移交；POSIX 走文件描述符继承，fd 天生就是对的。
        """
        if self.sock is None or os.name != "nt":
            return True
        try:
            hot.handshake_path().unlink()
        except OSError:
            pass
        deadline = time.monotonic() + min(30.0, self.ready_timeout)
        while time.monotonic() < deadline:
            if child.proc.poll() is not None:
                child.exit_code = int(child.proc.returncode or 0)
                return False
            real = hot.read_handshake()
            if real:
                if not hot.share_socket(self.sock, real):
                    log.warning("套接字移交失败 | 真实 pid=%d", real)
                    return False
                log.info("套接字已移交 | 启动器 pid=%d | 真实 pid=%d", child.pid, real)
                return True
            time.sleep(0.05)
        log.error("等子进程报上 pid 超时 | pid=%d", child.pid)
        return False

    def _wait_ready(self, child: Child, timeout: float | None = None) -> bool:
        """三段式判定：① 移交套接字 ② 应用起来了（就绪文件）③ **真的能收连接**。

        第二、三段都不是多余：就绪文件只说明数据库开了、后台任务起了；万一这个进程
        接不了那个套接字（平台差异见 :func:`hot.selector_loop_factory`），它会
        「看起来就绪」却一个请求都收不到——**连接甚至会成功**（进内核 backlog），
        所以只能真的发一个请求、用这一代的口令问一句「你是谁」，答上来才算换代成功。
        """
        if not self._handover(child):
            return False
        limit = time.monotonic() + (timeout if timeout is not None else self.ready_timeout)
        while time.monotonic() < limit:
            if child.ready.exists():
                child.real_pid, _ = hot.read_ready(child.ready)
                log.info(
                    "子进程已就绪 | pid=%d | 真实 pid=%d | 用时 %.1fs",
                    child.pid,
                    child.real_pid,
                    time.time() - child.started_at,
                )
                break
            code = child.proc.poll()
            if code is not None:
                child.exit_code = int(code)
                return False
            time.sleep(0.15)
        else:
            log.error("等待子进程就绪超时（%.0fs）| pid=%d", self.ready_timeout, child.pid)
            return False

        wanted = child.real_pid or child.pid
        verify_until = min(limit, time.monotonic() + 10.0)
        while time.monotonic() < verify_until:
            if child.proc.poll() is not None:
                child.exit_code = int(child.proc.returncode or 0)
                return False
            if hot.ping(self.host, self.port, child.secret) == wanted:
                log.info("已确认新进程在服务 | 真实 pid=%d", wanted)
                return True
            time.sleep(0.1)
        log.error("新进程起来了但收不到请求（就绪却不应答）| 真实 pid=%d", wanted)
        return False

    def _signal_tree(self, child: Child, *, force: bool) -> None:
        """给**整棵进程树**发信号（不是只杀那一个 pid）。

        两个理由，都是踩过的：

        * Windows 上 venv 的 ``python.exe`` 只是个**启动器**，``terminate()`` 只会杀掉
          启动器，真正的服务进程变成孤儿、继续占着端口——下次换代就再也起不来了；
        * POSIX 上业务进程自己 fork 的辅助进程同理（我们给它开了独立进程组）。
        """
        if os.name == "nt":  # pragma: no cover - Windows 分支
            # **两个 pid 都要杀**：Popen 拿到的是启动器，而真正在服务的可能是另一个
            # 进程（见 hot.py）。只杀启动器的话，服务进程会活下来继续占着端口，
            # 下一次换代就再也起不来了——这是实测踩过的坑。
            for pid in {child.pid, child.real_pid}:
                if not pid:
                    continue
                args = ["taskkill", "/T", "/PID", str(pid)]
                if force:
                    args.insert(1, "/F")
                subprocess.run(args, capture_output=True, check=False, timeout=30.0)
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(child.pid), sig)
        except OSError:
            with contextlib.suppress(OSError):
                child.proc.send_signal(sig)

    def _retire(self, child: Child, *, why: str) -> None:
        """让旧子进程体面退场：``SIGTERM`` → 等它跑完手上的请求 → 超时才强杀。

        Linux 上这就是 uvicorn 的优雅退出：正在处理的请求跑完、WebSocket 关掉再退，
        所以观众那边只是重连一下（前端有自动重连），不会有「请求被掐断」。
        """
        if not child.alive:
            return
        if os.name == "nt":  # pragma: no cover - Windows 分支
            # Windows 上没有「优雅退出」这回事：`taskkill` 不加 ``/F`` 杀不掉控制台程序，
            # 只会白等一轮超时（实测每次换代要 25 秒）。直接结束，然后把端口等回来。
            # Linux 上则是真正的优雅退出（见下）。
            log.info("结束旧进程 | 真实 pid=%d | 原因=%s", child.real_pid or child.pid, why)
            self._kill(child)
            return
        log.info("请求旧进程退出 | 真实 pid=%d | 原因=%s", child.real_pid or child.pid, why)
        self._signal_tree(child, force=False)
        try:
            child.proc.wait(timeout=self.drain_timeout)
            log.info("旧进程已退出 | 退出码=%s", child.proc.returncode)
        except subprocess.TimeoutExpired:
            log.warning("旧进程 %.0fs 未退出，强制杀掉 | pid=%d", self.drain_timeout, child.pid)
            self._kill(child)

    def _kill(self, child: Child) -> None:
        if not child.alive:
            return
        try:
            self._signal_tree(child, force=True)
            child.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            log.warning("子进程强杀失败 | pid=%d", child.pid)

    # ------------------------------------------------------------------ #
    # git
    # ------------------------------------------------------------------ #
    def _git(self, args: list[str], timeout: float = 60.0) -> tuple[bool, str]:
        git = shutil.which("git")
        if not git:
            return False, "找不到 git 命令"
        try:
            proc = subprocess.run(
                [git, *args],
                cwd=str(hot.repo_root()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,  # 退出码要看内容（pull 的冲突信息在 stderr 里），不抛异常
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"git {' '.join(args)} 执行失败：{exc}"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()

    def _git_pull(self) -> tuple[bool, str]:
        """``git pull --ff-only``（两条安全线见模块说明）。"""
        repo = hot.repo_root()
        if not (repo / ".git").exists():
            return False, "这不是一个 git 仓库（热更新只适用于「从仓库里跑」的部署）"
        ok, out = self._git(["status", "--porcelain"])
        if not ok:
            return False, f"git status 失败：{out}"
        if out:
            return False, "工作区有未提交的改动，已拒绝自动 pull（先 commit / stash 再点更新）"
        before = hot.head_commit(repo)
        ok, out = self._git(["pull", "--ff-only"], timeout=120.0)
        if not ok:
            return False, f"git pull 失败：{out[:300]}"
        after = hot.head_commit(repo)
        if after and after == before:
            return True, "已经是最新版本（没有新提交）"
        changed = self._changed_files(before, after)
        note = f"已更新到 {hot.short(after) or '?'}（{len(changed)} 个文件）"
        if any(name in {"pyproject.toml", "uv.lock"} for name in changed):
            synced = self._sync_deps()
            note += f"；{synced}"
        return True, note

    def _changed_files(self, before: str, after: str) -> list[str]:
        if not (before and after):
            return []
        ok, out = self._git(["diff", "--name-only", before, after])
        return [line.strip() for line in out.splitlines() if line.strip()] if ok else []

    def _sync_deps(self) -> str:
        """依赖变了就 ``uv sync --frozen``（不跟上的话新代码 import 就会炸）。

        注意带不带 ``--extra dev``：**开发机上是带的**（否则一次换代就把 pytest / ruff
        从虚拟环境里抹掉，下次 `uv run pytest` 才悄悄装回来——看着像灵异事件）；
        生产上本来就没装 dev，不加也对。判断依据是「现在这个解释器里有没有 pytest」。
        """
        if not self.sync_deps:
            return "依赖有变化（已按设置跳过同步）"
        uv = shutil.which("uv")
        if not uv:
            return "依赖有变化但找不到 uv，请手动同步（新版本可能起不来）"
        args = [uv, "sync", "--frozen"]
        if importlib.util.find_spec("pytest") is not None:
            args += ["--extra", "dev"]
        try:
            proc = subprocess.run(
                args,
                cwd=str(hot.repo_root()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900.0,
                check=False,  # 失败原因在下面对的 returncode 判断里（要读出来给人看）
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"依赖同步失败：{exc}"
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-300:]
            return f"依赖同步失败（新版本可能起不来）：{tail}"
        return "依赖已同步"


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nte-match hotrun",
        description="热更新守护：不中断服务地换上新代码（git pull 也行）",
    )
    parser.add_argument("--host", default=os.getenv("NTE_HOST", "0.0.0.0"), help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.getenv("NTE_PORT", "8000")), help="监听端口")
    parser.add_argument("--interval", type=float, default=2.0, help="检测代码变化的间隔（秒）")
    parser.add_argument("--ready-timeout", type=float, default=hot.READY_TIMEOUT, help="等新进程就绪的超时")
    parser.add_argument("--drain-timeout", type=float, default=hot.DRAIN_TIMEOUT, help="等旧进程退出的超时")
    parser.add_argument("--no-watch", action="store_true", help="不自动检测变化，只接受管理端触发的更新")
    parser.add_argument("--no-deps-sync", action="store_true", help="依赖变化时不自动 uv sync")
    parser.add_argument(
        "--no-hold-socket",
        action="store_true",
        help="不持有监听套接字（顺序换代：更新时约 1 秒空窗）。Windows 默认如此，Linux 默认相反",
    )
    parser.add_argument(
        "--hold-socket",
        action="store_true",
        help="强制持有监听套接字（零中断交接）。仅在有把握的平台用，见 README",
    )
    args = parser.parse_args(argv)
    hold: bool | None = None
    if args.no_hold_socket:
        hold = False
    if args.hold_socket:
        hold = True
    supervisor = Supervisor(
        host=args.host,
        port=args.port,
        interval=args.interval,
        watch=not args.no_watch,
        ready_timeout=args.ready_timeout,
        drain_timeout=args.drain_timeout,
        sync_deps=not args.no_deps_sync,
        hold_socket=hold,
    )
    try:
        return supervisor.run()
    except KeyboardInterrupt:  # pragma: no cover - 手动 Ctrl+C
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["Child", "Supervisor", "main"]
