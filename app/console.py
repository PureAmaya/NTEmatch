"""终端彩色输出：让帮助与提示一眼分得清重点，而不是一屏黑字。

三条约束，每条都是踩过的：

* **零依赖**：不引 ``colorama``——它只是 uvicorn 的传递依赖，哪天不在了 CLI 就花屏；
* **不是终端就不上色**：管道 / 重定向 / CI 日志里出现 ``\\033[…`` 是纯噪音，
  还会把日志文件弄脏；同时尊重 ``NO_COLOR`` 这个通行约定（测试里就靠它保证输出可断言）；
* **Windows 得主动开 VT**：老 conhost 默认不解析 ANSI 转义，要自己按一下
  ``SetConsoleMode`` 的位（失败也无所谓，顶多没颜色）。

用法：``paint("文本", "cmd")``；样式名是语义化的（见 :data:`STYLES`），
不要在调用处直接写颜色码——不然各处颜色会各不相同。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import IO

RESET = "\033[0m"

#: 语义化样式 → ANSI 码。**加新样式前先想清楚它代表什么**，别按颜色取名。
STYLES: dict[str, str] = {
    "bold": "1",
    "dim": "2",
    "ok": "32",  # 成功 / 已完成
    "warn": "33",  # 要注意，但没出错
    "err": "31",  # 出错
    "cmd": "96",  # 命令名（亮青：与站点主题色同系）
    "head": "95",  # 分组标题（亮品红）
    "note": "90",  # 补充说明 / 灰
    "accent": "36",  # 关键字
    "key": "93",  # 需要抄走的东西（密钥）
    "rule": "90",  # 分隔线
}

#: 每路输出只判定一次「要不要上色」（结果与是否 Windows-VT 无关，故只需一次）
_decided: dict[int, bool] = {}
_vt_ready = False

#: 折行时**不许出现在行首**的收尾标点（中文排版习惯：宁可上一行稍微超一点点）
_TRAILING = "，。；：、！？）】》」』”’%"


def _enable_windows_vt() -> None:
    """在 Windows 上打开「解析 ANSI 转义」这个开关（只做一次，失败就放弃）。"""
    global _vt_ready
    if _vt_ready or os.name != "nt":
        return
    _vt_ready = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE / STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL
    # 拿不到控制台就算了（输出重定向、没有窗口）：顶多没颜色，不影响任何功能。
    # 捕具体异常而不是 Exception：这里能出的就是「调用失败」这一类。
    except (OSError, AttributeError, ValueError):
        return


def _detect(stream: IO[str]) -> bool:
    if os.environ.get("NO_COLOR"):  # 通行约定：设了就不上色（值不重要）
        return False
    if os.environ.get("NTE_NO_COLOR"):  # 本项目自己的开关（意思更直白）
        return False
    if (os.environ.get("TERM") or "").lower() == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # 被换成了奇怪的对象（如测试的捕获器）
        return False


def enabled(stream: IO[str] | None = None) -> bool:
    """这路输出要不要上色（不是终端就 ``False``，见模块开头）。"""
    out = stream if stream is not None else sys.stdout
    key = id(out)
    known = _decided.get(key)
    if known is None:
        known = _detect(out)
        _decided[key] = known
    if known:
        _enable_windows_vt()
    return known


def paint(text: str, *styles: str) -> str:
    """给文本上色；**不支持的场合原样返回**（所以调用处不用自己判断）。"""
    codes = [STYLES[name] for name in styles if name in STYLES]
    if not text or not codes or not enabled():
        return text
    return f"\033[{';'.join(codes)}m{text}{RESET}"


def width(text: str) -> int:
    """文本在终端里占几列：东亚宽字符算 2 列。

    不这么算，中文与英文命令混排时列一定歪——``len("忘了管理员密钥")`` 是 8，
    终端里却占 16 列（帮助里的「常见操作」那一段正是这种混排）。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def pad(text: str, columns: int) -> str:
    """按**显示宽度**补空格——用来对齐。

    必须在 :func:`paint` **之前**调用：上色后字符串里多了转义码，
    ``f"{colored:<40}"`` 会把那些码也算进去，列就歪了。
    """
    return text + " " * max(0, columns - width(text))


def wrap(text: str, columns: int) -> list[str]:
    """按显示宽度折行（返回若干行，不含换行符）。

    两条与 ``textwrap`` 不同的地方，都是为了中文与命令行参数：

    * 宽度按**终端列**算（汉字两列）——``textwrap`` 用 ``len()``，中文会算少一截，
      排出来比实际窄，行尾就挤出屏幕了；
    * **命令与参数不从中间切开**（``textwrap`` 默认会在连字符处断，
      ``--delete-old`` 被拆成两行谁也认不出来）；中文长句没有空格，则按字符断。
    """
    units: list[str] = []
    for segment in str(text).split(" "):
        if not segment:
            continue
        if width(segment) <= columns:
            units.append(segment)
            continue
        chunk = ""
        for ch in segment:
            if chunk and width(chunk) + width(ch) > columns:
                # 中文排版的忌讳：收尾标点跑到下一行开头。宁可这行多占一两列
                if ch in _TRAILING:
                    chunk += ch
                    units.append(chunk)
                    chunk = ""
                    continue
                units.append(chunk)
                chunk = ""
            chunk += ch
        if chunk:
            units.append(chunk)
    lines: list[str] = []
    line = ""
    for unit in units:
        candidate = unit if not line else f"{line} {unit}"
        if width(candidate) <= columns or not line:
            line = candidate
            continue
        lines.append(line)
        line = unit
    lines.append(line)
    return lines or [""]
