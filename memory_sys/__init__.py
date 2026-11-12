"""长期记忆系统。

对外入口（推荐）::

    from memory_sys import Memory
    mem = Memory(llm=client)
    mem.add_session(turns, "2023-05-01")

内部模块（write_path / read_path / render / temporal …）是实现细节，
不承诺稳定；需要扩展时优先通过 `RuntimeOptions` 与 `Memory` 的方法。
"""
from __future__ import annotations

__all__ = ["Memory", "Recall", "Answer"]


def __getattr__(name: str):
    """惰性导出，避免 `import memory_sys.render` 这类用法触发整套导入。"""
    if name in __all__:
        from .api import Answer, Memory, Recall
        return {"Memory": Memory, "Recall": Recall, "Answer": Answer}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
