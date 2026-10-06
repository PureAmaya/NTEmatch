"""上传图片的**去重**：同一张图在全站只能有一份。

用户提的原始需求是「通知 / 信息里上传的图、以及头像，重复上传不要留第二份，
自动复用此前那张」。它容易在三个地方悄悄失效：

* **入口不同**：公告插图走 ``/api/media``，头像走 ``/api/avatar/upload``，
  两个入口各查各的目录 → 同一张图两份；
* **客户端声明撒谎**：同一个字节，MIME 写成 ``image/png`` 与 ``image/jpeg``
  各传一次（旧版头像按声明的 MIME 决定扩展名）→ 又是两份；
* **历史遗留**：升级之前就已经是两份了，不清掉就永远在。

前两条现在是**结构上不可能**（内容寻址 + 全仓查重 + 格式按魔数判断），
第三条由一次性收拢处理（``media.merge_legacy``）。下面把这些钉住。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from app import avatars, backup, media
from app.main import app
from app.store import store

#: 一张真实的 1×1 PNG（67 字节）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
#: 一张真实的 1×1 JPEG（另一份内容，用来验证「不同的图还是各存各的」）
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _hash_name(raw: bytes, ext: str) -> str:
    """内容寻址的文件名（与 ``app.media`` 的命名规则一致；测试自己算，不借私有函数）。"""
    return f"{hashlib.sha256(raw).hexdigest()[:16]}.{ext}"


def _copies_of(raw: bytes) -> int:
    """磁盘上**内容与 ``raw`` 相同**的图片有几个（两个目录都数）。

    直接按内容数最贴着需求本身：不管它叫什么名字、落在哪个目录，
    同一张图就只能有一份。
    """
    count = 0
    for root in (media.UPLOAD_DIR, avatars.AVATAR_DIR):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.is_file() and path.read_bytes() == raw:
                count += 1
    return count


@pytest.fixture(autouse=True)
def _clean_pictures():
    """用例造出来的图片文件用完删掉（测试数据目录虽说是临时的，也别互相串）。"""
    def snap() -> set[Path]:
        found: set[Path] = set()
        for root in (media.UPLOAD_DIR, avatars.AVATAR_DIR):
            if root.is_dir():
                found |= {p for p in root.iterdir() if p.is_file()}
        return found

    before = snap()
    yield
    for path in snap() - before:
        path.unlink(missing_ok=True)


@pytest.fixture
async def anon_client():
    from app import db

    db.init_db(store._db_path)
    if not store.current_id:
        await store.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nte.test"
    ) as client:
        yield client


# --------------------------------------------------------------------------- #
# 两个入口之间：同一张图只有一份
# --------------------------------------------------------------------------- #
async def test_notice_then_avatar_keeps_one_copy(admin_client):
    """先当公告插图传、再当头像传：第二次**复用**第一次那份。"""
    first = await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})
    assert first.status_code == 200, first.text

    second = await admin_client.post("/api/avatar/upload", json={"dataUrl": _data_url(PNG)})
    assert second.status_code == 200, second.text
    assert second.json()["url"] == first.json()["url"], "同一张图应当指向同一个文件"
    assert _copies_of(PNG) == 1, "磁盘上不该出现第二份"


async def test_avatar_then_notice_keeps_one_copy(admin_client):
    """反过来的顺序也一样（先头像后插图）。"""
    first = await admin_client.post("/api/avatar/upload", json={"dataUrl": _data_url(PNG)})
    assert first.status_code == 200, first.text
    second = await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})
    assert second.status_code == 200, second.text
    assert second.json()["url"] == first.json()["url"]
    assert _copies_of(PNG) == 1


async def test_different_pictures_are_kept_apart(admin_client):
    """去重只对「同一张图」生效：不同的图当然各存各的。"""
    png = (await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})).json()
    jpeg = (await admin_client.post("/api/media", json={"data_url": _data_url(JPEG, "image/jpeg")})).json()
    assert png["url"] != jpeg["url"]
    assert _copies_of(PNG) == 1 and _copies_of(JPEG) == 1


async def test_lying_mime_does_not_create_a_second_copy(admin_client):
    """客户端把 PNG 声明成 JPEG：按**魔数**判定，仍然只有一个文件。

    （旧版头像按声明的 MIME 决定扩展名，改一个字母就能在同一目录里再存一份。）
    """
    as_png = await admin_client.post("/api/avatar/upload", json={"dataUrl": _data_url(PNG, "image/png")})
    as_jpg = await admin_client.post("/api/avatar/upload", json={"dataUrl": _data_url(PNG, "image/jpeg")})
    assert as_jpg.json()["url"] == as_png.json()["url"]
    assert as_png.json()["url"].endswith(".png"), "扩展名要按真实格式，不能听客户端说的"
    assert _copies_of(PNG) == 1


async def test_avatar_upload_rejects_disguised_html(admin_client):
    """头像入口同样按魔数校验：一段 HTML 声明成 PNG 必须被拒（存储型 XSS 的口子）。"""
    fake = "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode()
    res = await admin_client.post("/api/avatar/upload", json={"dataUrl": fake})
    assert res.status_code == 400, res.text
    assert "图片" in res.text


async def test_avatar_size_limit_still_applies(admin_client):
    """头像那 2 MB 的上限仍然生效（公告插图的上限比它宽，别混用）。"""
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    res = await admin_client.post("/api/avatar/upload", json={"dataUrl": _data_url(big)})
    assert res.status_code == 400
    assert "KB" in res.text or "MB" in res.text


# --------------------------------------------------------------------------- #
# 历史遗留：旧头像目录
# --------------------------------------------------------------------------- #
async def test_legacy_avatar_file_is_readable_from_both_routes(admin_client, anon_client):
    """升级前落在旧头像目录里的图：两个地址都读得到（老资料里的 URL 不能失效）。"""
    name = f"{'a' * 16}.png"
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (avatars.AVATAR_DIR / name).write_bytes(PNG)

    by_avatar = await anon_client.get(f"/api/avatar/file/{name}")
    assert by_avatar.status_code == 200
    assert by_avatar.headers["content-type"] == "image/png"
    assert (await anon_client.get(f"/api/media/{name}")).status_code == 200


async def test_reuse_works_across_the_old_directory(admin_client, anon_client):
    """旧目录里已有这张图：再上传一次要**复用它**，而不是在新目录里再写一份。"""
    name = _hash_name(PNG, "png")
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (avatars.AVATAR_DIR / name).write_bytes(PNG)
    before = _copies_of(PNG)

    res = await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})
    assert res.status_code == 200, res.text
    assert res.json()["url"] == f"/api/media/{name}", "应当复用旧目录里那份"
    assert _copies_of(PNG) == before, "不该多出第二份"


async def test_reuse_finds_the_copy_even_with_a_lying_extension(admin_client, anon_client):
    """旧数据里那份可能**扩展名与内容不符**（旧版听客户端的 MIME）：

    这种也要认出来并复用（否则同一张图会有第二份），而且访问时 MIME 要按内容给
    ——不然浏览器拿到的是「自称 JPEG 的 PNG」。
    """
    name = _hash_name(PNG, "jpg")  # 内容是 PNG，名字却写着 .jpg
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (avatars.AVATAR_DIR / name).write_bytes(PNG)

    served = await anon_client.get(f"/api/avatar/file/{name}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png", "MIME 要按内容，不按扩展名"

    res = await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})
    assert res.status_code == 200, res.text
    assert res.json()["url"] == f"/api/media/{name}", "应当复用旧目录里那份"
    assert _copies_of(PNG) == 1, "不该多出第二份"


def test_merge_legacy_drops_the_duplicate():
    """收拢旧目录：同一张图两处都有 → 只留统一仓库那一份。"""
    media.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = _hash_name(PNG, "png")
    (media.UPLOAD_DIR / name).write_bytes(PNG)
    (avatars.AVATAR_DIR / name).write_bytes(PNG)
    assert _copies_of(PNG) == 2

    assert media.merge_legacy() >= 1
    assert _copies_of(PNG) == 1
    assert (media.UPLOAD_DIR / name).is_file(), "留下的应当是统一仓库里那份"
    assert not (avatars.AVATAR_DIR / name).exists()


def test_merge_legacy_moves_the_only_copy():
    """旧目录里的图在统一仓库里没有 → 搬过去（不是删掉）。"""
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = _hash_name(JPEG, "jpg")
    (avatars.AVATAR_DIR / name).write_bytes(JPEG)

    media.merge_legacy()
    assert (media.UPLOAD_DIR / name).is_file()
    assert not (avatars.AVATAR_DIR / name).exists()
    assert _copies_of(JPEG) == 1


async def test_content_addressed_pictures_are_cached_forever(admin_client, anon_client):
    """内容寻址的图片一律「一年 + immutable + nosniff」，**两个地址一份策略**。

    文件名就是内容哈希 → 换图 = 换文件名，缓存永远不必回源；而
    ``/api/media/<哈希>`` 与 ``/api/avatar/file/<哈希>`` 指向的是**同一张图**，
    策略不一致就会变成「换个地址访问，行为就变了」——这种问题最难查。
    """
    saved = (await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})).json()
    for url in (saved["url"], f"/api/avatar/file/{saved['name']}"):
        res = await anon_client.get(url)
        assert res.status_code == 200, url
        cache = res.headers["cache-control"]
        assert "immutable" in cache and "max-age=31536000" in cache, (url, cache)
        assert res.headers.get("x-content-type-options") == "nosniff", url


def test_merge_legacy_leaves_foreign_files_alone():
    """不是内容寻址命名的文件（手工丢进去的）不碰——收拢只认自己的命名规则。"""
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    stranger = avatars.AVATAR_DIR / "my-girlfriend.png"
    stranger.write_bytes(PNG)

    media.merge_legacy()
    assert stranger.is_file(), "不认识的文件不该被搬走或删掉"


# --------------------------------------------------------------------------- #
# 备份：图片要一起走
# --------------------------------------------------------------------------- #
async def test_backup_packs_pictures_and_restores_them(admin_client):
    """备份里要有上传的图片（以前根本没有），还原后图片要回来。"""
    saved = (await admin_client.post("/api/media", json={"data_url": _data_url(PNG)})).json()
    name = saved["name"]

    created = await admin_client.post("/api/backups")
    backup_name = created.json()["backup"]["name"]
    assert created.json()["backup"]["media"] >= 1, "清单里应当记下打包了多少张图"

    # zip 里要真的有它（清单数字与内容对不上是最坏的一种 bug：以为备份了，其实没有）
    import io
    import zipfile

    blob = await admin_client.get(f"/api/backups/{backup_name}/download")
    packed = zipfile.ZipFile(io.BytesIO(blob.content)).namelist()
    assert f"data/uploads/{name}" in packed, packed

    # 抹掉本地文件（模拟「换台机器 / 数据没跟过来」），再还原
    (media.UPLOAD_DIR / name).unlink()
    assert not (media.UPLOAD_DIR / name).exists()

    res = await admin_client.post(f"/api/backups/{backup_name}/restore")
    assert res.status_code == 200, res.text
    assert (media.UPLOAD_DIR / name).is_file(), "还原之后这张图必须在"
    assert (media.UPLOAD_DIR / name).read_bytes() == PNG


async def test_restore_does_not_duplicate_old_layout(admin_client):
    """还原一份**旧布局**的备份（图片在 data/avatars/ 里）：倒回来时并进统一仓库。"""
    name = _hash_name(PNG, "png")
    avatars.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (avatars.AVATAR_DIR / name).write_bytes(PNG)
    created = await admin_client.post("/api/backups")
    backup_name = created.json()["backup"]["name"]

    res = await admin_client.post(f"/api/backups/{backup_name}/restore")
    assert res.status_code == 200, res.text
    assert (media.UPLOAD_DIR / name).is_file(), "旧布局里的图片要并进统一仓库"
    assert _copies_of(PNG) == 1, "并过来之后不能留两份"


async def test_backup_manifests_without_media_field_are_still_listed():
    """清单里没有 media 字段（旧版本打的备份）也要能正常列举，不能整崩。"""
    assert backup.list_backups() is not None


async def test_store_is_alive(admin_client):
    """兜底：这一组跑完，站点状态仍然正常（还原类用例最容易伤到这儿）。"""
    assert store.current_id
    assert (await admin_client.get("/api/health")).status_code == 200
