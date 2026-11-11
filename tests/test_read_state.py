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


# ---------------- 跨平台确定性：日期格式化 ----------------

def test_date_format_is_platform_independent():
    """`%Y` 对 1000 年以前不保证补零（glibc 出 '1-01-01'，MSVC 出 '0001-01-01'）。

    查询窗口下界常用 datetime.min，若用 strftime，同一份缓存在 Linux 与
    Windows 上会渲染出不同的确定性结果——CI（ubuntu）因此判定基线漂移。
    """
    from memory_sys.textutil import _fmt_d

    assert _fmt_d(datetime.min) == "0001-01-01"
    assert _fmt_d(datetime(2023, 6, 1)) == "2023-06-01"
    assert _fmt_d(None) == "?"


def test_unbounded_window_renders_fixed_width_dates():
    """无时间窗的查询在 header 里应出现四位年份，跨平台一致。"""
    mem = FusionMemory(client=None, cache={})
    _add(mem, "bought a notebook today")
    result, meta = mem._execute(
        {"intent": "list_events", "topic": "notebook",
         "before_text": "today"}, datetime(2023, 6, 1))
    assert "0001-01-01..2023-06-01" in result
