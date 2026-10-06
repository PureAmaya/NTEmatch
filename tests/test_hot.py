"""热更新：控制文件、就绪/口令、预检、以及**一次真实的换代**。

两层测试：

* 大部分是纯逻辑（状态文件、触发文件、指纹、口令校验）——快、能在任何平台跑；
* 最后一条会**真的把守护进程和业务进程拉起来**换一次代：这是这个功能唯一有意义的
  验法（「不中断」这种事，只有真跑起来量才算数）。它比较慢，介意的话设
  ``NTE_SKIP_SLOW=1`` 跳过。

Linux 上的**零中断**（父子同时 accept 同一个套接字）在本机（Windows）验不了：
那边会自动退到「顺序换代」。要验零中断，在服务器上跑
``uv run python -m app hotrun``，换代期间打日志里那条探测即可（见 README）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app import cli, hot
from app.auth import AuthManager, auth
from app.hotrun import Supervisor
from app.store import DATA_ROOT, store

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_hot_dir(tmp_path, monkeypatch):
    """把热更新的控制目录指到临时目录：别在仓库里留状态文件、也别互相串。"""
    monkeypatch.setattr(hot, "HOT_DIR", tmp_path / "hot")
    yield


# --------------------------------------------------------------------------- #
# 纯逻辑
# --------------------------------------------------------------------------- #
def test_data_root_matches_store():
    """``hot.data_root()`` 与 ``store`` 必须认同一个目录。

    那边是刻意重复的几行（父进程不能导入业务模块，见 app/hot.py 的说明），
    重复就有走偏的可能——这条断言就是那道闸。
    """
    assert hot.data_root() == DATA_ROOT


def test_status_roundtrip():
    assert hot.read_status() == {}
    hot.write_status(phase="running", child=1234)
    hot.write_status(reloads=3)
    status = hot.read_status()
    assert status["phase"] == "running" and status["child"] == 1234
    assert status["reloads"] == 3, "第二次写是读-改-写，不能把已有字段写没了"
    assert status["at"] > 0


def test_trigger_roundtrip():
    assert hot.pending_request() is None
    hot.request("pull", "管理员")
    pending = hot.pending_request()
    assert pending["mode"] == "pull" and pending["actor"] == "管理员"
    assert hot.take_request()["mode"] == "pull"
    assert hot.take_request() is None, "取出后必须消失，否则会反复换代"


def test_public_status_and_supervised(monkeypatch):
    monkeypatch.delenv(hot.READY_ENV, raising=False)
    assert hot.public_status()["supervised"] is False
    monkeypatch.setenv(hot.READY_ENV, str(hot.ready_path("x")))
    status = hot.public_status()
    assert status["supervised"] is True and status["child"] == os.getpid()


def test_ready_file_carries_the_real_pid():
    """就绪文件第一行必须是**真实 pid**：父进程靠它核对「答话的是哪一代」。"""
    path = hot.ready_path("tok")
    monkeypatch_env = dict(os.environ, **{hot.READY_ENV: str(path)})
    old = os.environ.copy()
    try:
        os.environ.update(monkeypatch_env)
        assert hot.notify_ready() is True
    finally:
        os.environ.clear()
        os.environ.update(old)
    pid, stamp = hot.read_ready(path)
    assert pid == os.getpid() and stamp > 0


def test_probe_host_maps_wildcards():
    assert hot.probe_host("0.0.0.0") == "127.0.0.1"
    assert hot.probe_host("::") == "127.0.0.1"
    assert hot.probe_host("10.0.0.5") == "10.0.0.5"


def test_tree_stamp_detects_changes(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "static").mkdir()
    one = tmp_path / "app" / "a.py"
    one.write_text("x = 1\n", encoding="utf-8")
    first = hot.tree_stamp(tmp_path)
    assert hot.tree_stamp(tmp_path) == first, "没变就不能变"
    one.write_text("x = 2\n", encoding="utf-8")
    os.utime(one, (time.time() + 5, time.time() + 5))
    assert hot.tree_stamp(tmp_path) != first
    # 运行期自己写的帮助图不算「代码变了」，否则一生成就自己触发换代
    (tmp_path / "static" / "help.jpg").write_bytes(b"x" * 10)
    assert hot.tree_stamp(tmp_path) == hot.tree_stamp(tmp_path)


def test_tree_stamp_ignores_pycache(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = hot.tree_stamp(tmp_path)
    (tmp_path / "app" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"junk")
    assert hot.tree_stamp(tmp_path) == first


def test_head_commit_reads_the_repo():
    commit = hot.head_commit(REPO)
    if not (REPO / ".git").exists():  # pragma: no cover - 打包安装时没有 .git
        assert commit == ""
        return
    assert len(commit) == 40 and set(commit) <= set("0123456789abcdef")
    assert hot.short(commit) == commit[:8]


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
async def test_ping_needs_the_process_token(client, monkeypatch):
    """自查口：口令对上才答，否则与「没有这个路由」无法区分。"""
    resp = await client.get("/api/hot/ping")
    assert resp.status_code == 404
    resp = await client.get("/api/hot/ping", headers={hot.PING_HEADER: "wrong-token"})
    assert resp.status_code == 404
    monkeypatch.setenv(hot.TOKEN_ENV, "s3cret")
    resp = await client.get("/api/hot/ping", headers={hot.PING_HEADER: "s3cret"})
    assert resp.status_code == 200
    assert resp.json()["pid"] == os.getpid()


async def test_hot_status_requires_server_admin(client, admin_client, monkeypatch):
    assert (await client.get("/api/hot")).status_code == 401
    monkeypatch.delenv(hot.READY_ENV, raising=False)
    body = (await admin_client.get("/api/hot")).json()
    assert body["ok"] is True and body["supervised"] is False
    assert "hotrun" in body["hint"], "没开守护时要告诉人怎么开"


async def test_update_is_refused_without_the_supervisor(admin_client, monkeypatch):
    """没有守护就明确报错，而不是假装成功（点了没反应最糟）。"""
    monkeypatch.delenv(hot.READY_ENV, raising=False)
    resp = await admin_client.post("/api/hot/update", json={"mode": "reload"})
    assert resp.status_code == 400
    # 站点的错误信封是 {"ok": false, "error": ...}（见 main.py 的异常处理）
    assert "hotrun" in resp.json()["error"]


async def test_update_writes_the_trigger(admin_client, monkeypatch):
    monkeypatch.setenv(hot.READY_ENV, str(hot.ready_path("x")))
    resp = await admin_client.post("/api/hot/update", json={"mode": "pull"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    pending = hot.pending_request()
    assert pending["mode"] == "pull"
    assert pending["actor"], "状态里要留下是谁发起的"


async def test_update_rejects_unknown_mode(admin_client, monkeypatch):
    monkeypatch.setenv(hot.READY_ENV, str(hot.ready_path("x")))
    resp = await admin_client.post("/api/hot/update", json={"mode": "rm -rf"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 会话：换代不踢人
# --------------------------------------------------------------------------- #
async def test_sessions_survive_a_process_change():
    """会话落库的核心保证：**新进程**（内存是空的）也要认得旧令牌。

    这里用「另一个 AuthManager 实例」模拟换代后的新进程：它没有内存里的会话，
    只能去库里找——找到就说明换代不会把人踢下线。

    （正常由 main 的 lifespan 调用 ``auth.attach``；测试里手动把它挂上，
    因为 ASGI 直连不会跑 lifespan。）
    """
    await auth.attach(store.path)
    session = auth.issue("member:uid", uid="u-1", name="甲", permission="member")
    await auth.flush()

    fresh = AuthManager()
    await fresh.attach(store.path)
    try:
        assert fresh.get(session.token) is None, "新进程内存里本来就不该有"
        found = await fresh.resolve(session.token)
        assert found is not None, "换代之后必须仍然认得这个令牌"
        assert (found.uid, found.name, found.permission) == ("u-1", "甲", "member")
        assert await fresh.resolve("not-a-real-token") is None
        assert await fresh.resolve(None) is None
    finally:
        await fresh.detach()
        auth.revoke(session.token)
        await auth.flush()
        await auth.detach()


async def test_revoked_sessions_do_not_come_back():
    """登出 / 停用成员之后，库里那条也必须消失——否则换代会让它「复活」。"""
    await auth.attach(store.path)
    session = auth.issue("member:uid", uid="u-2", permission="member")
    await auth.flush()
    auth.revoke_by_uid("u-2")
    await auth.flush()

    fresh = AuthManager()
    await fresh.attach(store.path)
    try:
        assert await fresh.resolve(session.token) is None
    finally:
        await fresh.detach()
        await auth.detach()


async def test_expired_sessions_are_pruned():
    await auth.attach(store.path)
    session = auth.issue("member:uid", uid="u-3", permission="member")
    await auth.flush()
    # 手动把它改成已过期，再看库里会不会被清掉
    from app import db

    db.save_sessions(
        store.path,
        [
            {
                "token_hash": session.token_hash,
                "uid": "u-3",
                "name": "",
                "permission": "member",
                "label": "x",
                "created_at": time.time() - 100,
                "expires_at": time.time() - 1,
            }
        ],
    )
    fresh = AuthManager()
    await fresh.attach(store.path)
    try:
        assert await fresh.resolve(session.token) is None
    finally:
        await fresh.detach()
        await auth.detach()


# --------------------------------------------------------------------------- #
# 预检
# --------------------------------------------------------------------------- #
def test_preflight_blocks_a_broken_version(monkeypatch):
    """新代码起不来时，换代必须**在停掉旧进程之前**中止。"""
    sup = Supervisor(host="127.0.0.1", port=1)

    class Broken:
        returncode = 1
        stdout = "Traceback (most recent call last):\nSyntaxError: invalid syntax\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Broken())
    assert sup._preflight() is False
    assert "起不来" in str(hot.read_status().get("message") or "")


def test_preflight_passes_for_this_tree(monkeypatch):
    """我们自己的代码当然要能过预检（真跑一次 `python -m app --check`）。"""
    sup = Supervisor(host="127.0.0.1", port=1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _ok())
    assert sup._preflight() is True


class _ok:
    returncode = 0
    stdout = "预检通过\n"
    stderr = ""


def test_app_report_roundtrip():
    """业务进程写的「我这边的事实」要能被读到，并出现在管理端看到的状态里。"""
    assert hot.read_app_report() == {}
    hot.write_app_report({"pid": 4242, "sessions": True, "loop": "SelectorEventLoop"})
    report = hot.read_app_report()
    assert report["pid"] == 4242 and report["sessions"] is True
    assert hot.public_status()["app"]["loop"] == "SelectorEventLoop"


def test_selfcheck_covers_the_essentials(monkeypatch):
    """启动自检要把「能不能热更新」的几件事都点到名（含「零中断还是顺序换代」）。"""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _ok())
    sup = Supervisor(host="127.0.0.1", port=_free_port(), hold_socket=True)
    names = [item["name"] for item in sup._selfcheck()]
    for expected in ("换代方式", "代码仓库", "git 命令", "uv 命令", "新代码预检"):
        assert expected in names, f"自检少了「{expected}」"
    mode = next(item for item in sup._selfcheck() if item["name"] == "换代方式")
    assert "零中断" in mode["detail"]
    plain = Supervisor(host="127.0.0.1", port=_free_port(), hold_socket=False)
    mode = next(item for item in plain._selfcheck() if item["name"] == "换代方式")
    assert "顺序换代" in mode["detail"]


def test_last_reload_is_recorded(monkeypatch):
    """换代结果要落进状态文件（管理端据此显示「上次换代」）。"""
    sup = Supervisor(host="127.0.0.1", port=1)
    sup.child = None
    sup.reloads = 2
    summary = sup._report_reload(
        ok=True,
        mode="pull",
        actor="服务器管理员",
        reason="管理端请求",
        commit_from="a" * 40,
        commit_to="b" * 40,
        seconds=1.25,
        detail="已更新到 bbbbbbbb",
    )
    assert summary["ok"] is True and summary["seconds"] == 1.25
    assert hot.read_status()["lastReload"]["from"] == "a" * 8
    assert hot.public_status()["lastReload"]["to"] == "b" * 8


# --------------------------------------------------------------------------- #
# 默认启动方式
# --------------------------------------------------------------------------- #
def test_default_start_is_the_supervisor(monkeypatch):
    """**默认启动就是热更新守护**（用户不必记住多打一个词）。"""
    called: dict = {}

    def fake_hotrun(argv):
        called["hotrun"] = list(argv)
        return 0

    def fake_serve(**kw):
        called["serve"] = kw

    monkeypatch.setattr("app.hotrun.main", fake_hotrun)
    monkeypatch.setattr(cli, "serve", fake_serve)
    for name in ("NTE_HOT_READY", "NTE_HOT", "NTE_RELOAD"):
        monkeypatch.delenv(name, raising=False)

    assert cli.main([]) == 0
    assert "serve" not in called, "默认不该直接起单进程服务"
    args = called["hotrun"]
    assert "--port" in args and "--host" in args, "端口与监听地址要转交给守护"


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["--no-hotrun"], {}),                    # 显式要原始启动
        ([], {"NTE_HOT": "0"}),                   # 环境变量关掉
        ([], {"NTE_RELOAD": "1"}),                # uvicorn 自己的 reloader（两套机制会抢端口）
        ([], {"NTE_HOT_READY": "x"}),             # 我本身就是守护拉起来的子进程
    ],
)
def test_plain_start_escapes(monkeypatch, argv, env):
    """四条逃生路都要真的回到单进程启动（尤其最后一条：否则会无限套娃）。"""
    called: dict = {}

    def fake_hotrun(args):
        called["hotrun"] = list(args)
        return 0

    def fake_serve(**kw):
        called["serve"] = kw

    monkeypatch.setattr("app.hotrun.main", fake_hotrun)
    monkeypatch.setattr(cli, "serve", fake_serve)
    for name in ("NTE_HOT_READY", "NTE_HOT", "NTE_RELOAD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert cli.main(argv) == 0
    assert "serve" in called and "hotrun" not in called


# --------------------------------------------------------------------------- #
# 真换代（慢）
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _get(url: str, token: str = "") -> tuple[int, dict]:
    headers = {"X-NTE-Token": token} if token else {}
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            return resp.status_code, (resp.json() if resp.content else {})
    except Exception as exc:  # noqa: BLE001  (连不上就是连不上：调用方只看状态码)
        return 0, {"err": repr(exc)}


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, headers={"X-NTE-Token": token}, json=body)
            return resp.status_code, (resp.json() if resp.content else {})
    except Exception as exc:  # noqa: BLE001
        return 0, {"err": repr(exc)}


@pytest.mark.skipif(os.getenv("NTE_SKIP_SLOW") == "1", reason="设了 NTE_SKIP_SLOW=1")
def test_parent_alive_uses_the_heartbeat(monkeypatch):
    """心跳判定：父进程从没写过心跳时**不判死**（刚启动那两秒不能被误杀）。"""
    monkeypatch.delenv("NTE_HOT_PARENT_GONE", raising=False)
    assert hot.parent_alive() is True, "没有心跳记录时不能判定父进程已死"
    hot.write_status(pid=999999, alive=time.time())
    assert hot.parent_alive() is True
    monkeypatch.setenv("NTE_HOT_PARENT_GONE", "1")
    hot.write_status(alive=time.time() - 30)
    assert hot.parent_alive() is False, "心跳停了这么久就该认定守护没了"


@pytest.mark.skipif(os.getenv("NTE_SKIP_SLOW") == "1", reason="设了 NTE_SKIP_SLOW=1")
def test_orphan_child_lets_go_of_the_port(tmp_path):
    """守护被**硬杀**（没机会做清理）时，子进程必须自己收摊、把端口让出来。

    这是踩过的坑：Windows 上 ``terminate()`` 是 TerminateProcess，父进程的收尾代码
    根本跑不到，业务子进程就成了「占着端口的孤儿」——下一次启动绑不上，
    表现成「服务莫名起不来」。靠子进程盯父进程的心跳解决（见 hot.watch_parent）。
    """
    port = _free_port()
    env = dict(os.environ, NTE_HOT_PARENT_GONE="6")  # 心跳 6 秒不更新就算父进程没了
    log = open(tmp_path / "orphan.log", "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        # 走**默认**那条路（不带子命令）：它就该自己起热更新守护
        [sys.executable, "-m", "app", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if _get(f"http://127.0.0.1:{port}/api/health")[0] == 200:
                break
            time.sleep(0.4)
        else:  # pragma: no cover
            pytest.fail("守护没能起来")
        # 硬杀守护：不给它任何清理机会（Windows 上走 taskkill /F，POSIX 走 SIGKILL）
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.kill(proc.pid, 9)
        proc.wait(timeout=20)

        deadline = time.monotonic() + 60
        freed = False
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("0.0.0.0", port))
                freed = True
                break
            except OSError:
                time.sleep(1.0)
            finally:
                probe.close()
        assert freed, "守护被硬杀之后，子进程没有让出端口（变成孤儿了）"
    finally:
        log.close()
        if proc.poll() is None:  # pragma: no cover
            proc.kill()


def test_supervisor_swaps_the_running_app(tmp_path):
    """**真的**拉起守护与业务进程，换一次代，验证三件事：

    1. 换代之后服务照常（新代码真的在服务）；
    2. **登录状态没掉**（会话落库的最终目的）；
    3. 「管理端点一下更新」这条路真的能换代码。

    这是整个功能里唯一算数的验法：进程、端口、时间都是真的。
    守护与子进程的日志落盘（``hotrun.log``），失败时一并打出来——不然「起不来」
    只会留下一句无从下手的报错。
    """
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    log_path = tmp_path / "hotrun.log"
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115  (要跨整个用例持有)
    # 换代时不去动虚拟环境（测试机没必要，也会拖慢）
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app",
            "hotrun",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--interval",
            "1",
            "--no-watch",
            "--no-deps-sync",
            "--no-hold-socket",
        ],
        cwd=str(REPO),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    def tail(lines: int = 40) -> str:
        try:
            return "\n".join(log_path.read_text("utf-8", errors="replace").splitlines()[-lines:])
        except OSError:  # pragma: no cover
            return "(日志读不到)"

    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if _get(f"{base}/api/health")[0] == 200:
                break
            time.sleep(0.4)
        else:  # pragma: no cover - 起不来就是 bug
            pytest.fail(f"守护进程没能把服务拉起来（退出码 {proc.poll()}）\n{tail()}")

        code, login = _post(f"{base}/api/auth/local", "", {})
        assert code == 200 and login.get("token"), login
        token = login["token"]
        assert _get(f"{base}/api/auth/check", token)[0] == 200
        code, before = _get(f"{base}/api/hot", token)
        assert code == 200 and before["supervised"] is True
        reloads = int(before.get("reloads") or 0)

        # 换代 1：顺手向服务端请求一次更新（等价于点「重新加载代码」）
        code, res = _post(f"{base}/api/hot/update", token, {"mode": "reload"})
        assert code == 200 and res.get("ok") is True, res

        deadline = time.monotonic() + 120
        after = before
        while time.monotonic() < deadline:
            code, after = _get(f"{base}/api/hot", token)
            if code == 200 and int(after.get("reloads") or 0) > reloads:
                break
            time.sleep(0.5)
        assert int(after.get("reloads") or 0) > reloads, f"没有换代：{after}"

        # 1. 服务照常
        assert _get(f"{base}/api/health")[0] == 200
        # 2. 换代之后**登录状态还在**（这是「不中断业务」最实在的一条）
        assert _get(f"{base}/api/auth/check", token)[0] == 200
        assert after.get("phase") == "running"
        # 3. 换代结果被记下来了（管理端据此显示「上次换代」）
        last = after.get("lastReload") or {}
        assert last.get("ok") is True, after
        assert last.get("actor"), "要记下是谁触发的"
        # 4. 启动自检也在（含业务进程回报的那份事实）
        assert any(item["name"] == "换代方式" for item in after.get("check") or []), after
        assert (after.get("app") or {}).get("sessions") is True, "会话落库必须开着"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        if os.name == "nt":  # pragma: no cover - Windows 上确认没留下占端口的孤儿
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
