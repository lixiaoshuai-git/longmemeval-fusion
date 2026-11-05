"""写入期时间几何：evolve 的新值必须使用 when_raw 的确定性解析结果。

回归用例来自 500 题审计：`Last month, the user attended an open mic night`
被写成陈述当天（2023-05-27）而不是 2023-04-01，读者据此算出的间隔整体偏大
（c9f37c46 答 3 个月，金标 2 个月）。
"""
from datetime import datetime

from memory_sys.fusion import FusionMemory


def _mem_with_target():
    mem = FusionMemory(client=None, cache={})
    mem.store.add_event("user", "event", "The user plans to attend an open mic night.",
                        "event", datetime(2023, 3, 1), None, False, None, "",
                        ("s1", 0), stated_at=datetime(2023, 3, 1))
    tgt = mem.store.events[-1]
    assert tgt.status == "active"
    return mem, tgt


def test_evolve_prefers_resolved_window_over_anchor():
    mem, tgt = _mem_with_target()
    cand = {"fact": "Last month, the user attended an open mic night at a local club.",
            "when_raw": "Last month", "when": "2023-04", "kind": "event",
            "category": "event", "_turn": 2,
            "_rng": (datetime(2023, 4, 1), datetime(2023, 5, 1))}
    mem._apply(cand, {"action": "evolve", "anchor": datetime(2023, 5, 27),
                      "target": tgt}, datetime(2023, 5, 27), "s2")
    new = [e for e in mem.store.events if "open mic" in (e.fact or "")][0]
    assert new.valid_from == datetime(2023, 4, 1)
    assert tgt.status == "superseded"


def test_evolve_falls_back_to_anchor_without_resolved_time():
    mem, tgt = _mem_with_target()
    cand = {"fact": "The user now prefers morning runs.", "when_raw": "",
            "when": "none", "kind": "preference", "category": "health",
            "_turn": 3, "_rng": None}
    mem._apply(cand, {"action": "evolve", "anchor": datetime(2023, 6, 2),
                      "target": tgt}, datetime(2023, 6, 2), "s3")
    new = [e for e in mem.store.events if "morning runs" in (e.fact or "")][0]
    assert new.valid_from == datetime(2023, 6, 2)
