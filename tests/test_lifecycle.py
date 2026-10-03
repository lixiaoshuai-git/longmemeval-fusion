"""遗忘/生命周期单测（memory_sys.lifecycle）。

语义锚点：遗忘 = 软归档——退出"当前有效事实"召回（status 过滤），
但历史时刻查询（value_at）仍可审计；策略幂等。
"""
from datetime import datetime

from memory_sys.lifecycle import apply_lifecycle
from memory_sys.schema import MemoryStore

NOW = datetime(2026, 10, 3)


def build_store():
    st = MemoryStore()
    # active 闭合事件（ttl 目标）：用户明确陈述了截止时间（"住到 6/1 为止"）
    # 注意不能用两条 live_in 制造闭合——那会走演化路径把第一条置为 superseded
    st.add_event("user", "live_in", "Berlin", "place", datetime(2023, 1, 1),
                 datetime(2023, 6, 1), False, None, "", ("s1", 0),
                 stated_at=datetime(2023, 1, 1))
    # 开放区间（ttl 不得动）：不同谓词，避免演化闭合
    st.add_event("user", "work_at", "Munich Office", "work", datetime(2023, 6, 1),
                 None, False, None, "", ("s1", 1), stated_at=datetime(2023, 6, 1))
    return st


def test_ttl_archives_expired_only():
    st = build_store()
    r = apply_lifecycle(st, now=NOW, ttl_days=365)
    assert r["counts"]["ttl"] == 1
    assert st.events[0].status == "archived"
    assert st.events[1].status == "active"          # 开放区间 = 仍为真


def test_capacity_archives_low_importance_first():
    st = MemoryStore()
    for i in range(5):
        st.add_event("user", "acquire", f"plant number {i}", "plant",
                     datetime(2026, 1, 1 + i), None, False, None, "",
                     ("s1", i), stated_at=datetime(2026, 1, 1 + i))
    r = apply_lifecycle(st, now=NOW, capacity=2)
    assert r["counts"]["capacity"] == 3
    act = [e for e in st.events if e.status == "active"]
    assert len(act) == 2


def test_capacity_within_limit_noop():
    st = build_store()
    apply_lifecycle(st, now=NOW, capacity=100)
    assert all(e.status != "archived" for e in st.events)


def test_compact_keeps_latest_old_version():
    st = MemoryStore()
    for i, val in enumerate(("Alpha", "Beta", "Gamma")):
        st.add_event("user", "work_at", val, "work", datetime(2020 + i, 1, 1),
                     None, False, None, "", ("s1", i),
                     stated_at=datetime(2020 + i, 1, 1))
    r = apply_lifecycle(st, now=NOW, compact_days=180)
    # 链上最老的两个 superseded 中，只归档更老的 Alpha，Beta 保留可审计
    assert r["counts"]["compact"] == 1
    assert st.events[0].status == "archived"
    assert st.events[1].status == "superseded"
    assert st.events[2].status == "active"


def test_idempotent():
    st = build_store()
    apply_lifecycle(st, now=NOW, ttl_days=365)
    r2 = apply_lifecycle(st, now=NOW, ttl_days=365)
    assert r2["counts"]["ttl"] == 0


def test_archived_exits_recall_but_auditable():
    st = build_store()
    apply_lifecycle(st, now=NOW, ttl_days=365)
    assert st.events[0] not in st.active_events()   # 退出当前召回
    slot = st.slot("user", "live_in")
    assert slot.value_at(datetime(2023, 3, 1)) is not None   # 历史仍可审计
