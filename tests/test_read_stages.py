"""read() 拆分后的阶段边界单测（v3.17）。

`read()` 现在只做编排：`_plan()`（解析 + 静态审计）与 `_render()`（执行 +
渲染 + 守卫）。这一层拆分的价值不在于好看，而在于两段可以分别调用、分别
断言，且渲染守卫是纯函数、能脱离 LLM 单独验证。
"""
from datetime import datetime

from memory_sys.fusion import FusionMemory


class FakeClient:
    def __init__(self, payload: str = '{"intent": "agg_count", '
                                      '"topic": "20-gallon tank", '
                                      '"entities": ["tank"]}'):
        self.payload = payload
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        return self.payload


def _mem(payload: str | None = None):
    client = FakeClient(payload) if payload else FakeClient()
    m = FusionMemory(client=client, cache={})
    m.store.add_event("user", "acquire", "20-gallon tank", "furniture",
                      datetime(2023, 3, 1), None, False, None, "",
                      ("s1", 0), stated_at=datetime(2023, 3, 1))
    return m


def test_plan_stops_before_execution():
    m = _mem()
    m.last_trace = {}
    qdate, sq = m._plan("How many tanks do I have?", "2023-06-01")
    assert qdate == datetime(2023, 6, 1)
    assert sq["intent"] == "agg_count"
    # 计划阶段的 trace 只有解析相关字段，不包含执行结果
    assert "plan" in m.last_trace and "plan_issues" in m.last_trace
    assert "parse_ms" in m.last_trace
    assert "execution" not in m.last_trace
    assert "deterministic_result" not in m.last_trace
    assert m.last_meta == {"intent": "agg_count", "topic": "20-gallon tank"}


def test_render_only_needs_plan_and_date():
    m = _mem()
    m.last_trace = {}
    qdate, sq = m._plan("How many tanks do I have?", "2023-06-01")
    ctx = m._render("How many tanks do I have?", sq, qdate)
    assert "[FACT MEMORY]" in ctx
    # 渲染阶段补齐执行与段落字段
    assert m.last_trace["execution"]["matched"] >= 1
    assert m.last_trace["sections"]
    assert m.last_trace["context_chars"] == len(ctx)


def test_read_is_orchestration_only():
    m = _mem()
    ctx = m.read("How many tanks do I have?", "2023-06-01")
    tr = m.last_trace
    # 三个阶段的字段都在：解析、执行、整体耗时
    assert {"plan", "parse_ms", "execute_ms", "elapsed_ms", "sections"} <= set(tr)
    assert tr["elapsed_ms"] >= tr["parse_ms"]
    assert ctx == "[DETERMINISTIC QUERY RESULT]\n" + tr["deterministic_result"] + \
        ctx[len("[DETERMINISTIC QUERY RESULT]\n" + tr["deterministic_result"]):]


def test_guard_sections_are_pure_and_ordered():
    m = _mem(payload='{"intent": "unknown", "topic": "siblings", '
                     '"entities": ["siblings"]}')
    qdate, sq = m._plan("How many siblings do I have?", "2023-06-01")
    first = m._guard_sections("How many siblings do I have?", sq)
    second = m._guard_sections("How many siblings do I have?", sq)
    assert first == second                       # 纯函数：同输入同输出
    assert all(s.startswith("[") for s in first)
    assert any("MEMORY SCOPE NOTE" in s for s in first)


def test_guard_sections_skip_advice_questions():
    m = _mem(payload='{"intent": "unknown", "topic": "slow cooker", '
                     '"entities": ["slow cooker"]}')
    qdate, sq = m._plan("Any advice on a slow cooker?", "2023-06-01")
    sections = m._guard_sections("Any advice on a slow cooker?", sq)
    assert any("RECOMMENDATION HINT" in s for s in sections)
    assert not any("MEMORY SCOPE NOTE" in s for s in sections)
