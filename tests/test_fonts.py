"""字体查找：**装在哪都要认出来**。

以前这里只认几条写死的路径，于是「照文档装了字体，图里的中文还是方框」——
日志里那句「没有可用的中文字体」正是它。各发行版的字体目录都不一样：

* Debian / Ubuntu：``/usr/share/fonts/opentype/noto/``（fonts-noto-cjk）
* RHEL / Alma / Rocky / Fedora：``/usr/share/fonts/google-noto-cjk/``
* Arch：``/usr/share/fonts/noto-cjk/``；Alpine：``/usr/share/fonts/noto/``

下面把四层查找逐条钉住（环境变量 → 常见路径 → 扫目录 → 内置兜底），
用例全部用临时目录造「字体文件」，不依赖跑测试的这台机器上真装了中文字体。
"""

from __future__ import annotations

import pytest

from app import fonts


@pytest.fixture(autouse=True)
def _fresh_cache():
    """查找结果按种类缓存，换了环境变量 / 目录必须重来。"""
    fonts.reset()
    yield
    fonts.reset()


def _isolate(monkeypatch, tmp_path, *, cjk: tuple[str, ...] = ()) -> None:
    """把查找范围收进临时目录（免得真扫到这台机器上的字体，测试就随环境飘了）。"""
    monkeypatch.setattr(fonts, "CJK_PATHS", cjk)
    monkeypatch.setattr(fonts, "_FONT_DIRS", (str(tmp_path),))


# --------------------------------------------------------------------------- #
# 第一层：环境变量
# --------------------------------------------------------------------------- #
def test_env_var_wins(tmp_path, monkeypatch):
    """``NTE_FONT_CJK`` 指定了就听它的（自建镜像 / 字体放在奇怪地方）。"""
    mine = tmp_path / "MyFont.ttc"
    mine.write_bytes(b"not a real font")
    monkeypatch.setenv("NTE_FONT_CJK", str(mine))
    _isolate(monkeypatch, tmp_path)
    assert fonts.resolve("cjk") == mine


def test_env_var_pointing_nowhere_falls_through(tmp_path, monkeypatch):
    """环境变量写错了不该把人锁死：记一句警告，继续按默认顺序找。"""
    monkeypatch.setenv("NTE_FONT_CJK", str(tmp_path / "nope.ttc"))
    _isolate(monkeypatch, tmp_path)
    assert fonts.resolve("cjk") is None


# --------------------------------------------------------------------------- #
# 第二层：常见路径
# --------------------------------------------------------------------------- #
def test_known_path_is_used(tmp_path, monkeypatch):
    mine = tmp_path / "NotoSansCJK-Regular.ttc"
    mine.write_bytes(b"x")
    _isolate(monkeypatch, tmp_path, cjk=(str(mine),))
    assert fonts.resolve("cjk") == mine


# --------------------------------------------------------------------------- #
# 第三层：扫字体目录
# --------------------------------------------------------------------------- #
def test_scan_finds_it_wherever_the_distro_put_it(tmp_path, monkeypatch):
    """扫目录：不管发行版塞在哪一层子目录都能找到。"""
    deep = tmp_path / "opentype" / "noto"
    deep.mkdir(parents=True)
    font = deep / "NotoSansCJK-Regular.ttc"
    font.write_bytes(b"x")
    _isolate(monkeypatch, tmp_path)
    assert fonts.resolve("cjk") == font


def test_scan_accepts_the_names_we_promise(tmp_path, monkeypatch):
    """思源黑体的常见叫法都要认（SourceHanSans / NotoSansSC / 文泉驿…）。"""
    _isolate(monkeypatch, tmp_path)
    for name in ("SourceHanSansSC-Regular.otf", "NotoSansSC-Regular.otf", "wqy-zenhei.ttc"):
        for old in tmp_path.iterdir():
            old.unlink()
        (tmp_path / name).write_bytes(b"x")
        fonts.reset()
        assert fonts.resolve("cjk") == tmp_path / name, name


def test_scan_ignores_unrelated_files(tmp_path, monkeypatch):
    """不是字体的文件、不相干的字体都不认（否则会挑中一支画不出中文的）。"""
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "DejaVuSansMono.ttf").write_bytes(b"x")
    (tmp_path / "readme.txt").write_bytes(b"x")
    assert fonts.resolve("cjk") is None


# --------------------------------------------------------------------------- #
# 兜底
# --------------------------------------------------------------------------- #
def test_mono_falls_back_to_cjk(tmp_path, monkeypatch):
    """只有中文字体时，拿它显示数字也比方框强。"""
    cjk = tmp_path / "NotoSansCJK-Regular.ttc"
    cjk.write_bytes(b"x")
    monkeypatch.setattr(fonts, "CJK_PATHS", (str(cjk),))
    monkeypatch.setattr(fonts, "MONO_PATHS", ())
    monkeypatch.setattr(fonts, "_MONO_PREFIXES", ())
    monkeypatch.setattr(fonts, "_FONT_DIRS", ())
    assert fonts.resolve("mono") == cjk


def test_load_survives_a_broken_font_file(tmp_path, monkeypatch):
    """字体文件坏掉（不是真 TrueType）也要能画：退回内置位图字体，不抛异常。"""
    broken = tmp_path / "broken.ttc"
    broken.write_bytes(b"not a font at all")
    _isolate(monkeypatch, tmp_path, cjk=(str(broken),))
    font = fonts.load("cjk", 20)
    assert font is not None
    assert font.getlength("中文") > 0  # 内置字体也能量出宽度（字形是方框，但不崩）


def test_load_caches_per_kind_and_size():
    """同一（种类, 字号）只解析一次——卡片一次渲染要用十几个字号。"""
    assert fonts.load("cjk", 26) is fonts.load("cjk", 26)
    assert fonts.load("cjk", 26) is not fonts.load("cjk", 27)
