"""终端彩色输出：**只在真终端上色**，且对齐不受颜色影响。

这两条都是「不测就会悄悄坏」的：

* 颜色码漏进管道 / 日志 / 测试捕获器里，输出就不是给人看的东西了
  （``\033[96m`` 之类的乱码，会让 `> help.txt`、CI 日志、正则提取全变脏）；
* ``f"{上色后的文本:<40}"`` 会把转义码也算进位宽，列会歪——所以补空格必须
  在 ``paint`` 之前做（见 ``console.pad``）。
"""

from __future__ import annotations

import io

from app import console


class _Tty(io.StringIO):
    """假装是终端（只有 ``isatty`` 为真这一件事重要）。"""

    def isatty(self) -> bool:  # pragma: no cover - 一行直给
        return True


def _fresh(monkeypatch, stream):
    """换掉 stdout 并清掉「要不要上色」的判定缓存（它按对象 id 记）。"""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("NTE_NO_COLOR", raising=False)
    monkeypatch.setattr(console.sys, "stdout", stream)
    console._decided.clear()  # 测试要绕开这个按 id 的缓存


def test_colors_on_a_tty(monkeypatch):
    _fresh(monkeypatch, _Tty())
    painted = console.paint("客户端", "cmd")
    assert painted.startswith("\033[") and painted.endswith(console.RESET)
    assert "客户端" in painted
    console._decided.clear()


def test_no_colors_when_not_a_tty(monkeypatch):
    """管道 / 重定向 / 测试捕获器：原样输出，一个转义码都不许有。"""
    _fresh(monkeypatch, io.StringIO())
    assert console.paint("客户端", "cmd") == "客户端"
    assert console.enabled() is False
    console._decided.clear()


def test_no_color_env_wins_even_on_a_tty(monkeypatch):
    """``NO_COLOR`` 是通行约定：设了就一定不上色（测试就靠它保证输出可断言）。"""
    _fresh(monkeypatch, _Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    console._decided.clear()
    assert console.paint("客户端", "cmd") == "客户端"
    console._decided.clear()


def test_unknown_style_is_ignored(monkeypatch):
    """写错的样式名不该炸，也不该输出半截转义码。"""
    _fresh(monkeypatch, _Tty())
    assert console.paint("x", "没有这个样式") == "x"
    console._decided.clear()


def test_pad_counts_plain_text(monkeypatch):
    """对齐要按**纯文本长度**补空格（上色后长度会变，先补再上色）。"""
    _fresh(monkeypatch, _Tty())
    padded = console.pad("abc", 8)
    assert len(padded) == 8
    assert console.paint(padded, "cmd").replace("\033[96m", "").replace(console.RESET, "") == padded
    assert console.pad("a-very-long-name", 4) == "a-very-long-name", "超长不该被截断"
    console._decided.clear()


def test_width_counts_wide_chars_as_two():
    """列宽要按终端显示宽度：一个汉字两列（按 ``len`` 算就一定会歪）。"""
    assert console.width("abc") == 3
    assert console.width("忘了管理员密钥") == 14
    assert console.width("python -m app --help") == 20


def test_wrap_never_splits_a_command():
    """折行不能从连字符处把命令切开（``--delete-old`` 断成两行谁也认不出来）。"""
    lines = console.wrap(
        "交接服务器管理员。目标可填 uid / QQ / 名字片段，不给就交互式选择；"
        "另有 --delete-old（把旧管理员删号）/ --keep-old（降级，默认）/ --key <新密钥>",
        40,
    )
    joined = " ".join(lines)
    for token in ("--delete-old", "--keep-old", "--key", "<新密钥>"):
        assert token in joined, f"{token} 被拆开了：{lines}"
    for line in lines:
        assert not line.startswith(("，", "。", "；", "：", "、", "）")), f"标点跑到行首：{line}"
    # 允许标点把行尾多撑一两列，但不能离谱
    assert all(console.width(line) <= 42 for line in lines), lines
