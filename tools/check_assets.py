"""前端静态资源自检（stdlib，无依赖）。

前端是原生 ESM + 手写 CSS，**没有打包器**——也就没有编译期帮我们抓错。这个脚本
补上最容易踩的四类问题，CI 与本地都跑同一个：

1. ``nte.css`` 括号配平（写多一个 ``}`` 会让后面所有样式静默失效）；
2. ``import ... from './x.js'`` 指向的文件真的存在（改名/挪文件后的漏网之鱼）；
3. **导入的符号真的被导出**（``import { foo }`` 而 ``x.js`` 里根本没有 ``foo``）——
   这类错没有任何静态检查兜着，只有在浏览器里点开那个页面才会炸；
4. ``index.html`` 里引用的 ``/static/...`` 文件真的存在；
5. **路由不会被页签回落踩掉**（``syncTabs`` 必须按目标页判断，见该函数注释）；
6. **帮助图这一份（若有）不比它的文案旧**，且文案里没有 Markdown 记号
   （``static/help.jpg`` 由服务启动时渲染，**不入库**，见该函数注释）。

用法：``uv run python tools/check_assets.py``
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
CSS = ROOT / "static" / "css" / "nte.css"
INDEX = ROOT / "static" / "index.html"
ICONS = JS_DIR / "icons.js"

# import ... from './xxx.js'   /   import './xxx.js'
_IMPORT_RE = re.compile(r"""(?:from\s*|import\s*)['"](\./[^'"]+\.js)['"]""")
# data-icon="xxx"（静态 HTML 里的图标）
_DATA_ICON_RE = re.compile(r'data-icon="([A-Za-z0-9_-]+)"')
# import { a, b as c } from './x.js'（跨行也要认）
_NAMED_IMPORT_RE = re.compile(
    r"""import\s*\{(?P<names>[^}]*)\}\s*from\s*['"](?P<target>\./[^'"]+\.js)['"]""",
    re.DOTALL,
)
# export function foo / export async function foo / export const foo / export class foo
_EXPORT_DECL_RE = re.compile(
    r"""export\s+(?:async\s+)?(?:function|const|let|var|class)\s+(?P<name>[A-Za-z_$][\w$]*)"""
)
# export { a, b as c }
_EXPORT_LIST_RE = re.compile(r"""export\s*\{(?P<names>[^}]*)\}""", re.DOTALL)
_ASSET_RE = re.compile(r"""['"(](/static/[^'")\s]+)['")]""")


def _exported_names(path: Path) -> set[str]:
    """这个模块导出的名字（够用于「导入了不存在的符号」这类检查）。"""
    text = path.read_text(encoding="utf-8")
    names = set(_EXPORT_DECL_RE.findall(text))
    for block in _EXPORT_LIST_RE.findall(text):
        for item in block.split(","):
            name = item.strip().split(" as ")[-1].strip()
            if name:
                names.add(name)
    # export * from './x.js' 之类：不解析，交给运行时（这里只保证不误报）
    return names


def check_css() -> list[str]:
    if not CSS.exists():
        return [f"缺少样式表：{CSS.relative_to(ROOT)}"]
    text = CSS.read_text(encoding="utf-8")
    opened, closed = text.count("{"), text.count("}")
    if opened != closed:
        return [f"nte.css 括号不配平：{opened} 个 {{ / {closed} 个 }}"]
    print(f"  OK  nte.css 括号配平（{opened} 对）")
    return []


def check_imports() -> list[str]:
    problems: list[str] = []
    files = sorted(JS_DIR.glob("*.js"))
    for path in files:
        source = path.read_text(encoding="utf-8")
        for target in _IMPORT_RE.findall(source):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                problems.append(f"{path.name} 引用了不存在的模块：{target}")
    if not problems:
        print(f"  OK  {len(files)} 个前端模块的相对导入都存在")
    return problems


def check_exported_symbols() -> list[str]:
    """导入的符号必须真的被导出。

    浏览器里这属于「模块实例化失败」：整页白屏、控制台一句 ``does not provide
    an export named``。既然没有打包器，就由这个脚本兜住。
    """
    problems: list[str] = []
    checked = 0
    for path in sorted(JS_DIR.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for match in _NAMED_IMPORT_RE.finditer(source):
            target = (path.parent / match.group("target")).resolve()
            if not target.exists():
                continue  # 文件不存在由上一个检查报，这里不重复
            exported = _exported_names(target)
            if not exported:
                continue  # 该模块没有可解析的导出（如纯副作用模块），不误报
            for item in match.group("names").split(","):
                name = item.strip().split(" as ")[0].strip()
                if not name or name.startswith("//"):
                    continue
                checked += 1
                if name not in exported:
                    problems.append(f"{path.name} 导入了 {target.name} 没有导出的 {name}")
    if not problems:
        print(f"  OK  {checked} 处跨模块导入的符号都存在")
    return problems


def check_index_assets() -> list[str]:
    if not INDEX.exists():
        return [f"缺少首页：{INDEX.relative_to(ROOT)}"]
    problems: list[str] = []
    for ref in sorted(set(_ASSET_RE.findall(INDEX.read_text(encoding="utf-8")))):
        if not (ROOT / ref.lstrip("/")).exists():
            problems.append(f"index.html 引用了不存在的资源：{ref}")
    if not problems:
        print("  OK  index.html 引用的静态资源都存在")
    return problems


_ICON_CALL_RE = re.compile(r"\bicon\(\s*'([A-Za-z0-9_-]+)'")
_PANEL_ICON_RE = re.compile(r",\s*'([A-Za-z0-9_-]+)'\s*\]")


def _icon_names() -> set[str]:
    """「图标表」里的全部名字。

    直接解析 ``PATHS = {…}`` 那个对象字面量的键：够用、也不引入 JS 解析器。
    """
    text = ICONS.read_text(encoding="utf-8")
    block = text.split("const PATHS = {", 1)[-1].split("\n};", 1)[0]
    return set(re.findall(r"^\s{2}([A-Za-z_$][\w$]*)\s*:", block, re.MULTILINE))


def check_icons() -> list[str]:
    """图标名必须真的存在。

    写错一个名字不会报错、不会白屏，只是**那个位置静静少一个图标**——
    正是那种没人会去查、但一眼就能看出来的瑕疵。所以静态查一遍。
    """
    if not ICONS.exists():
        return [f"缺少图标表：{ICONS.relative_to(ROOT)}"]
    known = _icon_names()
    if not known:
        return ["没能解析出 icons.js 的图标表（改结构了？请同步本脚本）"]

    problems: list[str] = []
    for name in sorted(set(_DATA_ICON_RE.findall(INDEX.read_text(encoding="utf-8")))):
        if name not in known:
            problems.append(f'index.html 用了不存在的图标：data-icon="{name}"')

    for path in sorted(JS_DIR.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for name in sorted(set(_ICON_CALL_RE.findall(source))):
            if name not in known:
                problems.append(f"{path.name} 调用了不存在的图标：icon('{name}')")

    # 面板标题 → 图标 的关键词表：写错名字等于所有面板都少图标
    panel_block = (ICONS.read_text(encoding="utf-8").split("const PANEL_ICONS", 1)[-1]).split(
        "];", 1
    )[0]
    for name in sorted(set(_PANEL_ICON_RE.findall(panel_block))):
        if name not in known:
            problems.append(f"icons.js 的面板图标表引用了不存在的图标：{name}")

    if not problems:
        print(f"  OK  图标名都对得上（图标表 {len(known)} 个）")
    return problems


def check_sync_tabs_fallback() -> list[str]:
    """``syncTabs`` 的「页签收起 → 回落」必须按**目标页**判断，不能按 ``App.view``（旧值）。

    踩过的坑：从赛事页点页脚「开发者」去 ``/developer`` 时，`App.view` 还是 ``overview``，
    于是误触发一次「回落到总览」；而那一刻 ``App.routeEvent`` 已被清空，``goto('', 'overview')``
    又被「赛事页必须带届 ID」的规则弹回**主页**——表现就是「第一次点跳到主页，第二次才进去」。
    这类 bug 前端没有编译期保护，所以在这里钉一句源码约定。
    """
    source = (JS_DIR / "views.js").read_text(encoding="utf-8")
    bad = "!allow && App.view === view"
    if bad in source:
        return [
            (
                "views.js 的 syncTabs 又按 App.view（旧值）判断回落了："
                "会从赛事页去独立页时把路由踩回主页，请改成 page === view"
            )
        ]
    print("  OK  syncTabs 的页签回落按目标页判断（不会踩掉独立页路由）")
    return []


def _help_content():
    """加载帮助图的内容模块（零依赖；内容在 ``app/helpcard_content.py``）。"""
    path = ROOT / "app" / "helpcard_content.py"
    spec = importlib.util.spec_from_file_location("help_card_content", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件在就不会走到
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_help_card_freshness() -> list[str]:
    """帮助图：**本地若有这一份，它必须与文案同步**；内容里不许有 Markdown 记号。

    图是**代码渲染**的（``app/helpcard.py``），上面每一条命令群友都会照着打；
    文案改了不重画，发出去的就是一张写着旧命令的图——命令精确匹配，照着打**毫无反应**，
    而且群里没人知道为什么。

    注意：这张图**不入库**，服务启动时会自动重画一份（见 ``app/helpcard.py``），
    所以「没有这张图」是正常状态（干净检出就是这样），只比对**存在**的那一份：
    出图时把源文件指纹写进 ``static/help.jpg.src.sha256``，这里比对。
    """
    module = _help_content()
    problems: list[str] = []
    leaks = module.markdown_leaks() if module is not None else []
    if leaks:
        problems.append(
            "帮助图文案里出现了 Markdown 记号（图只会画字，星号会原样印出来）：" + "、".join(leaks)
        )
    art = ROOT / "static" / "help.jpg"
    if not art.exists():
        print("  --  没有 static/help.jpg（不入库，服务启动时会自动生成，跳过比对）")
        return problems
    if module is None:  # pragma: no cover - 见上
        return problems
    stamp = art.with_name(art.name + ".src.sha256")
    got = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
    if got != str(module.source_digest()):
        problems.append(
            "static/help.jpg 比它的文案旧（或指纹缺失）：重启一次服务会自动重画，"
            "也可以跑 `uv run --with pillow python tools/make_help_card.py`"
        )
    if not problems:
        print("  OK  帮助图与文案同步（static/help.jpg，且文案里没有 Markdown 记号）")
    return problems


def main() -> int:
    print("前端静态资源自检：")
    problems = (
        check_css()
        + check_imports()
        + check_exported_symbols()
        + check_icons()
        + check_index_assets()
        + check_sync_tabs_fallback()
        + check_help_card_freshness()
    )
    for line in problems:
        print(f"  !! {line}")
    if problems:
        print(f"\n共 {len(problems)} 处问题")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
