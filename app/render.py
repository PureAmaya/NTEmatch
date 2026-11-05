"""Go 出图侧车（``nterender``）的客户端：**一次调用画一张图**，失败即回退文本。

为什么要一个单独的二进制：出图（比赛卡片 / 结果卡片 / 帮助图 / 分享图）是纯几何 +
文字排版的活，用 Go 画比 Pillow 快一个量级，也省掉 Python 侧一台 26MB 的画布和
一层 C 绑定。协议很简单——stdin 一行 JSON 进、stdout 一行 JSON 出，图写到一个
指定的路径上：

    {"kind":"event|result|help|share","site":"https://…","out":"/path/x.png","payload":{…}}

回退是硬要求：二进制缺失、版本不符、超时、返回不合法、写不出文件，一律返回 ``None``，
调用方继续走纯文本推送（文案里本来就带规则摘要与逐场比分，少了图信息不缺）。

相关环境变量（都可以不设，默认值即可跑）：

===========================  ==================================================
``NTE_RENDER``               ``0``/``false``/``off`` 时彻底关闭出图
``NTE_RENDER_BIN``           指定二进制路径（默认找 ``go/bin/nterender`` 与 PATH）
``NTE_RENDER_TIMEOUT``       单次出图超时秒数（默认 20），超时会杀掉进程并回退
===========================  ==================================================
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .logging_conf import get_logger

log = get_logger("render")

#: 协议版本：必须与 go/cmd/nterender/main.go 的 Protocol 一致。
PROTOCOL_VERSION = 2

_BINARY_NAME = "nterender"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_TIMEOUT = 20.0


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _binary_candidates() -> list[Path]:
    """按优先级给出二进制候选路径（第一次命中即用）。"""
    candidates: list[Path] = []
    custom = (os.getenv("NTE_RENDER_BIN") or "").strip()
    if custom:
        candidates.append(Path(custom))
    candidates.append(_REPO_ROOT / "go" / "bin" / _BINARY_NAME)
    found = shutil.which(_BINARY_NAME)
    if found:
        candidates.append(Path(found))
    candidates.append(_REPO_ROOT / "go" / "bin" / f"{_BINARY_NAME}.exe")
    return candidates


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


class _Renderer:
    """二进制探测 + 调用。**不做常驻进程**：一张图几十毫秒，进程启动那几毫秒不值一提。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probed = False
        self._path: Path | None = None
        self._reason = ""
        self._tag = ""
        self._font = ""
        self._timeout = _DEFAULT_TIMEOUT
        self._stats = {"calls": 0, "ok": 0, "fallback": 0, "last_ms": 0.0}

    # ---------------------------------------------------------------- 状态
    def _probe(self) -> None:
        if self._probed:
            return
        self._probed = True
        try:
            self._timeout = float(os.getenv("NTE_RENDER_TIMEOUT") or _DEFAULT_TIMEOUT)
        except ValueError:
            log.warning("NTE_RENDER_TIMEOUT 不是数字，按 %.1fs 处理", _DEFAULT_TIMEOUT)
            self._timeout = _DEFAULT_TIMEOUT
        if not _env_flag("NTE_RENDER", True):
            self._reason = "已由 NTE_RENDER 关闭"
            log.info("出图侧车未启用（NTE_RENDER=0），卡片推送走纯文本")
            return
        for path in _binary_candidates():
            if not _is_executable(path):
                continue
            info = self._read_version(path)
            if info is None:
                log.warning("出图侧车版本探测失败，忽略该文件 | path=%s", path)
                continue
            protocol, tag = info
            if protocol != PROTOCOL_VERSION:
                log.warning(
                    "出图侧车协议版本不符，已回退纯文本 | path=%s | 二进制=%d | 期望=%d",
                    path,
                    protocol,
                    PROTOCOL_VERSION,
                )
                self._reason = f"协议版本不符：{protocol} != {PROTOCOL_VERSION}"
                return
            self._path = path
            self._tag = tag
            self._font = self._read_font(path)
            log.info(
                "出图侧车可用 | path=%s | protocol=%d | tag=%s | 字体=%s",
                path,
                protocol,
                tag,
                self._font or "?",
            )
            return
        self._reason = "未找到 nterender 二进制"
        log.info(
            "未找到 nterender 二进制，卡片推送走纯文本"
            "（构建：cd go && go build -o bin/nterender ./cmd/nterender）"
        )

    @staticmethod
    def _read_version(path: Path) -> tuple[int, str] | None:
        """``nterender --version`` 输出 ``nterender <protocol> <tag>``。"""
        try:
            done = subprocess.run(
                [str(path), "--version"], capture_output=True, timeout=5.0, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        parts = done.stdout.decode("utf-8", "replace").split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1]), (parts[2] if len(parts) > 2 else "")
        except ValueError:
            return None

    @staticmethod
    def _read_font(path: Path) -> str:
        """``nterender --font`` 输出实际用到的字体文件（并进卡片指纹）。"""
        try:
            done = subprocess.run([str(path), "--font"], capture_output=True, timeout=10.0, check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        if done.returncode != 0:
            return ""
        return done.stdout.decode("utf-8", "replace").strip()

    def _usable(self) -> bool:
        return self._path is not None

    def available(self) -> bool:
        with self._lock:
            self._probe()
            return self._usable()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            self._probe()
            return {
                "enabled": self._usable(),
                "path": str(self._path) if self._path else "",
                "reason": self._reason,
                "tag": self._tag,
                "font": self._font,
                "stats": dict(self._stats),
            }

    def signature(self) -> str:
        """「画法」指纹：协议版本 + 构建标记 + **二进制指纹** + 实际字体。

        卡片是按内容哈希缓存的；把这些算进去，换二进制 / 换字体之后同一份内容
        会得到新的指纹，旧图自然失效（踩过：换了字体重启，群里还是旧那张方块图）。

        为什么要 binary 的 size/mtime：`buildTag` 只有一个词（docker / local），
        改了画法重新编译、tag 却不变，指纹就看不出来——图和代码又对不上了。
        """
        with self._lock:
            self._probe()
            build = ""
            if self._path is not None:
                try:
                    st = self._path.stat()
                    build = f"{st.st_size:x}-{int(st.st_mtime)}"
                except OSError:
                    build = ""
            return f"r{PROTOCOL_VERSION}|{self._tag}|{build}|{self._font}"

    # ---------------------------------------------------------------- 调用
    def render(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        site: str = "",
        quality: int = 0,
        timeout: float | None = None,
    ) -> tuple[bytes, int, int] | None:
        """画一张图；任何异常都返回 ``None``（调用方回退纯文本）。"""
        with self._lock:
            self._probe()
            if not self._usable():
                return None
            assert self._path is not None
            limit = self._timeout if timeout is None else timeout
            request = {
                "kind": kind,
                "site": site,
                "payload": payload,
            }
            if quality:
                request["quality"] = quality
            with tempfile.TemporaryDirectory(prefix="nte-render-") as tmpdir:
                out = Path(tmpdir) / "out.bin"
                request["out"] = str(out)
                body = json.dumps(request, ensure_ascii=False).encode("utf-8")
                started = time.perf_counter()
                try:
                    done = subprocess.run(
                        [str(self._path)],
                        input=body,
                        capture_output=True,
                        timeout=limit,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    self._record(f"渲染超时（{limit:.0f}s），本次退回纯文本")
                    return None
                except OSError as exc:
                    self._record(f"进程启动失败: {exc}")
                    return None
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._stats["calls"] += 1
                self._stats["last_ms"] = round(elapsed_ms, 1)
                if done.returncode != 0:
                    detail = done.stderr.decode("utf-8", "replace").strip()[-200:]
                    self._record(f"渲染失败（退出码 {done.returncode}）| {detail}")
                    return None
                try:
                    resp = json.loads(done.stdout.decode("utf-8", "replace") or "{}")
                except json.JSONDecodeError:
                    self._record("响应不是合法 JSON")
                    return None
                if not isinstance(resp, dict) or not resp.get("ok"):
                    self._record(f"渲染未成功 | {resp.get('error') if isinstance(resp, dict) else '?'}")
                    return None
                try:
                    data = out.read_bytes()
                except OSError as exc:
                    self._record(f"读不到渲染结果: {exc}")
                    return None
                if not data:
                    self._record("渲染结果为空")
                    return None
                self._stats["ok"] += 1
                log.debug(
                    "出图完成 | kind=%s | %s×%s | %.0fms",
                    kind,
                    resp.get("width"),
                    resp.get("height"),
                    elapsed_ms,
                )
                return data, int(resp.get("width") or 0), int(resp.get("height") or 0)

    def _record(self, message: str) -> None:
        self._stats["fallback"] += 1
        log.warning("出图侧车调用失败，本次退回纯文本 | %s", message)


_renderer = _Renderer()


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def available() -> bool:
    """这台机器上能不能出图（二进制在不在、版本对不对）。"""
    return _renderer.available()


def describe() -> dict[str, Any]:
    return _renderer.describe()


def signature() -> str:
    return _renderer.signature()


def render(
    kind: str,
    payload: dict[str, Any],
    *,
    site: str = "",
    quality: int = 0,
    timeout: float | None = None,
) -> tuple[bytes, int, int] | None:
    """画一张图，返回 ``(字节, 宽, 高)``；不可用或失败返回 ``None``。"""
    return _renderer.render(kind, payload, site=site, quality=quality, timeout=timeout)
