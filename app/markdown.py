r"""极简 Markdown → HTML（受限子集，**不放行原始 HTML**）。

为什么不装一个库：

* 通知与赛事信息是「**管理员写、所有人看**」的内容。任何允许原始 HTML 的实现，
  都等于给赛事管理员一个 XSS 入口——他能借此摸到服务器管理员的会话。
  这里**先整体转义、再做结构解析**，所以写进来的标签只会被显示出来；
* 只支持一套够写图文公告的子集，渲染结果可预测、样式统一（全部走主题色）；
* 在后端渲染一次：前端不必引入 MD 库（保持零依赖），也不会出现
  「编辑器预览好好的、发布出来另一个样」。

支持的写法：

| 语法 | 效果 |
| --- | --- |
| `# ` ~ `###### ` | 标题（H1~H6，界面默认给 H1~H4） |
| `**粗**` / `*斜*` / `_斜_` / `~~删除~~` | 行内强调 |
| `` `代码` `` / ```` ``` ```` 代码块 | 等宽显示 |
| `> 引用` | 引用块 |
| `- ` / `* ` / `1. ` | 无序 / 有序列表（两个空格缩进可嵌套） |
| `\| a \| b \|` + `\|---\|---\|` | 表格 |
| `[文字](链接)` | 链接 |
| `![说明](图片 =50%)` | 图片（可带尺寸，见下） |
| `---` | 分隔线 |

图片尺寸：`![图](/api/media/x.png =320)`、`=320x180`、`=50%` 三种写法。
只认「数字（可选 × 数字）或百分比」，不会被拼进任意样式。

**段落里的单个换行会渲染成 `<br>`**（GFM 风格的硬换行）：写通知的人不会
为了换行去敲两个空格，而公告里换行本来就是有意义的。
"""

from __future__ import annotations

import re

#: 允许出现的链接协议（其余一律丢弃：``javascript:`` / ``data:`` 是经典 XSS 入口）
_SAFE_SCHEMES = ("http://", "https://", "mailto:")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)\s*$")
# 注意：解析发生在**转义之后**，所以这里的 ``>`` 已经是 ``&gt;``——
# 这样写进来的 HTML 标签只会被显示出来，而结构解析仍然照常работа（见模块说明）。
_QUOTE_RE = re.compile(r"^\s{0,3}&gt;\s?(.*)$")
_UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$")
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+(?P<size>[^)]*))?\)")
_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<href>[^)\s]+)(?:\s+[^)]*)?\)")
_SIZE_RE = re.compile(r"^=?(?P<w>\d{1,5})(?:x(?P<h>\d{1,5}))?$|^=?(?P<pct>\d{1,3})%$")
_BARE_URL_RE = re.compile(r"(?<![\"'>=])\bhttps?://[^\s<>()\[\]]+")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_STRONG_RE = re.compile(r"\*\*(?P<text>.+?)\*\*|__(?P<text2>.+?)__")
_EM_RE = re.compile(r"(?<![\w*])\*(?P<text>[^*\n]+)\*(?![\w*])|(?<![\w_])_(?P<text2>[^_\n]+)_(?![\w_])")
_DEL_RE = re.compile(r"~~(?P<text>.+?)~~")


def escape(text: str) -> str:
    """转义 HTML 元字符（渲染的第一步，保证后面拼的标签都是我们自己写的）。"""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_href(url: str) -> str:
    """链接只放行 http(s) / mailto / 站内相对路径。"""
    clean = (url or "").strip()
    if not clean:
        return ""
    if clean.startswith("/") and not clean.startswith("//"):
        return clean
    if clean.lower().startswith(_SAFE_SCHEMES):
        return clean
    return ""


def _size_attrs(size: str) -> str:
    """把 ``=320x180`` / ``=50%`` 变成 width/height 属性；认不出来就一个字都不加。"""
    raw = (size or "").strip()
    if not raw or not raw.startswith("="):
        return ""
    match = _SIZE_RE.match(raw)
    if match is None:
        return ""
    if match.group("pct"):
        pct = min(100, max(5, int(match.group("pct"))))
        return f' style="width:{pct}%;height:auto"'
    width = min(4000, max(16, int(match.group("w") or 0)))
    out = f' width="{width}"'
    if match.group("h"):
        out += f' height="{min(4000, max(16, int(match.group("h"))))}"'
    return out


def _inline(text: str) -> str:
    """行内元素。``text`` 必须已经过 :func:`escape`。"""
    # 代码片段先摘出来，避免其中的 * _ 被当成强调
    codes: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE_SPAN_RE.sub(_stash_code, text)

    def _image(match: re.Match[str]) -> str:
        src = _safe_href(match.group("src"))
        if not src:
            return match.group(0)
        alt = match.group("alt") or ""
        return (
            f'<img src="{src}" alt="{alt}" loading="lazy" decoding="async"'
            f"{_size_attrs(match.group('size') or '')}>"
        )

    def _link(match: re.Match[str]) -> str:
        href = _safe_href(match.group("href"))
        if not href:
            return match.group("text")
        rel = ' target="_blank" rel="noopener noreferrer"' if href.startswith("http") else ""
        return f'<a href="{href}"{rel}>{match.group("text")}</a>'

    out = _IMAGE_RE.sub(_image, out)
    out = _LINK_RE.sub(_link, out)
    out = _STRONG_RE.sub(lambda m: f"<strong>{m.group('text') or m.group('text2')}</strong>", out)
    out = _EM_RE.sub(lambda m: f"<em>{m.group('text') or m.group('text2')}</em>", out)
    out = _DEL_RE.sub(lambda m: f"<del>{m.group('text')}</del>", out)
    # 裸链接（写公告的人经常直接粘地址）
    out = _BARE_URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener noreferrer">{m.group(0)}</a>',
        out,
    )
    for index, code in enumerate(codes):
        out = out.replace(f"\x00{index}\x00", f"<code>{code}</code>")
    return out


def _table(rows: list[list[str]]) -> str:
    head, *body = rows
    cells = "".join(f"<th>{cell}</th>" for cell in head)
    lines = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row[: len(head)]) + "</tr>" for row in body
    )
    return (
        '<div class="md__table-wrap"><table class="md__table">'
        f"<thead><tr>{cells}</tr></thead><tbody>{lines}</tbody></table></div>"
    )


def _split_row(line: str) -> list[str]:
    body = line.strip().removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in body.split("|")]


def render(text: str) -> str:
    """Markdown → HTML。输入为空时返回空串。"""
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return ""

    lines = escape(source).split("\n")
    html: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            html.append("<p>" + "<br>".join(_inline(item) for item in paragraph) + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]

        # 代码块：整段原样（已转义）放进 <pre>
        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)[0]
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith(marker * 3):
                block.append(lines[index])
                index += 1
            index += 1  # 跳过收尾的 ```
            html.append('<pre class="md__pre"><code>' + "\n".join(block) + "</code></pre>")
            continue

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        if _HR_RE.match(line):
            flush_paragraph()
            html.append("<hr>")
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            html.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        # 表格：表头 + 分隔行 + 若干数据行
        if line.strip().startswith("|") and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]):
            flush_paragraph()
            rows = [_split_row(line)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_row(lines[index]))
                index += 1
            html.append(_table(rows))
            continue

        if _QUOTE_RE.match(line):
            flush_paragraph()
            block = []
            while index < len(lines) and (quote := _QUOTE_RE.match(lines[index])):
                block.append(quote.group(1))
                index += 1
            html.append("<blockquote>" + "<br>".join(_inline(item) for item in block) + "</blockquote>")
            continue

        if _UL_RE.match(line) or _OL_RE.match(line):
            flush_paragraph()
            html.append(_list(lines, index))
            index = _list_end(lines, index)
            continue

        paragraph.append(line.strip())
        index += 1

    flush_paragraph()
    return "".join(html)


def _list_match(line: str) -> tuple[int, str, str] | None:
    unordered = _UL_RE.match(line)
    if unordered:
        return (len(unordered.group(1)), "ul", unordered.group(3))
    ordered = _OL_RE.match(line)
    if ordered:
        return (len(ordered.group(1)), "ol", ordered.group(3))
    return None


def _list_end(lines: list[str], start: int) -> int:
    """列表到哪一行结束（遇到空行且下一行不是列表项就收尾）。"""
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            if _list_match(nxt) is None:
                break
            index += 1
            continue
        if _list_match(line) is None:
            break
        index += 1
    return index


def _list(lines: list[str], start: int) -> str:
    """列表渲染（支持两空格一层的嵌套）。"""
    end = _list_end(lines, start)
    items = [item for line in lines[start:end] if (item := _list_match(line)) is not None]
    out: list[str] = []
    pos = 0
    while pos < len(items):
        chunk, pos = _list_items(items, pos, items[pos][0])
        out.append(chunk)
    return "".join(out)


def _list_items(items: list[tuple[int, str, str]], pos: int, indent: int) -> tuple[str, int]:
    """渲染同一类型、同一缩进的一层列表；返回 ``(html, 下一个位置)``。

    子列表放在父 ``<li>`` **里面**——``<ul>`` 直接套 ``<ul>`` 是不合法的 HTML，
    浏览器会自己猜，结果往往与写的人预期不符。
    """
    kind = items[pos][1]
    out = [f"<{kind}>"]
    index = pos
    while index < len(items):
        cur_indent, cur_kind, content = items[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            # 更深的一层：递归渲染后并入上一个 <li>（若还没有，就当作本层的项）
            sub, index = _list_items(items, index, cur_indent)
            if out[-1].endswith("</li>"):
                out[-1] = out[-1][: -len("</li>")] + sub + "</li>"
            else:
                out.append(sub)
            continue
        if cur_kind != kind:
            break  # 同层换了类型：交给上层再开一个列表
        item = f"<li>{_inline(content)}"
        index += 1
        if index < len(items) and items[index][0] > indent:
            sub, index = _list_items(items, index, items[index][0])
            item += sub
        out.append(item + "</li>")
    out.append(f"</{kind}>")
    return "".join(out), index


# --------------------------------------------------------------------------- #
# 纯文本摘要（卡片列表、列表预览与搜索用）
# --------------------------------------------------------------------------- #
_STRIP_RE = re.compile(
    r"`{1,3}|~{2}|\*{1,2}|_{1,2}|^#{1,6}\s*|^>\s?|^\s*[-*+]\s+|^\s*\d+[.)]\s+",
    re.MULTILINE,
)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TEXT_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def to_text(text: str, limit: int = 0) -> str:
    """把 Markdown 压成一行纯文本（图片换成「[图片]」），供卡片摘要与检索用。"""
    body = str(text or "")
    body = _IMG_RE.sub(" [图片] ", body)
    body = _LINK_TEXT_RE.sub(r"\1", body)
    # 行首标记必须**逐行**剥掉：先合并空白的话，``^`` 就再也匹配不上了
    lines = [_STRIP_RE.sub("", line) for line in body.split("\n")]
    body = " ".join(lines)
    body = body.replace("|", " ")
    body = re.sub(r"\s+", " ", body).strip()
    if limit and len(body) > limit:
        return body[:limit].rstrip() + "…"
    return body
