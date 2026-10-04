"""版权清单与许可证兼容性。

这不是「文档作业」，而是**一条能被机器执行的许可证约束**：

1. 用到的每个依赖都得在清单里（含那个独立部署的 MediaMTX）——
   AGPL 是著佐权许可，署名不是可选项；
2. 清单里不许出现 GPL-only 之类的组件——那会与 AGPL-3.0 冲突，
   而这正是「开源之前必须先查一遍」的那件事。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from app import credits

ROOT = Path(__file__).resolve().parent.parent

# AGPL-3.0 与它们都兼容：宽松许可 + 弱著佐权（MPL / PSF 属弱或兼容型）。
ALLOWED = {
    "mit",
    "bsd-2-clause",
    "bsd-3-clause",
    "apache-2.0",
    "apache-2.0 或 bsd-2-clause",
    "mpl-2.0",
    "psf-2.0",
}


def _declared_runtime_deps() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    return [dep.split(">=")[0].split("[")[0].strip().lower() for dep in deps]


def test_every_declared_dependency_is_credited():
    """pyproject 里声明的运行依赖必须都在清单里——漏一个就是漏一份署名。"""
    names = credits.component_names()
    missing = [dep for dep in _declared_runtime_deps() if dep not in names]
    assert not missing, f"这些依赖没写进 app/credits.py：{missing}"


def test_mediamtx_is_credited():
    """MediaMTX 不在 pyproject 里，但它才是直播那条链路的关键组件，必须署名。"""
    names = credits.component_names()
    assert "mediamtx" in names
    mediamtx = next(item for item in credits.PROGRAM if item[0] == "MediaMTX")
    assert mediamtx[1] == "MIT"
    assert "mediamtx" in mediamtx[2].lower()


def test_no_copyleft_conflict_with_agpl():
    """清单里的许可必须与 AGPL-3.0 兼容（没有 GPL-only 这类会冲突的组件）。"""
    rows = credits.RUNTIME + credits.FRONTEND + credits.PROGRAM + credits.DEV
    bad = [(name, lic) for name, lic, *_ in rows if lic.strip().lower() not in ALLOWED]
    assert not bad, f"这些组件的许可需要人工确认是否与 AGPL-3.0 冲突：{bad}"


def test_license_file_matches_declared_id():
    """LICENSE 文件真的存在，而且就是声明的那一份（AGPL-3.0）。"""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3" in text
    assert credits.LICENSE["id"].startswith("AGPL-3.0")


def test_payload_is_public_and_complete():
    """下发给页脚的数据：署名、许可、源码入口一个都不能少（AGPL 第 13 条靠它）。"""
    payload = credits.payload()
    assert payload["source"].startswith("https://")
    assert payload["author"]["name"] and payload["author"]["bilibili"]
    assert payload["license"]["url"].startswith("https://")
    titles = [group["title"] for group in payload["groups"]]
    assert titles == ["运行依赖", "前端资源", "外部程序", "开发与 CI"]
    assert all(group["items"] for group in payload["groups"])


def test_payload_does_not_hand_out_the_qq_number():
    """**不把 QQ 号下发给前端**：页面不展示它，接口里也就不该有。

    ``AUTHOR`` 里仍然留着 ``qq``——头像是按它拼出来的（``qqAvatar``）；
    但那是给后端拼 URL 用的内部字段，没必要跟「署名」一起发给每个访客。
    """
    assert credits.AUTHOR["qq"], "头像 URL 还要靠它拼，别顺手删了"
    author = credits.payload()["author"]
    assert "qq" not in author
    # 头像是按 QQ 号从官方接口取的，所以那条 URL 里确实带着号码——那是「图片地址」，
    # 不是「页面上展示的 QQ 号」；这一条只是防止有人把 qq 字段又加回署名数据里。
    assert author["qqAvatar"].startswith("https://")



async def test_credits_endpoint_is_public(client):
    """接口公开只读：署名信息本来就该让任何人看到，不需要登录。"""
    res = await client.get("/api/credits")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["author"]["name"]
    assert "qq" not in body["author"], "署名数据里不该带 QQ 号"
    assert any(
        item["name"] == "MediaMTX"
        for group in body["groups"]
        for item in group["items"]
    )
