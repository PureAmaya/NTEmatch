"""推流标识与推流令牌**只收 ASCII**。

为什么这条要专门测：非 ASCII 的流名/令牌不会立刻报错，而是表现为
「推流地址用不了」「鉴权接口 500」这类离原因很远的现象；而旧实现里
``str.isalnum()`` 对中文返回 True，很容易以为已经过滤干净了。
"""

from __future__ import annotations

import pytest

from app.defaults import default_config
from app.logic import check_stream_key, clean_key, validate_config
from app.main import app
from app.models import Config, Player, StreamConfig


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tom", "tom"),
        ("live-1_2", "live-1_2"),
        ("  玩家甲  ", ""),  # 全是中文与空白：全被剔掉
        ("a中b文c", "abc"),  # 非 ASCII 一律剔除
        ("a b", "ab"),
        ("", ""),
    ],
)
def test_clean_key_drops_non_ascii(raw, expected):
    assert clean_key(raw) == expected


def test_clean_key_rejects_fullwidth_and_emoji():
    assert clean_key("ｔｏｍ") == ""  # 全角字母
    assert clean_key("tom🏐") == "tom"
    assert clean_key("中文id") == "id"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("tom", "tom"), ("live-1_2", "live-1_2"), ("  tom  ", "tom"), ("", "")],
)
def test_check_stream_key_accepts_ascii(raw, expected):
    assert check_stream_key(raw) == expected


@pytest.mark.parametrize("raw", ["中文id", "a b", "tom🏐", "ｔｏｍ"])
def test_check_stream_key_rejects_non_ascii(raw):
    """非法字符**直接报错**，不静默丢字符（否则用户照着填的地址推不动）。"""
    with pytest.raises(ValueError) as exc:
        check_stream_key(raw)
    assert "ASCII" in str(exc.value)


def test_push_token_must_be_ascii():
    assert StreamConfig(push_token="ok-token_1").push_token == "ok-token_1"
    for bad in ("中文令牌", "有 空格", "token🏐"):
        with pytest.raises(ValueError):
            StreamConfig(push_token=bad)


def test_validate_config_flags_legacy_non_ascii_stream_keys(make_config):
    """历史数据里的非 ASCII 流名不能拒绝加载，但必须**提示出来**。"""
    cfg: Config = make_config()
    cfg.players = [Player(id="p1", name="甲", stream_key="中文流名")]
    issues = validate_config(cfg)
    assert any("非 ASCII" in line for line in issues)


def test_default_config_has_no_non_ascii_stream_keys():
    """出厂配置本身要干净（不然新装站点一上来就带着提示）。"""
    cfg = Config.model_validate(default_config())
    for player in cfg.players:
        assert clean_key(player.stream_key) == player.stream_key


def test_value_error_becomes_400_not_500():
    """业务校验抛的 ``ValueError`` 必须被统一转成 400。

    上面那些 ``check_stream_key`` 依赖它：否则一个中文流名会让接口变成 500。
    """
    assert ValueError in app.exception_handlers


def test_validation_error_message_is_one_readable_line():
    """pydantic 的多行调试文本要收拾成一句话（前端 toast 只放得下一句）。"""
    from pydantic import ValidationError

    from app.main import _error_message

    with pytest.raises(ValidationError) as exc:
        StreamConfig(push_token="中文令牌")
    message = _error_message(exc.value)
    assert "ASCII" in message
    assert "\n" not in message
    assert "validation error" not in message.lower()
