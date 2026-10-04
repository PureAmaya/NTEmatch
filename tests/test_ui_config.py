"""界面配置的清洗规则：自定义主题色与分享图。

两者都是「人填进来的字符串」，所以边界必须在**模型层**收掉：分享图会原样出现在
``<meta property="og:image">`` 里，多塞一个协议就等于给自己开个口子。
"""

from __future__ import annotations

import pytest

from app.models import UiConfig


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#FF6A00", "#ff6a00"),  # 统一小写
        ("22e0e8", "#22e0e8"),  # 省掉 # 也认
        ("#0af", "#0af"),  # 三位简写保留
        ("", ""),  # 留空 = 用预设
        ("不是颜色", ""),  # 垃圾值一律置空，而不是塞进 CSS
        ("#12345", ""),  # 位数不对
        ("red", ""),  # 不支持颜色名（只收十六进制）
    ],
)
def test_accent_custom_cleaning(raw, expected):
    assert UiConfig(accent_custom=raw).accent_custom == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/static/share.png", "/static/share.png"),  # 站内路径
        ("https://cdn.example/og.png", "https://cdn.example/og.png"),
        ("http://cdn.example/og.png", "http://cdn.example/og.png"),
        ("", ""),
        ("javascript:alert(1)", ""),  # 这类协议必须被拒
        ("data:image/png;base64,AAAA", ""),
        ("ftp://example/og.png", ""),
    ],
)
def test_og_image_cleaning(raw, expected):
    assert UiConfig(og_image=raw).og_image == expected


def test_ui_config_keeps_other_fields():
    """清洗只影响这两个新字段，别把其它设置顺手改掉。"""
    ui = UiConfig(accent="lime", show_avatar=False, ticker="测试")
    assert (ui.accent, ui.show_avatar, ui.ticker) == ("lime", False, "测试")
