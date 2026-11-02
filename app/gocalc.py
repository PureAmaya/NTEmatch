"""Go 计算侧车（``ntecalc``）的客户端：懒启动常驻进程、逐行 JSON 收发、失败即回退。

为什么只把「动态轮换赛程」交给 Go：这是全站唯一「CPU 密集 + 持全局写锁 +
跑在事件循环里」的操作（12 个起点 × 每局 200 次抽样 × 枚举分队）。
其余计算（排名、状态组装）规模只有几十人 × 几十局，走一次 IPC 的序列化开销
就比 Python 自己算更贵，因此**不**放进侧车。

回退是硬要求：二进制缺失、启动失败、超时、返回结构不合法、协议版本不符，
一律返回 ``None``，调用方继续用 :mod:`app.league` 里的原实现。
回退不会影响正确性，只会退回到优化前的速度。

相关环境变量（都可以不设，默认值即可跑）：

===========================  ==================================================
``NTE_GOCALC``               ``0``/``false``/``off`` 时彻底关闭侧车
``NTE_GOCALC_BIN``           指定二进制路径（默认找 ``go/bin/ntecalc`` 与 PATH）
``NTE_GOCALC_TIMEOUT``       单次调用超时秒数（默认 5），超时会杀掉进程并回退
===========================  ==================================================
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .logging_conf import get_logger

log = get_logger("gocalc")

# 协议版本与操作名：必须与 go/internal/protocol/protocol.go 保持一致。
PROTOCOL_VERSION = 1
OP_LEAGUE_ROTATION = "league.rotation"

_BINARY_NAME = "ntecalc"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_TIMEOUT = 5.0
_FAILURE_LIMIT = 3  # 连续失败多少次开始熔断
_COOLDOWN_SECONDS = 60.0  # 熔断时长：期间直接用 Python 实现，不再尝试拉起


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _binary_candidates() -> list[Path]:
    """按优先级给出二进制候选路径（第一次命中即用）。"""
    candidates: list[Path] = []
    custom = (os.getenv("NTE_GOCALC_BIN") or "").strip()
    if custom:
        candidates.append(Path(custom))
    candidates.append(_REPO_ROOT / "go" / "bin" / _BINARY_NAME)
    found = shutil.which(_BINARY_NAME)
    if found:
        candidates.append(Path(found))
    # Windows 上二进制带扩展名；Linux/Docker 里不会命中，纯属兜底。
    candidates.append(_REPO_ROOT / "go" / "bin" / f"{_BINARY_NAME}.exe")
    return candidates


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


class _Client:
    """常驻子进程 + 串行请求。

    串行是刻意的：赛程生成一年点几次，并发收益为零，而串行让「读一行响应」
    的配对逻辑简单到不需要任何多路复用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._path: Path | None = None
        self._reason = ""  # 不可用的原因（一次定论，不再反复探测）
        self._probed = False
        self._failures = 0
        self._disabled_until = 0.0
        self._timeout = _DEFAULT_TIMEOUT
        self._next_id = 0
        self._closed = False
        self._stats = {"calls": 0, "ok": 0, "fallback": 0, "last_ms": 0.0}

    # ---------------------------------------------------------------- 状态
    def available(self) -> bool:
        with self._lock:
            self._probe()
            return self._usable()

    def describe(self) -> dict[str, Any]:
        """给 ``/api/diagnostics`` 用的一行状态。"""
        with self._lock:
            self._probe()
            return {
                "enabled": self._usable(),
                "path": str(self._path) if self._path else "",
                "reason": self._reason,
                "running": bool(self._proc and self._proc.poll() is None),
                "failures": self._failures,
                "cooldown": round(max(0.0, self._disabled_until - time.monotonic()), 1),
                "stats": dict(self._stats),
            }

    def _probe(self) -> None:
        """只做一次：开关、二进制存在性、协议版本。"""
        if self._probed:
            return
        self._probed = True
        try:
            self._timeout = float(os.getenv("NTE_GOCALC_TIMEOUT") or _DEFAULT_TIMEOUT)
        except ValueError:
            log.warning("NTE_GOCALC_TIMEOUT 不是数字，按 %.1fs 处理", _DEFAULT_TIMEOUT)
            self._timeout = _DEFAULT_TIMEOUT
        if not _env_flag("NTE_GOCALC", True):
            self._reason = "已由 NTE_GOCALC 关闭"
            log.info("Go 计算侧车未启用（NTE_GOCALC=0），赛程生成走 Python 实现")
            return
        for path in _binary_candidates():
            if not _is_executable(path):
                continue
            version = self._read_version(path)
            if version is None:
                log.warning("Go 计算侧车版本探测失败，忽略该文件 | path=%s", path)
                continue
            if version != PROTOCOL_VERSION:
                log.warning(
                    "Go 计算侧车协议版本不符，已回退 Python 实现 | path=%s | 二进制=%d | 期望=%d",
                    path,
                    version,
                    PROTOCOL_VERSION,
                )
                self._reason = f"协议版本不符：{version} != {PROTOCOL_VERSION}"
                return
            self._path = path
            log.info("Go 计算侧车可用 | path=%s | protocol=%d", path, PROTOCOL_VERSION)
            return
        self._reason = "未找到 ntecalc 二进制"
        log.info(
            "未找到 ntecalc 二进制，赛程生成走 Python 实现"
            "（构建：cd go && go build -o bin/ntecalc ./cmd/ntecalc）"
        )

    @staticmethod
    def _read_version(path: Path) -> int | None:
        """``ntecalc --version`` 输出 ``ntecalc <protocol> <tag>``。"""
        try:
            done = subprocess.run([str(path), "--version"], capture_output=True, timeout=3.0, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        parts = done.stdout.decode("utf-8", "replace").split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    def _usable(self) -> bool:
        if self._path is None or self._closed:
            return False
        return time.monotonic() >= self._disabled_until

    # ---------------------------------------------------------------- 进程
    def _ensure_proc(self) -> subprocess.Popen[bytes]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._reset_proc()
        assert self._path is not None
        started = time.perf_counter()
        proc = subprocess.Popen(
            [str(self._path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._proc = proc
        threading.Thread(
            target=self._pump_stderr,
            args=(proc,),
            name="gocalc-stderr",
            daemon=True,
        ).start()
        log.info(
            "Go 计算侧车已启动 | path=%s | pid=%d | 启动耗时=%.1fms",
            self._path,
            proc.pid,
            (time.perf_counter() - started) * 1000,
        )
        return proc

    def _pump_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        """把侧车的 stderr 转进本站日志（debug 级，避免正常运行时刷屏）。"""
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    log.debug("ntecalc | %s", text)
        except (OSError, ValueError):
            pass

    def _reset_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=1.0)

    def shutdown(self, *, quiet: bool = False) -> None:
        """收掉子进程。

        ``quiet`` 供 atexit 用：那时日志 handler 可能已经关闭，
        再写一行 INFO 只会换来一条 ``ValueError: I/O operation on closed file``。
        """
        with self._lock:
            self._closed = True
            proc = self._proc
            if proc is not None and not quiet:
                with contextlib.suppress(ValueError, OSError):
                    log.info("关闭 Go 计算侧车 | pid=%d", proc.pid)
            self._reset_proc()

    # ---------------------------------------------------------------- 调用
    def call(self, op: str, args: dict[str, Any], timeout: float | None = None) -> Any | None:
        """同步调用一次；任何异常/超时/协议不符都返回 ``None``（调用方回退）。"""
        with self._lock:
            self._probe()
            if not self._usable():
                return None
            try:
                proc = self._ensure_proc()
            except OSError as exc:
                self._record_failure(f"进程启动失败: {exc}")
                return None
            if proc.stdin is None or proc.stdout is None:
                self._record_failure("子进程管道不可用")
                return None

            limit = self._timeout if timeout is None else timeout
            self._next_id += 1
            req_id = self._next_id
            payload = (
                json.dumps(
                    {"id": req_id, "op": op, "args": args},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )

            started = time.perf_counter()
            guard = threading.Timer(limit, self._kill_quietly, args=(proc,))
            guard.daemon = True
            guard.start()
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                raw = proc.stdout.readline()
            except (OSError, ValueError) as exc:
                self._record_failure(f"收发失败: {exc}")
                return None
            finally:
                guard.cancel()
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._stats["calls"] += 1
            self._stats["last_ms"] = round(elapsed_ms, 1)

            if not raw:
                self._record_failure(f"进程无响应（{elapsed_ms:.0f}ms，可能已超时被杀）")
                return None
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError:
                self._record_failure("响应不是合法 JSON")
                return None
            if not isinstance(resp, dict) or resp.get("id") != req_id:
                self._record_failure("响应 id 与请求不匹配")
                return None
            if not resp.get("ok"):
                message = str(resp.get("error") or "未知错误")
                if message.startswith("bad input"):
                    # 配置本身不合法：Python 侧会用原实现给出同样明确的报错
                    log.debug("Go 计算侧车拒绝请求 | op=%s | %s", op, message)
                    self._stats["fallback"] += 1
                    return None
                self._record_failure(f"计算失败: {message}")
                return None

            self._failures = 0
            self._stats["ok"] += 1
            log.debug("Go 计算侧车完成 | op=%s | 耗时=%.1fms", op, elapsed_ms)
            return resp.get("data")

    @staticmethod
    def _kill_quietly(proc: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(Exception):
            proc.kill()

    def _record_failure(self, message: str) -> None:
        self._failures += 1
        self._stats["fallback"] += 1
        log.warning("Go 计算侧车调用失败，回退 Python 实现 | 连续失败=%d | %s", self._failures, message)
        self._reset_proc()
        if self._failures >= _FAILURE_LIMIT:
            self._disabled_until = time.monotonic() + _COOLDOWN_SECONDS
            log.warning(
                "Go 计算侧车熔断 %.0fs（期间赛程生成走 Python 实现，不影响正确性）",
                _COOLDOWN_SECONDS,
            )


_client = _Client()
atexit.register(lambda: _client.shutdown(quiet=True))


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def available() -> bool:
    """侧车是否可用（只做一次探测，之后是内存判断）。"""
    return _client.available()


def describe() -> dict[str, Any]:
    return _client.describe()


def shutdown() -> None:
    _client.shutdown()


def rotation(
    pool: Sequence[tuple[str, str]],
    team_size: int,
    total_rounds: int,
    seed: int | None,
    fair: bool,
) -> list[dict[str, list[str]]] | None:
    """动态轮换赛程的搜索（对应 ``league.generate_rotation_rounds`` 的搜索部分）。

    ``pool`` 是 ``(id, name)`` 的序列，顺序即 Python 侧选手池顺序——
    顺序影响洗牌结果，必须原样传。

    返回每局 ``{"a": [...], "b": [...], "resting": [...]}``；不可用或不合法时返回
    ``None``，调用方回退到 Python 实现。
    """
    if not _client.available():
        return None
    if seed is not None:
        seed = int(seed)  # bool 也走这里：True 与 1 在 Python 的 Random 里等价
        if not (-(2**63) <= seed < 2**63):
            # 协议里 seed 是 int64；超大 seed 交给 Python 实现，避免两边序列分叉
            log.debug("seed 超出 int64 范围，跳过侧车 | seed=%s", seed)
            return None
    players = [{"id": pid, "name": name} for pid, name in pool]
    data = _client.call(
        OP_LEAGUE_ROTATION,
        {
            "players": players,
            "teamSize": team_size,
            "totalRounds": total_rounds,
            "seed": seed,
            "fair": fair,
        },
    )
    if not isinstance(data, dict):
        return None
    rounds = data.get("rounds")
    if not _valid_rounds(rounds, {pid for pid, _ in pool}, team_size, total_rounds):
        log.warning(
            "Go 计算侧车返回的赛程结构不合法，回退 Python 实现 | 局数=%s | 每队=%s",
            len(rounds) if isinstance(rounds, list) else "?",
            team_size,
        )
        return None
    return rounds


def _valid_rounds(rounds: Any, pool_ids: set[str], team_size: int, total_rounds: int) -> bool:
    """结构自检：宁可回退，也不能把非法赛程写进库。"""
    if not isinstance(rounds, list) or len(rounds) != total_rounds:
        return False
    for rnd in rounds:
        if not isinstance(rnd, dict):
            return False
        a, b = rnd.get("a"), rnd.get("b")
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        if len(a) != team_size or len(b) != team_size:
            return False
        ids = [*a, *b]
        if len(set(ids)) != len(ids):
            return False
        if any(not isinstance(pid, str) or pid not in pool_ids for pid in ids):
            return False
    return True
