"""事件构造必须只有一条路径（原则：耦合度 / 单一构造点）。

历史：`add_event` 与写入期执行器各写一份 Event 构造，v3.10 的
「evolve 丢时间」正是两套逻辑漂移的产物。现在两者都走 `append_event`。
"""
from datetime import datetime

from memory_sys.schema import MemoryStore


def test_append_event_is_the_single_construction_point():
    store = MemoryStore()
    ev = store.append_event(
        subject="user", predicate="event", value="a clipped value",
        category="other", valid_from=datetime(2023, 1, 1),
        fact="the full fact sentence", date_text="last week",
        evidence=[("s1", 0)], stated_at=datetime(2023, 1, 2))
    assert ev.tx_seq == store.tx_seq == 1
    assert ev.fact == "the full fact sentence"       # fact 与 value 分离
    assert ev.value == "a clipped value"
    assert ev.slot_id is None                         # 由调用方决定
    assert store.stats["added"] == 1


def test_append_event_copies_evidence():
    store = MemoryStore()
    ptr = [("s1", 0)]
    ev = store.append_event(subject="user", predicate="event", value="v",
                            category="other", evidence=ptr)
    ptr.append(("s2", 1))
    assert ev.evidence == [("s1", 0)]


def test_add_event_still_dedups_through_the_same_path():
    store = MemoryStore()
    store.add_event("user", "event", "bought a notebook", "work",
                    datetime(2023, 1, 1), None, False, None, "", ("s1", 0))
    store.add_event("user", "event", "bought a notebook", "work",
                    datetime(2023, 1, 1), None, False, None, "", ("s2", 3))
    assert len(store.events) == 1
    assert store.events[0].evidence == [("s1", 0), ("s2", 3)]
    assert store.stats["merged"] == 1
