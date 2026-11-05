"""部署自举：缺 Go 侧车二进制就先编出来，**再**启动服务。

为什么放在启动路径上：源码部署（不是镜像）时 ``go/bin/`` 里什么都没有——卡片图会
安静退回纯文本、赛程生成退回纯 Python，功能都在但慢一个量级，而用户往往不知道
「只是没编译」。所以启动前看一眼：

* 二进制**不存在** → 编译；
* 二进制比 Go 源码**旧** → 也重编（改了渲染器忘了 build，症状和没编一样）；
* 这台机器上**没有 Go 工具链** → 只记一行日志并继续启动（退回文本，不影响可用性）；
* ``go/`` 目录不存在（镜像里只拷了 app/ 与 static/，二进制在 PATH）→ 什么都不做。

想彻底关掉：``NTE_GO_BUILD=0``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .logging_conf import get_logger

log = get_logger("gobuild")

#: 源码包 → 产物名（与 Dockerfile、两个客户端里的查找路径一致）
BINARIES: tuple[tuple[str, str], ...] = (
    ("ntecalc", "./cmd/ntecalc"),
    ("nterender", "./cmd/nterender"),
)

#: 仓库根（``app/`` 的上一层）
ROOT = Path(__file__).resolve().parent.parent
GO_DIR = ROOT / "go"
BIN_DIR = GO_DIR / "bin"

#: 单次编译的超时：第一次要下依赖，给得宽一点；之后都是秒级
BUILD_TIMEOUT = 600.0


def _suffix() -> str:
    return ".exe" if os.name == "nt" else ""


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _go_binary() -> str | None:
    """这台机器上的 ``go``（没有就返回 ``None``——那就只记日志、不折腾）。"""
    return shutil.which("go")


def _tag() -> str:
    """构建标记：``<短提交号>-<日期>``；不是 git 仓库就只留日期。

    它进 ``--version``，也就进卡片与帮助图的指纹——重编之后图不会继续用旧的。
    """
    stamp = time.strftime("%Y%m%d")
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return stamp
    sha = done.stdout.decode("utf-8", "replace").strip()
    return f"{sha}-{stamp}" if done.returncode == 0 and sha else stamp


def _newest_source() -> float:
    """``go/`` 下最新的源文件时间（用来判断二进制是不是比代码旧）。"""
    newest = 0.0
    for path in GO_DIR.rglob("*"):
        if path.suffix not in (".go", ".mod", ".sum") or not path.is_file():
            continue
        if BIN_DIR in path.parents:  # 产物目录本身不算
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _needs_build(name: str) -> tuple[bool, str]:
    """这个二进制要不要编，以及理由（日志里直接说清楚）。"""
    target = BIN_DIR / f"{name}{_suffix()}"
    if not target.exists():
        return True, "还没有二进制"
    try:
        built = target.stat().st_mtime
    except OSError:
        return True, "读不到二进制的修改时间"
    newest = _newest_source()
    if newest > built + 0.5:  # 容一格时间戳抖动的误差
        return True, "Go 源码比二进制新"
    return False, "已是最新"


def status() -> dict[str, Any]:
    """给诊断接口 / 自检用的一行状态（不编译、不写盘）。"""
    items = []
    for name, package in BINARIES:
        target = BIN_DIR / f"{name}{_suffix()}"
        need, reason = _needs_build(name)
        items.append(
            {
                "name": name,
                "package": package,
                "path": str(target),
                "exists": target.exists(),
                "missing": need,
                "reason": reason,
            }
        )
    return {
        "enabled": _env_flag("NTE_GO_BUILD", True),
        "toolchain": _go_binary() or "",
        "goDir": str(GO_DIR),
        "binaries": items,
    }


def _build(name: str, package: str) -> bool:
    """编译一个二进制；失败只记日志（调用方继续启动）。"""
    go = _go_binary()
    assert go is not None
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / f"{name}{_suffix()}"
    command = [
        go,
        "build",
        "-trimpath",
        "-ldflags",
        f"-s -w -X main.buildTag={_tag()}",
        "-o",
        str(target),
        package,
    ]
    env = {**os.environ, "CGO_ENABLED": "0"}
    started = time.perf_counter()
    log.info("正在编译 Go 侧车 | %s | 命令：%s", name, " ".join(command))
    try:
        done = subprocess.run(
            command,
            cwd=GO_DIR,
            capture_output=True,
            timeout=BUILD_TIMEOUT,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "编译超时（%.0fs），跳过 %s：本次启动照常进行，该功能退回纯 Python / 纯文本",
            BUILD_TIMEOUT,
            name,
        )
        return False
    except OSError as exc:
        log.warning("编译失败（启动命令出错）：%s | %s", name, exc)
        return False
    elapsed = (time.perf_counter() - started) * 1000
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        tail = "\n".join(detail.splitlines()[-8:])
        log.warning(
            "编译失败（退出码 %d），本次启动照常进行，%s 相关功能退回纯 Python / 纯文本\n%s",
            done.returncode,
            name,
            tail,
        )
        return False
    log.info("编译完成 | %s | %.0fms | %s", name, elapsed, target)
    return True


def ensure_ready(*, timeout: float | None = None) -> dict[str, Any]:
    """缺哪个编哪个；返回 ``{ok, built, skipped, reason}``（**不抛异常**）。

    调用点只有一处：命令行入口在起服务之前（见 :func:`app.cli.main`）。维护类命令
    （``--reset-key`` / ``--transfer-admin``）与 ``--check`` 不会走到这里——那些路径
    不该为了看一眼帮助就等一次编译。
    """
    global BUILD_TIMEOUT
    if timeout is not None:
        BUILD_TIMEOUT = timeout
    if not _env_flag("NTE_GO_BUILD", True):
        return {"ok": True, "built": [], "skipped": [], "reason": "已由 NTE_GO_BUILD 关闭"}
    if not GO_DIR.is_dir():
        # 镜像里只有 app/ 与 static/，二进制在 PATH 上；这里什么都不用做
        return {"ok": True, "built": [], "skipped": [], "reason": "没有 go/ 源码目录"}
    if _go_binary() is None:
        log.info(
            "这台机器上没有 Go 工具链，跳过编译侧车（卡片图退回纯文本、赛程生成走纯 Python；"
            "装上 Go 或直接放好 go/bin/ 下的二进制即可）"
        )
        return {"ok": False, "built": [], "skipped": [], "reason": "没有 Go 工具链"}
    built: list[str] = []
    skipped: list[str] = []
    for name, package in BINARIES:
        need, reason = _needs_build(name)
        if not need:
            skipped.append(name)
            continue
        log.info("Go 侧车需要编译 | %s | %s", name, reason)
        if _build(name, package):
            built.append(name)
        else:
            skipped.append(name)
    return {"ok": True, "built": built, "skipped": skipped, "reason": ""}
