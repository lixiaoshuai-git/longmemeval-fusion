"""对外接口单测：`from memory_sys import Memory` 的 5 个动作。

不调用任何网络：用假 client 按提示词类型返回抽取 / 解析 / 作答三种结果。
"""
from __future__ import annotations

import pytest

from memory_sys import Answer, Memory, Recall


class FakeLLM:
    """按提示词前缀分派：抽取 → 解析 → 作答。"""

    def __init__(self):
        self.calls: list[str] = []

    def chat(self, prompt: str, max_tokens: int = 512) -> str:
        self.calls.append(prompt[:60])
        if "extract durable facts" in prompt:
            return ('[{"fact": "I have 30 coins.", "when_raw": "", '
                    '"when": "none", "category": "other", "kind": "numeric"}]')
        if "Convert the question" in prompt:
            return ('{"intent": "agg_sum", "topic": "coins", '
                    '"entities": ["coins"]}')
        return "You have 30 coins."


def _mem(llm=None):
    return Memory(llm=llm if llm is not None else FakeLLM())


def test_add_session_returns_new_event_count():
    m = _mem()
    added = m.add_session([{"role": "user", "content": "I have 30 coins."}],
                          "2023-05-01")
    assert added == 1
    assert m.stats()["events"] == 1
    assert "Memory(sessions=1" in repr(m)


def test_add_sessions_bulk():
    m = _mem()
    n = m.add_sessions([
        ([{"role": "user", "content": "I have 30 coins."}], "2023-05-01"),
        ([{"role": "user", "content": "I have 30 coins."}], "2023-05-02"),
    ])
    assert n >= 1 and m.stats()["sessions"] == 2


def test_recall_returns_structured_result():
    m = _mem()
    m.add_session([{"role": "user", "content": "I have 30 coins."}], "2023-05-01")
    r = m.recall("How many coins do I have?", "2023-06-01")
    assert isinstance(r, Recall)
    assert r.plan["intent"] == "agg_sum"
    assert "[DETERMINISTIC QUERY RESULT]" in r.context
    assert "30" in r.context
    assert r.sections                      # 段落标题可检查
    assert str(r) == r.context             # 可直接当上下文用


def test_answer_uses_llm_and_keeps_recall():
    m = _mem()
    m.add_session([{"role": "user", "content": "I have 30 coins."}], "2023-05-01")
    a = m.answer("How many coins do I have?", "2023-06-01")
    assert isinstance(a, Answer)
    assert a.text == "You have 30 coins."
    assert a.recall.plan["intent"] == "agg_sum"
    assert a.latency_s >= 0
    assert str(a) == a.text


def test_answer_without_llm_fails_loudly():
    m = Memory()
    with pytest.raises(RuntimeError, match="answer\\(\\) 需要 llm"):
        m.answer("How many coins?", "2023-06-01")


def test_recall_without_llm_explains_why():
    m = Memory()
    with pytest.raises(RuntimeError, match="recall\\(\\) 需要 llm"):
        m.recall("An unseen question?", "2023-06-01")


def test_invalid_inputs_are_rejected_with_context():
    m = _mem()
    with pytest.raises(ValueError, match="无法解析日期"):
        m.add_session([{"role": "user", "content": "x"}], "not-a-date")
    with pytest.raises(ValueError, match="role/content"):
        m.add_session([{"content": "x"}], "2023-05-01")
    with pytest.raises(ValueError, match="question 不能为空"):
        m.recall("   ", "2023-06-01")


def test_forget_and_reset():
    m = _mem()
    m.add_session([{"role": "user", "content": "I have 30 coins."}], "2023-05-01")
    report = m.forget(ttl_days=0)          # 全部过期 → 软归档
    assert isinstance(report, dict)
    m.reset()
    assert m.stats()["events"] == 0 and m.stats()["sessions"] == 0


def test_stats_exposes_effective_options():
    m = _mem()
    s = m.stats()
    assert s["options"]["premise_proper_only"] is True
    assert s["options"]["ablation"] == {"arith": True, "excerpts": True,
                                        "floor": True}
