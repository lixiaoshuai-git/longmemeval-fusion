"""时间规则解析单测（fusion.resolve_when_expr / resolve_when）。

这里曾经是全系统最大的翻车点：模糊词让模型猜日期。
规则解析是确定性核心，任何行为变化都必须先过这一关。
"""
from datetime import datetime

from memory_sys.fusion import (_shift_month_keep_day, _valid_range,
                               resolve_when, resolve_when_expr)

SD = datetime(2023, 5, 28)          # 统一的陈述时间锚点


def test_last_month_is_calendar_month():
    assert resolve_when_expr("last month", SD) == (
        datetime(2023, 4, 1), datetime(2023, 5, 1))


def test_two_weeks_ago_point_estimate():
    # rug 题教训：相对表达给点估计，不是月边界区间
    assert resolve_when_expr("two weeks ago", SD) == (
        datetime(2023, 5, 14), datetime(2023, 5, 15))


def test_numeric_days_ago():
    assert resolve_when_expr("3 days ago", SD) == (
        datetime(2023, 5, 25), datetime(2023, 5, 26))


def test_a_month_ago_keeps_day():
    assert resolve_when_expr("a month ago", SD) == (
        datetime(2023, 4, 28), datetime(2023, 4, 29))


def test_month_end_clamp():
    # 1/31 + 1 个月不溢出成 2/31
    assert _shift_month_keep_day(datetime(2023, 1, 31), 1) == datetime(2023, 2, 28)


def test_yesterday():
    assert resolve_when_expr("yesterday", SD) == (
        datetime(2023, 5, 27), datetime(2023, 5, 28))


def test_fuzzy_words_refused():
    # 铁律：模糊词绝不解析、绝不信任模型猜测
    assert resolve_when_expr("recently", SD) is None
    assert resolve_when_expr("a while ago", SD) is None
    assert resolve_when_expr("not long ago", SD) is None


def test_month_without_year_defaults_past():
    # 5 月的问题里说 December → 上一个 12 月（记忆默认过去）
    assert resolve_when_expr("in December", SD) == (
        datetime(2022, 12, 1), datetime(2023, 1, 1))


def test_month_with_explicit_year():
    assert resolve_when_expr("December 2023", SD) == (
        datetime(2023, 12, 1), datetime(2024, 1, 1))


def test_bare_year():
    assert resolve_when_expr("back in 2021", SD) == (
        datetime(2021, 1, 1), datetime(2022, 1, 1))


def test_valid_range_rejects_far_future():
    # 陈述时说"December 2023"（5 月）→ +60d 之外，规则拒绝
    assert _valid_range((datetime(2023, 12, 1), datetime(2024, 1, 1)), SD) is None


def test_valid_range_accepts_past():
    assert _valid_range((datetime(2023, 5, 14), datetime(2023, 5, 15)), SD) == (
        datetime(2023, 5, 14), datetime(2023, 5, 15))


def test_resolve_when_rule_first_then_model_fallback():
    assert resolve_when("two weeks ago", "", SD) == (
        datetime(2023, 5, 14), datetime(2023, 5, 15))
    # 规则 miss（模糊词）→ 模型给的绝对日期兜底
    assert resolve_when("recently", "2023-05-20", SD) == (
        datetime(2023, 5, 20), datetime(2023, 5, 21))


def test_resolve_when_all_miss():
    assert resolve_when("recently", "", SD) is None
