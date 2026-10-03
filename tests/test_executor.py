"""双时间轴执行器与槽位算子单测（schema.MemoryStore / Slot）。

语义锚点（设计文档 §2）：
- 演化（evolve）：旧值曾为真 → 只封 τ_v，历史时刻仍可查
- 更正（correct）：旧值从未为真 → τ_v、τ_t 都封，任何时刻不可见
- 多值谓词不触发版本闭合；同值写入走去重合并
"""
from datetime import datetime

from memory_sys.schema import MemoryStore


def add(st, val, vf, vt=None, corrects=False, pred="live_in", cat="place"):
    return st.add_event("user", pred, val, cat, vf, vt, corrects,
                        None, "", ("s1", st.tx_seq), stated_at=vf)


def test_evolve_seals_temporal_axis_only():
    st = MemoryStore()
    add(st, "Berlin", datetime(2023, 1, 1))
    add(st, "Munich", datetime(2023, 6, 1))
    old, new = st.events
    assert old.status == "superseded" and old.valid_to == datetime(2023, 6, 1)
    assert new.status == "active"
    slot = st.slot("user", "live_in")
    assert slot.current().value == "Munich"
    # τ_v 只封未来：6/1 之前旧值仍为真
    assert slot.value_at(datetime(2023, 3, 1)).value == "Berlin"
    assert slot.value_at(datetime(2023, 9, 1)).value == "Munich"


def test_correct_seals_both_axes():
    st = MemoryStore()
    add(st, "Berlin", datetime(2023, 1, 1))
    add(st, "Munich", datetime(2023, 6, 1), corrects=True)
    assert st.events[0].status == "invalidated"
    slot = st.slot("user", "live_in")
    assert slot.current().value == "Munich"
    # 更正 = 旧值从未为真：**任何时刻**都不可见
    assert slot.value_at(datetime(2023, 3, 1)) is None


def test_multi_value_predicate_no_sealing():
    st = MemoryStore()
    add(st, "peace lily", datetime(2023, 2, 1), pred="acquire", cat="plant")
    add(st, "succulent", datetime(2023, 3, 1), pred="acquire", cat="plant")
    assert all(e.status == "active" for e in st.events)
    assert len(st.slot("user", "acquire").events) == 2


def test_same_value_merges_not_duplicates():
    st = MemoryStore()
    e1 = add(st, "peace lily", datetime(2023, 2, 1), pred="acquire", cat="plant")
    e2 = st.add_event("user", "acquire", "peace lilies", "plant",
                      datetime(2023, 3, 1), None, False, None, "",
                      ("s2", 0), stated_at=datetime(2023, 3, 1))
    assert e2 is e1                       # norm_value 折叠复数 → 同键去重
    assert len(e1.evidence) == 2
    assert st.stats["merged"] == 1


def test_empty_slot_returns_none():
    st = MemoryStore()
    assert st.slot("user", "live_in").current() is None
    assert st.slot("user", "live_in").value_at(datetime(2023, 1, 1)) is None


def test_norm_helpers():
    from memory_sys.schema import norm_amount, norm_value
    assert norm_value("The Peace Lilies") == "peace lily"
    assert norm_amount("$1,200") == 1200.0
    assert norm_amount("no number") is None
