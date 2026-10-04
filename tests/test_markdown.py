"""Markdown 渲染：该渲染的都渲染，该挡的都挡住。

这一块的**第一职责是安全**：通知与赛事信息是「管理员写、所有人看」的内容，
一旦能塞进原始 HTML 或 `javascript:` 链接，赛事管理员就成了打向服务器管理员的
XSS 跳板（会话令牌就在 localStorage 里）。所以这里的断言一半是「结构正确」，
另一半是「危险写法只被当普通文字」。
"""

from __future__ import annotations

import pytest

from app import markdown


# --------------------------------------------------------------------------- #
# 安全：不放行任何原始 HTML / 危险协议
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dirty",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">点我</a>',
        "<iframe src='//evil.example'></iframe>",
    ],
)
def test_raw_html_is_shown_as_text(dirty):
    html = markdown.render(dirty)
    assert "<script" not in html
    assert "<iframe" not in html
    assert "onerror" not in html or "&lt;" in html  # 只可能是被转义后的文字


def test_javascript_link_is_dropped():
    """``javascript:`` 链接退化成纯文字，绝不生成 href。"""
    html = markdown.render("[点我](javascript:alert(1))")
    assert "javascript:" not in html
    assert "<a " not in html
    assert "点我" in html


def test_data_url_image_is_dropped():
    html = markdown.render("![x](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:" not in html
    assert "<img" not in html


def test_relative_and_http_links_are_kept():
    html = markdown.render("[站内](/events) 与 [外链](https://example.com)")
    assert '<a href="/events">站内</a>' in html
    assert 'href="https://example.com"' in html
    assert 'rel="noopener noreferrer"' in html


def test_quotes_in_attributes_cannot_break_out():
    """属性注入：引号已被转义，塞不进 onerror 之类的东西。"""
    html = markdown.render('![x](/api/media/a.png" onerror="alert(1))')
    assert "onerror" not in html


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_headings(level):
    html = markdown.render("#" * level + " 标题")
    assert html == f"<h{level}>标题</h{level}>"


def test_inline_emphasis_and_code():
    html = markdown.render("**粗** *斜* _斜_ ~~删~~ `代码`")
    assert "<strong>粗</strong>" in html
    assert html.count("<em>斜</em>") == 2
    assert "<del>删</del>" in html
    assert "<code>代码</code>" in html


def test_emphasis_inside_code_is_untouched():
    """代码片段里的 * 不该变成强调——写命令 / 通配符时最常见。"""
    html = markdown.render("`*.png`")
    assert html == "<p><code>*.png</code></p>"


def test_quote_and_paragraph_break():
    html = markdown.render("> 第一行\n> 第二行")
    assert html == "<blockquote>第一行<br>第二行</blockquote>"
    # 段落里的单个换行就是换行（写公告的人不会为了换行去敲两个空格）
    assert markdown.render("甲\n乙") == "<p>甲<br>乙</p>"


def test_lists_flat_and_nested():
    html = markdown.render("- 甲\n- 乙\n  - 乙一\n  - 乙二\n- 丙")
    assert html == "<ul><li>甲</li><li>乙<ul><li>乙一</li><li>乙二</li></ul></li><li>丙</li></ul>"


def test_ordered_list():
    assert markdown.render("1. 甲\n2. 乙") == "<ol><li>甲</li><li>乙</li></ol>"


def test_list_then_paragraph_ends_the_list():
    html = markdown.render("- 甲\n\n普通一段")
    assert html == "<ul><li>甲</li></ul><p>普通一段</p>"


def test_table():
    html = markdown.render("| 项目 | 分值 |\n| --- | --- |\n| 排球 | 25 |")
    assert "<table" in html and "<th>项目</th>" in html and "<td>25</td>" in html


def test_horizontal_rule_and_code_block():
    html = markdown.render("---\n\n```python\nprint(1 < 2)\n```")
    assert "<hr>" in html
    assert "<pre" in html and "print(1 &lt; 2)" in html


# --------------------------------------------------------------------------- #
# 图片与尺寸
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("size", "expect"),
    [
        ("=320", 'width="320"'),
        ("=320x180", 'width="320" height="180"'),
        ("=50%", 'style="width:50%;height:auto"'),
    ],
)
def test_image_size_forms(size, expect):
    html = markdown.render(f"![示意](/api/media/a.png {size})")
    assert expect in html
    assert 'loading="lazy"' in html


def test_image_size_is_clamped_and_validated():
    """尺寸只认数字/百分比，且夹在合理范围内——不能借样式字段搞注入。"""
    assert 'width="4000"' in markdown.render("![a](/api/media/a.png =99999)")
    assert 'width="16"' in markdown.render("![a](/api/media/a.png =0)")
    crazy = markdown.render('![a](/api/media/a.png =100%;background:url(x))')
    assert "background" not in crazy
    assert 'width="16"' not in crazy  # 认不出来就干脆不加尺寸


def test_bare_url_becomes_link():
    html = markdown.render("见 https://nte.example.com/x")
    assert '<a href="https://nte.example.com/x"' in html


# --------------------------------------------------------------------------- #
# 纯文本摘要
# --------------------------------------------------------------------------- #
def test_to_text_strips_markup_and_truncates():
    text = markdown.to_text("# 标题\n\n![图](/api/media/a.png)\n\n**粗** 正文", limit=8)
    assert "#" not in text and "*" not in text
    assert "标题" in text
    assert "图片" in text          # 图片在摘要里占个位，不然卡片会像少了一段
    assert text.endswith("…")
    assert len(text) <= 9


def test_empty_input_renders_nothing():
    assert markdown.render("") == ""
    assert markdown.render("   \n  ") == ""
    assert markdown.to_text("") == ""
