"""查询计划契约与静态审计的单测（零 LLM 调用）。"""
from datetime import datetime

from memory_sys.plan import QueryPlan, audit_plan, entities_not_in_question

QDATE = datetime(2023, 6, 1)
BASE = {
    "intent": "fact_at_time", "topic": "bird watching", "category": "event",
    "count_kind": None, "window_days": None, "month": None,
    "at_text": None, "before_text": None, "entities": ["bird watching"],
}


def test_plan_roundtrip_matches_dict():
    """dataclass 只是视图：转回去必须与缓存里的 dict 完全一致。"""
    assert QueryPlan.from_dict(BASE).to_dict() == BASE


def test_audit_passes_a_clean_plan():
    assert audit_plan(BASE, "When did I go bird watching?", QDATE) == []


def test_audit_flags_event_anchor_as_unresolved():
    """候选计划的 at_text 多是事件引用（"when I moved..."），规则解不出时间窗。"""
    plan = dict(BASE, at_text="when I moved to the United States")
    assert "at_text_unresolved" in audit_plan(plan, "When did I move?", QDATE)
    ok = dict(BASE, at_text="2023-03-31")
    assert "at_text_unresolved" not in audit_plan(ok, "Where was I on 2023-03-31?", QDATE)


def test_audit_flags_unknown_intent_and_missing_topic():
    plan = {"intent": "unknown", "topic": "", "entities": []}
    issues = audit_plan(plan, "What did I say?", QDATE)
    assert "intent_unknown" in issues and "topic_missing" in issues


def test_audit_flags_invalid_window_and_month():
    assert "window_days_invalid" in audit_plan(
        dict(BASE, window_days=-3), "q", QDATE)
    assert "month_invalid" in audit_plan(
        dict(BASE, month="2023-13"), "q", QDATE)


def test_entities_not_in_question():
    assert entities_not_in_question(BASE, "When did I go bird watching?") == []
    assert entities_not_in_question(BASE, "When did I move?") == ["bird watching"]
