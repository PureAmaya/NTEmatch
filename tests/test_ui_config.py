"""界面配置的清洗规则：分享图。

分享图是「人填进来的字符串」，所以边界必须在**模型层**收掉：它会原样出现在
``<meta property="og:image">`` 里，多塞一个协议就等于给自己开个口子。
（主题色已固定、不再随届次切换，因此没有相关的清洗规则。）
"""

from __future__ import annotations

import pytest

from app.models import UiConfig


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
    """清洗只影响分享图，别把其它设置顺手改掉。"""
    ui = UiConfig(show_avatar=False, ticker="测试")
    assert (ui.show_avatar, ui.ticker) == (False, "测试")
