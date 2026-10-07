"""画图用的字体：找到哪支就用哪支，装在哪儿都认得出来。

比赛卡片（:mod:`app.card`）、机器人帮助图（:mod:`app.helpcard`）与出图脚本
（``tools/make_share_card.py``）都要**中文 CJK 字体**——中文必须走 CJK 字体，
西文字体只会画出方框。而「字体到底装在哪」每个发行版都不一样：

* Debian / Ubuntu：``apt install fonts-noto-cjk``（思源黑体的 Google 版）
  → ``/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc``
* RHEL / AlmaLinux / Rocky / Fedora：``dnf install google-noto-sans-cjk-fonts``
  → ``/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc``
* Arch：``pacman -S noto-fonts-cjk`` → ``/usr/share/fonts/noto-cjk/``
* Alpine：``apk add font-noto-cjk`` → ``/usr/share/fonts/noto/``

以前这里只认几条**写死的路径**，于是「照文档装好了字体，图里的中文还是方框」——
日志里那句「没有可用的中文字体」就是它。现在分四层找，越靠前越优先：

1. **环境变量**：``NTE_FONT_CJK`` / ``NTE_FONT_MONO`` / ``NTE_FONT_LATIN``
   （自建镜像、字体放在奇怪位置时用这个，最直接）；
2. 常见安装路径（见下面几个常量，Windows / macOS / 各系 Linux 都列了）；
3. **扫一遍字体目录**：按文件名前缀认（``NotoSansCJK*`` / ``SourceHanSans*`` /
   ``NotoSansSC*`` / ``wqy-*`` …），所以不管发行版把它塞在哪一层子目录都能找到；
4. 实在没有才退回 Pillow 内置位图字体（中文会是方框，但不崩，日志里会说明该怎么装）。

结果**按种类缓存**：字体文件不会在运行中变来变去，而 Pillow 每次 ``truetype()``
都要解析一遍字体表——卡片一次渲染就要六到十个字号，不缓存等于白烧。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .logging_conf import get_logger

log = get_logger("font")

#: 中文候选（前几条是 Windows 的微软雅黑 / 等线，站点字体栈里的中文就是它）
#:
#: **简体优先**：多语言合集（``NotoSansCJK-*.ttc`` / ``SourceHanSans*.ttc``）里
#: 一台机器上同时装着 JP / KR / SC / TC 四套字形，取错一支就会把简体字画成日文字形
#: （「直」「骨」「次」那类一眼就能看出是日文的写法）。所以顺序是：
#: ① 简体单语言文件 → ② 简体命名的合集 → ③ 多语言合集（由 :func:`_sc_face_index` 挑 face）。
CJK_PATHS: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-sans-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
)

#: 等宽 / 科技感西文（卡片顶部那行编号与状态）
MONO_PATHS: tuple[str, ...] = (
    r"C:\Windows\Fonts\bahnschrift.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)

#: 西文粗体（帮助图上的「COMMANDS」这类小标签）
LATIN_PATHS: tuple[str, ...] = (
    r"C:\Windows\Fonts\bahnschrift.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
)

#: 扫目录时按**文件名前缀**认字体（一律小写比较）
_CJK_PREFIXES = (
    "notosanscjk",
    "notoserifcjk",
    "notosanssc",
    "notoserifsc",
    "sourcehansans",
    "sourcehanserif",
    "wqy-zenhei",
    "wqy-microhei",
    "msyh",
    "simhei",
    "pingfang",
)
_MONO_PREFIXES = ("dejavusansmono", "notosansmono", "jetbrainsmono", "firacode", "consola", "menlo")
_LATIN_PREFIXES = ("bahnschrift", "seguisb", "dejavusanscondensed", "opensans")

#: 认的字体后缀
_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")

#: 扫字体时看的目录（``~`` 会展开；不存在的直接跳过）
_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
    "/Library/Fonts",
)

def _spec(kind: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """这一种类的（候选路径, 扫描前缀, 环境变量名）。

    **现取**模块级常量，而不是在字典里存一份值：值被冻住的话，测试（或运行中）
    改 ``fonts.CJK_PATHS`` 就毫无效果——「改了没反应」这种坑不值得留。
    """
    if kind == "mono":
        return MONO_PATHS, _MONO_PREFIXES, "NTE_FONT_MONO"
    if kind == "latin":
        return LATIN_PATHS, _LATIN_PREFIXES, "NTE_FONT_LATIN"
    return CJK_PATHS, _CJK_PREFIXES, "NTE_FONT_CJK"

#: 缓存：种类 → 字体文件（``None`` 表示找过了、确实没有）
_resolved: dict[str, Path | None] = {}
#: 缓存：（种类, 字号）→ 字体对象
_fonts: dict[tuple[str, int], object] = {}


def reset() -> None:
    """清掉缓存（测试里换了环境变量 / 字体目录之后要调；运行中不需要）。"""
    _resolved.clear()
    _fonts.clear()
    _FACE_INDEX.clear()


def _from_env(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_file():
        return path
    log.warning("%s 指向的字体不存在，继续按默认顺序找 | %s", name, raw)
    return None


def _first_existing(paths: tuple[str, ...]) -> Path | None:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            return path
    return None


def _scan(prefixes: tuple[str, ...]) -> Path | None:
    """扫常见字体目录，按文件名前缀认（发行版放在哪一层都能找到）。

    简体优先：同一台机器上既有 ``NotoSansCJKsc-*.otf`` 也有 ``NotoSansCJK-*.ttc`` 时，
    取前者（单语言文件，不用猜 face；见 :func:`_sc_face_index`）。
    """
    found: list[Path] = []
    for raw_root in _FONT_DIRS:
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            continue
        try:
            candidates = sorted(root.rglob("*"))
        except OSError:  # pragma: no cover - 目录权限问题：跳过这一个
            continue
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
                continue
            name = path.name.lower()
            if name.startswith(prefixes):
                found.append(path)
    if not found:
        return None
    # 简体优先，其次按路径（同一台机器上重复安装同一支字体时结果要稳定）
    return min(found, key=lambda p: (not _looks_simplified(p.name), str(p)))


#: 认「这支字体是简体中文」的记号（文件名与 face 名都按这个认）
_SC_TOKENS = ("sc", "simplified", "chs", "gb", "简体")


def _looks_simplified(name: str) -> bool:
    """这个字体名（文件名或 face 名）看着是**简体中文**那一支吗。

    ``sc`` 单独成段才算：``NotoSansCJKsc`` / ``Noto Sans CJK SC`` / ``Yozai SC`` 都认，
    而 ``Scaramouche`` 这种顺带出现的字母组合不该被当成简体。
    """
    text = str(name or "").lower()
    if any(tok in text for tok in _SC_TOKENS if tok != "sc"):
        return True
    return any(part == "sc" or part.endswith("sc") for part in re.split(r"[^a-z0-9]+", text))


def resolve(kind: str = "cjk") -> Path | None:
    """找这一类字体用的文件；找不到回 ``None``（结果会缓存，重复调用不重复扫盘）。"""
    if kind in _resolved:
        return _resolved[kind]
    paths, prefixes, env_name = _spec(kind)
    found = _from_env(env_name) or _first_existing(paths) or _scan(prefixes)
    if found is None and kind in ("mono", "latin"):
        # 只有中文字体时，拿它显示数字 / 西文也比方框强
        found = resolve("cjk")
    if found is not None:
        # **只缓存找到的**：「没找到」也缓存的话，运行中（`apt install` 完不重启）
        # 装上的字体永远不会被认出来——那种「明明装了却还是方框」最难查。
        _resolved[kind] = found
    if found is None:
        log.warning(
            "没找到 %s 字体（中文会显示成方框）：装一个思源黑体 / Noto Sans CJK"
            "（Debian 系 `apt install fonts-noto-cjk`），或设 %s 直接指定字体文件",
            kind,
            env_name,
        )
    else:
        log.info("%s 字体 | %s", kind, found)
    return found


#: 集合字体（.ttc / .otc）里挑出来的 face 序号（按文件路径缓存）
_FACE_INDEX: dict[str, int] = {}


def _sc_face_index(path: Path) -> int:
    """集合字体里**简体中文**那一支的 face 序号（挑不到就 0）。

    ``NotoSansCJK-*.ttc`` / ``SourceHanSans*.ttc`` 这类**多语言合集**里同时装着
    JP / KR / SC / TC 四套字形，而 ``ImageFont.truetype`` 默认取第 0 个——取到 JP 那支，
    简体中文就会被画成**日文字形**（「直」「骨」「次」的写法一眼能看出来）。
    所以这里按 face 名挑：名字里带 SC / Simplified 的那一支才是要的。

    单语言文件（``NotoSansSC-Regular.otf`` / ``msyh.ttc`` 之类）只有一个 face，
    这里不会去动它。
    """
    key = str(path)
    hit = _FACE_INDEX.get(key)
    if hit is not None:
        return hit
    index = 0
    if path.suffix.lower() in (".ttc", ".otc"):
        from PIL import ImageFont

        for candidate in range(12):
            try:
                probe = ImageFont.truetype(key, 20, index=candidate)
            except OSError:
                break
            family, style = probe.getname()
            if _looks_simplified(family) or _looks_simplified(style):
                index = candidate
                log.info(
                    "集合字体里挑了简体那一支 | %s | face=%d | %s", path.name, candidate, family
                )
                break
    _FACE_INDEX[key] = index
    return index


def identity(kind: str = "cjk") -> str:
    """这一类字体用来「算内容指纹」的标识：**文件 + face**（找不到回 ``-``）。

    为什么不只写路径：多语言合集（``NotoSansCJK-*.ttc``）里 JP / KR / SC / TC 是**同一个
    文件的不同 face**——只按路径算指纹的话，把 face 从日文改成简体也认不出来，
    于是同一份内容会**继续用那张旧图**（日文字形的卡片就是这么被缓存下来的）。
    """
    path = resolve(kind)
    if path is None:
        return "-"
    face = _sc_face_index(path)
    if not face:
        return str(path)
    try:
        family, _style = load(kind, 20).getname()
    except Exception:  # noqa: BLE001  (个别字体读不出名字：face 序号本身也够用)
        family = ""
    return f"{path}#{face}{'·' + family if family else ''}"


def load(kind: str, size: int):
    """拿一个指定字号的字体对象（没有可用字体时退回 Pillow 内置位图字体）。

    **``mono`` / ``latin`` 只能用来画纯西文**——它们没有中文字形，画中文只会得到
    空心方块（真踩过：卡片副标题「e902 · 筹备中 · 第 1 届」被等宽西文字体画成了
    一排方块）。拿不准就用 ``cjk``：它也含西文与数字，只是不那么「科技感」。
    """
    key = (kind, int(size))
    hit = _fonts.get(key)
    if hit is not None:
        return hit
    from PIL import ImageFont

    path = resolve(kind)
    font = None
    if path is not None:
        try:
            # face 序号只在集合字体上有效；单语言文件恒为 0（见 _sc_face_index）
            font = ImageFont.truetype(str(path), int(size), index=_sc_face_index(path))
        except OSError as exc:  # 字体损坏 / 不是 TrueType：兜回内置
            log.warning("字体读不动，改用内置字体 | %s | %s", path, exc)
    if font is None:
        font = ImageFont.load_default(int(size))
    _fonts[key] = font
    return font


def has_glyph(font, char: str) -> bool:
    """这支字体画得出 ``char`` 吗（画不出就会变成空心方块）。

    做法：把它与一个「必定不存在」的私用区字符各画一遍，比对位图——缺字形时
    FreeType 画的都是同一个 ``.notdef`` 方块，位图逐字节相同。

    用途是自检：「我选的这支字体能不能画这句话」。卡片副标题那次的方块就是这么
    查出来的，``tests/test_card.py`` 用它在真实内容上跑一遍。
    """
    from PIL import Image, ImageDraw

    def mask(text: str) -> bytes:
        img = Image.new("L", (64, 64), 0)
        ImageDraw.Draw(img).text((8, 8), text, font=font, fill=255)
        return img.tobytes()

    return mask(char) != mask("\ue000")  # U+E000 是私用区，正常字体都没有这个字形
