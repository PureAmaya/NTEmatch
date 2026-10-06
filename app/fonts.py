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
from pathlib import Path

from .logging_conf import get_logger

log = get_logger("font")

#: 中文候选（前两条是 Windows 的微软雅黑，站点字体栈里的中文就是它）
CJK_PATHS: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    # 单语言版（无 .ttc 的 face 索引问题）优先于多语言合集
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
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
    """扫常见字体目录，按文件名前缀认（发行版放在哪一层都能找到）。"""
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
                return path
    return None


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


def load(kind: str, size: int):
    """拿一个指定字号的字体对象（没有可用字体时退回 Pillow 内置位图字体）。"""
    key = (kind, int(size))
    hit = _fonts.get(key)
    if hit is not None:
        return hit
    from PIL import ImageFont

    path = resolve(kind)
    font = None
    if path is not None:
        try:
            font = ImageFont.truetype(str(path), int(size))
        except OSError as exc:  # 字体损坏 / 不是 TrueType：兜回内置
            log.warning("字体读不动，改用内置字体 | %s | %s", path, exc)
    if font is None:
        font = ImageFont.load_default(int(size))
    _fonts[key] = font
    return font
