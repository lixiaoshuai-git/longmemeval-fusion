"""记忆系统接口与实现。

接口设计遵循项目手册的两条硬要求：

- **与底层 LLM 解耦**（B.3）：写路径与读路径只依赖结构化数据，不把
  prompt 字符串当作存储格式。更换模型不需要改记忆逻辑。
- **模块边界清晰**（B.5）：写入 / 更新 / 检索 / 遗忘四个动作在接口上分开，
  便于单测与答辩时逐项说明。

当前只实现一个基线策略 ``FullContext``：把全部会话原样拼进上下文。
它属于手册附录 F.1 明令禁止的「投机实现」，**仅用作开发期的性能上界探针**
与流水线连通性验证，不作为交付方案。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod

from lme_core import SESSION_TEMPLATE, render_session, strip_answer_labels


class MemorySystem(ABC):
    """记忆系统接口。

    生命周期：``reset()`` -> 多次 ``write()`` -> ``read()``
    """

    name: str = "abstract"

    @abstractmethod
    def reset(self) -> None:
        """清空内部状态，准备处理下一道题。"""

    @abstractmethod
    def write(self, session: list[dict], session_date: str) -> None:
        """写入一个会话。实现方负责抽取、更新与冲突处理。"""

    @abstractmethod
    def read(self, question: str, question_date: str) -> str:
        """按问题检索并组装上下文文本。"""

    # 可选：便于评估报告给出规模指标
    def stats(self) -> dict:
        return {}


class FullContext(MemorySystem):
    """基线：全量会话拼入上下文。

    .. warning::
       手册附录 F.1 第 2 条将「全量历史拼入上下文，以超长窗口替代记忆机制」
       列为投机实现。本类**仅用于建立性能上界**（报告中的 upper bound）
       与验证流水线，不得作为最终交付。
    """

    name = "full-context"

    def __init__(self, history_format: str = "nl", useronly: bool = False):
        self.history_format = history_format
        self.useronly = useronly
        self._chunks: list[tuple[str, str]] = []

    def reset(self) -> None:
        self._chunks = []

    def write(self, session: list[dict], session_date: str) -> None:
        clean = strip_answer_labels(session)
        body = render_session(clean, self.history_format, self.useronly)
        self._chunks.append((session_date, body))

    def read(self, question: str, question_date: str) -> str:
        out = []
        for i, (date, body) in enumerate(self._chunks):
            out.append(SESSION_TEMPLATE.format(i + 1, date, body))
        return "".join(out)

    def stats(self) -> dict:
        return {"sessions_kept": len(self._chunks)}


class UserTurnOnly(FullContext):
    """消融对照：只保留 user 轮次，丢弃 assistant 回复。

    LongMemEval 里相当一部分证据在 user 发言中，且 assistant 回复长且噪声大。
    用它来量化「assistant 回复是否值得占上下文预算」。
    """

    name = "user-turn-only"

    def __init__(self, history_format: str = "nl"):
        super().__init__(history_format=history_format, useronly=True)


REGISTRY: dict[str, type[MemorySystem]] = {
    "full-context": FullContext,
    "user-turn-only": UserTurnOnly,
}


def build_memory(strategy: str, **kwargs) -> MemorySystem:
    if strategy == "structured":
        # 第二层实现：需要 client（入口抽取 + 查询解析）
        # 设计文档：docs/layer2-data-structure.md
        import sys, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from memory_sys.structured import StructuredMemory
        return StructuredMemory(**kwargs)   # type: ignore[return-value]
    if strategy == "fusion":
        # v2 融合策略：自由文本事实 + 写入期对齐 + BM25 模糊回退 + 证据地板
        import sys, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from memory_sys.fusion import FusionMemory
        return FusionMemory(**kwargs)       # type: ignore[return-value]
    if strategy not in REGISTRY:
        raise KeyError(f"未知策略 {strategy!r}，"
                       f"可选: {sorted(REGISTRY) + ['structured', 'fusion']}")
    return REGISTRY[strategy](**kwargs)
