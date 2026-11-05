"""启动自举：缺侧车二进制就先编，编不了也不能挡住启动。

这条链路的价值全在「出问题时不会更糟」：没有 Go 工具链、编译报错、超时——
任何一种都必须**只记日志**，服务照常起来，只是出图 / 赛程搜索退回纯 Python。
所以这里既测「该编的时候编了」，也测「不该编 / 编不了的时候干净退出」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app import gobuild


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 go/ 与 go/bin/ 换成临时目录（别碰仓库里真正的产物）。"""
    go_dir = tmp_path / "go"
    bin_dir = go_dir / "bin"
    (go_dir / "cmd" / "nterender").mkdir(parents=True)
    (go_dir / "cmd" / "nterender" / "main.go").write_text("package main\n", encoding="utf-8")
    monkeypatch.setattr(gobuild, "GO_DIR", go_dir)
    monkeypatch.setattr(gobuild, "BIN_DIR", bin_dir)
    return go_dir, bin_dir


@pytest.fixture
def fake_go(tmp_path, monkeypatch):
    """一个「假 go」：只按 ``-o <路径>`` 写个文件出来，用来验证命令拼得对。"""
    script = tmp_path / "fake-go"
    script.write_text(
        "#!/bin/sh\n"
        "out=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-o\" ]; then out=\"$2\"; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        "[ -n \"$out\" ] && printf '#!/bin/sh\\necho fake\\n' > \"$out\" && chmod +x \"$out\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(gobuild, "_go_binary", lambda: str(script))
    return script


def test_missing_binary_is_built(sandbox, fake_go):
    """二进制不在 → 编出来（并且落在 go/bin 下、带 tag）。"""
    _go_dir, bin_dir = sandbox
    result = gobuild.ensure_ready()
    assert result["ok"] is True
    assert set(result["built"]) == {name for name, _ in gobuild.BINARIES}
    for name, _package in gobuild.BINARIES:
        assert (bin_dir / f"{name}{gobuild._suffix()}").exists()


def test_fresh_binary_is_not_rebuilt(sandbox, fake_go):
    """已经在、且比源码新 → 一次编译都不发起。"""
    _go_dir, bin_dir = sandbox
    bin_dir.mkdir(parents=True)
    newer = gobuild._newest_source() + 10
    for name, _package in gobuild.BINARIES:
        target = bin_dir / f"{name}{gobuild._suffix()}"
        target.write_text("x", encoding="utf-8")
        os.utime(target, (newer, newer))

    calls: list[str] = []
    original = gobuild._build

    def spy(name: str, package: str) -> bool:
        calls.append(name)
        return original(name, package)

    gobuild._build = spy  # type: ignore[assignment]
    try:
        result = gobuild.ensure_ready()
    finally:
        gobuild._build = original  # type: ignore[assignment]
    assert result["built"] == []
    assert calls == []
    assert set(result["skipped"]) == {name for name, _ in gobuild.BINARIES}


def test_stale_binary_is_rebuilt(sandbox, fake_go):
    """二进制比 Go 源码旧 → 重编（改了渲染器忘了 build，症状和没编一样）。"""
    _go_dir, bin_dir = sandbox
    bin_dir.mkdir(parents=True)
    for name, _package in gobuild.BINARIES:
        target = bin_dir / f"{name}{gobuild._suffix()}"
        target.write_text("x", encoding="utf-8")
        os.utime(target, (1, 1))  # 1970 年：必然比源码旧
    result = gobuild.ensure_ready()
    assert result["ok"] is True
    assert set(result["built"]) == {name for name, _ in gobuild.BINARIES}


def test_without_toolchain_it_only_logs(sandbox, monkeypatch):
    """没有 Go 工具链：不抛异常、不写文件，只返回「没编」。"""
    _go_dir, bin_dir = sandbox
    monkeypatch.setattr(gobuild, "_go_binary", lambda: None)
    result = gobuild.ensure_ready()
    assert result["ok"] is False
    assert result["reason"] == "没有 Go 工具链"
    assert not bin_dir.exists()


def test_missing_go_dir_is_fine(tmp_path, monkeypatch):
    """镜像里没有 go/ 源码（二进制在 PATH 上）：什么都不做，也算正常。"""
    monkeypatch.setattr(gobuild, "GO_DIR", tmp_path / "nope")
    monkeypatch.setattr(gobuild, "BIN_DIR", tmp_path / "nope" / "bin")
    result = gobuild.ensure_ready()
    assert result["ok"] is True
    assert result["reason"] == "没有 go/ 源码目录"


def test_can_be_disabled_by_env(sandbox, fake_go, monkeypatch):
    """NTE_GO_BUILD=0：完全不碰编译（自己管二进制的人说了算）。"""
    monkeypatch.setenv("NTE_GO_BUILD", "0")
    _go_dir, bin_dir = sandbox
    result = gobuild.ensure_ready()
    assert result["built"] == []
    assert "NTE_GO_BUILD" in result["reason"]
    assert not bin_dir.exists()


def test_build_failure_does_not_raise(sandbox, tmp_path, monkeypatch):
    """编译报错：只记日志，返回 skipped，服务照常起来。"""
    bad = tmp_path / "bad-go"
    bad.write_text("#!/bin/sh\necho 'boom: no network' >&2\nexit 1\n", encoding="utf-8")
    bad.chmod(0o755)
    monkeypatch.setattr(gobuild, "_go_binary", lambda: str(bad))
    result = gobuild.ensure_ready()
    assert result["ok"] is True
    assert result["built"] == []
    assert set(result["skipped"]) == {name for name, _ in gobuild.BINARIES}


def test_status_reports_without_side_effects(sandbox):
    """``status()`` 只读：查得到路径与「缺不缺」，但不编译、不建目录。"""
    _go_dir, bin_dir = sandbox
    info = gobuild.status()
    assert [item["name"] for item in info["binaries"]] == [name for name, _ in gobuild.BINARIES]
    assert all(item["missing"] for item in info["binaries"])
    assert not bin_dir.exists()


def test_cli_skips_build_for_maintenance_commands():
    """维护命令不该为了「重置一次密钥」先等一次编译。"""
    from app import cli

    assert cli._skips_go_build(["--help"])
    assert cli._skips_go_build(["--reset-key"])
    assert cli._skips_go_build(["--transfer-admin", "abc"])
    assert cli._skips_go_build(["--check"])
    assert cli._skips_go_build(["help"])
    # 起服务的路径要编
    assert not cli._skips_go_build([])
    assert not cli._skips_go_build(["--port", "8123"])
    assert not cli._skips_go_build(["hotrun"])
    assert not cli._skips_go_build(["--debug"])


@pytest.mark.skipif(sys.platform == "win32", reason="假 go 脚本是 sh")
def test_build_uses_static_flags(sandbox, tmp_path, monkeypatch):
    """编译命令要带 ``CGO_ENABLED=0`` / ``-trimpath`` / ``-ldflags``（静态、可复现）。"""
    recorded = tmp_path / "args.txt"
    script = tmp_path / "rec-go"
    script.write_text(
        "#!/bin/sh\n"
        "echo \"$CGO_ENABLED|$*\" > " + str(recorded) + "\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(gobuild, "_go_binary", lambda: str(script))
    gobuild.ensure_ready()
    line = recorded.read_text(encoding="utf-8")
    assert line.startswith("0|"), "CGO_ENABLED 必须是 0（要完全静态的二进制）"
    assert "-trimpath" in line
    assert "-ldflags" in line and "main.buildTag=" in line
    # 记录文件是覆盖写的，留下的是最后一个包；两种二进制都在允许集合里
    package = Path(line.rsplit(" ", 1)[-1].strip()).name
    assert package in {name for name, _ in gobuild.BINARIES}
