"""可观测性与缓存版本拆分单测（v3.10 迭代基础设施）。"""
import hashlib
import json
from datetime import datetime

from memory_sys import fusion as fusion_mod
from memory_sys.fusion import FusionMemory


class FakeClient:
    """只回答一次 parse JSON；写入路径未使用。"""

    def __init__(self, payload: str = '{"intent":"agg_count","topic":"20-gallon tank","entities":["tank"]}'):
        self.payload = payload
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        return self.payload


def _key_for(scope: str, scope_key: str) -> str:
    """按某 scope 的**当前**版本算缓存键（版本会随提示词演进而变）。"""
    version = fusion_mod.PROMPT_VERSIONS[scope]
    return hashlib.sha1((version + "|" + scope_key).encode("utf-8")).hexdigest()[:16]


def test_prompt_version_split_keeps_scope_isolation():
    key = "ext|unit-test"
    mem = FusionMemory(client=FakeClient(), cache={_key_for("extract", key): ["cached"]})
    got = mem._cached(key, lambda: (_ for _ in ()).throw(AssertionError("cache miss")),
                      scope="extract")
    assert got == ["cached"]
    # 改 parse 版本不应影响 extract 的缓存命中（各边界独立演进）
    old = fusion_mod.PROMPT_VERSIONS["parse"]
    try:
        fusion_mod.PROMPT_VERSIONS["parse"] = "fusion-parse-vtest"
        assert mem._cached(key, lambda: ["recomputed"], scope="extract") == ["cached"]
    finally:
        fusion_mod.PROMPT_VERSIONS["parse"] = old


def test_read_emits_trace_with_plan_and_timing():
    mem = FusionMemory(client=FakeClient(), cache={})
    mem.store.add_event("user", "acquire", "20-gallon tank", "furniture",
                        datetime(2023, 3, 1), None, False, None, "",
                        ("s1", 0), stated_at=datetime(2023, 3, 1))
    mem.read("How many tanks do I have?", "2023-06-01")
    tr = mem.last_trace
    assert tr["plan"]["intent"] == "agg_count"
    assert tr["execution"]["trace_candidates"], "候选事件轨迹不能为空"
    assert "tank" in tr["execution"]["trace_candidates"][0]["fact"]
    assert tr["context_chars"] > 0
    assert tr["elapsed_ms"] >= 0
    json.dumps(tr, ensure_ascii=False)  # 必须可序列化


def test_trace_candidate_is_json_safe_and_compact():
    mem = FusionMemory(client=FakeClient(), cache={})
    for v in ("20-gallon tank", "1-gallon tank"):
        mem.store.add_event("user", "acquire", v, "furniture", datetime(2023, 3, 1),
                            None, False, None, "", ("s1", 0),
                            stated_at=datetime(2023, 3, 1))
    events = mem._active_events()
    rows = mem._trace_events([(e, 1.2345) for e in events])
    assert rows and set(rows[0]) == {"date", "score", "fact", "status", "slot_id"}
    assert rows[0]["score"] == 1.234
    json.dumps(rows)



