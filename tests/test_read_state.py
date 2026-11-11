"""读侧状态一致性：派生索引不能陈旧，相对时间锚点必须来自问题日期。"""
from datetime import datetime

from memory_sys.fusion import FusionMemory


def _add(mem, value, day=1):
    mem.store.add_event("user", "event", value, "work", datetime(2023, 1, day),
                        None, False, None, "", ("s1", 0),
                        stated_at=datetime(2023, 1, day))


def test_before_relative_time_uses_question_date():
    mem = FusionMemory(client=None, cache={})
    rng = mem._resolve_before({"before_text": "today"}, datetime(2023, 3, 2))
    assert rng == datetime(2023, 3, 2)


def test_entity_index_refreshes_after_status_change():
    mem = FusionMemory(client=None, cache={})
    _add(mem, "bought a notebook today")
    assert "notebook" in mem._entity_index()
    mem.store.events[0].status = "archived"   # 遗忘归档
    assert "notebook" not in mem._entity_index()


def test_entity_index_refreshes_after_direct_store_write():
    """绕过 write() 直接 add_event 时也必须更新（旧实现只在 write() 里失效）。"""
    mem = FusionMemory(client=None, cache={})
    _add(mem, "bought a notebook today")
    assert "notebook" in mem._entity_index()
    _add(mem, "returned a stapler yesterday", day=2)
    assert "stapler" in mem._entity_index()
