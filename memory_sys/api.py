"""对外接口：把内部的 write / read 收敛成 5 个直观动作。

为什么需要这层
--------------
原来的用法是 `FusionMemory.write(session, date)` + `read(question, date) -> str`。
问题在于 `read()` 返回的是一段**给 LLM 的上下文**，不是答案；查询计划、
命中的候选事实只能从 `last_trace` 这个侧门拿。演示、答辩、换模型复现时都
要先解释这件事。facade 把它收敛成：

    mem = Memory(llm=client)
    mem.add_session(turns, "2023-05-01")
    r = mem.recall("How many tanks do I have?", "2023-06-01")
    a = mem.answer("How many tanks do I have?", "2023-06-01")

- `recall()` 返回 `Recall`：上下文 + 查询计划 + 候选事实 + 段落，结构化可检查；
- `answer()` 返回 `Answer`：答案文本 + 它依据的那次 recall；
- `llm` 只需要 `.chat(prompt, max_tokens=...)`，换模型不改记忆逻辑（手册 B.3）。

写入 / 更新 / 检索 / 遗忘在接口上是四个分开的动作（手册 B.5），
`forget()` 委托给 lifecycle，`stats()` 给出规模指标。
"""
from __future__ import annotations

import pathlib
import sys
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_HERE), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fusion import FusionMemory                      # noqa: E402
from lme_core import build_prompt, parse_dt          # noqa: E402
from options import RuntimeOptions                    # noqa: E402


@dataclass(frozen=True)
class Recall:
    """一次检索的完整结果（不只是给模型看的字符串）。"""

    question: str
    question_date: str
    context: str
    plan: Mapping[str, Any] = field(default_factory=dict)
    sections: tuple[str, ...] = ()
    candidates: tuple[Mapping[str, Any], ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:            # 直接当上下文用
        return self.context


@dataclass(frozen=True)
class Answer:
    """一次作答：文本 + 依据的 Recall + 耗时。"""

    text: str
    recall: Recall
    latency_s: float = 0.0

    def __str__(self) -> str:
        return self.text


def _norm_turns(turns: Sequence[Mapping[str, Any]]) -> list[dict]:
    if isinstance(turns, (str, bytes)) or not isinstance(turns, (list, tuple)):
        raise TypeError("turns 必须是 [{'role': ..., 'content': ...}, ...]")
    if not turns:
        raise ValueError("turns 不能为空")
    out = []
    for i, t in enumerate(turns):
        if not isinstance(t, Mapping) or "role" not in t or "content" not in t:
            raise ValueError(f"第 {i} 条缺少 role/content: {t!r}")
        role = str(t["role"])
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"第 {i} 条 role 非法: {role!r}")
        out.append({"role": role, "content": str(t["content"])})
    return out


def _check_date(value: Any) -> str:
    if parse_dt(value) is None:
        raise ValueError(
            f"无法解析日期 {value!r}；支持 ISO（2023-05-01）与数据集格式"
            "（2023/05/01 (Mon) 17:50）")
    return str(value)


class Memory:
    """长期记忆系统的对外门面（默认 fusion 策略）。

    典型用法::

        mem = Memory(llm=client)                 # client 需有 .chat()
        mem.add_session(turns, "2023-05-01")
        r = mem.recall("How many tanks?", "2023-06-01")
        print(r.context)                         # 传给模型的记忆块
        a = mem.answer("How many tanks?", "2023-06-01")

    只做检索（不生成答案）时，`llm` 仅用于解析查询；如果查询计划已被缓存，
    可以不传 `llm` 做离线回放。
    """

    def __init__(self, llm: Any = None, *,
                 cache: dict | None = None,
                 cache_path: str | None = None,
                 options: RuntimeOptions | None = None,
                 strategy: str = "fusion"):
        if strategy != "fusion":
            raise ValueError(
                f"当前 facade 只封装 fusion 策略，收到 {strategy!r}；"
                "基线策略请直接用 scripts/memory.py 的 build_memory()")
        self._llm = llm
        self._mem = FusionMemory(client=llm, cache=cache,
                                 cache_path=cache_path, options=options)
        self._sessions = 0

    # -- 写入 ---------------------------------------------------------

    def add_session(self, turns: Sequence[Mapping[str, Any]], date: str) -> int:
        """写入一个会话；返回本次新增的事件数。"""
        clean = _norm_turns(turns)
        stamp = _check_date(date)
        before = self._mem.stats()["events"]
        self._mem.write(clean, stamp)
        self._sessions += 1
        return self._mem.stats()["events"] - before

    def add_sessions(self, sessions: Iterable[tuple[Sequence[Mapping[str, Any]], str]]) -> int:
        """批量写入 ``[(turns, date), ...]``；返回新增事件总数。"""
        added = 0
        for turns, date in sessions:
            added += self.add_session(turns, date)
        return added

    # -- 检索 / 作答 ---------------------------------------------------

    def recall(self, question: str, question_date: str) -> Recall:
        """检索并组装上下文；不调用答题模型。"""
        q = str(question).strip()
        if not q:
            raise ValueError("question 不能为空")
        stamp = _check_date(question_date)
        try:
            context = self._mem.read(q, stamp)
        except AttributeError as exc:      # client=None 且解析缓存未命中
            if self._llm is None:
                raise RuntimeError(
                    "recall() 需要 llm 来解析查询（未见过的问题必然要一次调用）；"
                    "只有解析缓存已命中时才可以不传 llm") from exc
            raise
        trace = getattr(self._mem, "last_trace", {}) or {}
        ex = trace.get("execution") or {}
        return Recall(
            question=q,
            question_date=stamp,
            context=context,
            plan=dict(trace.get("plan") or {}),
            sections=tuple(trace.get("sections") or ()),
            candidates=tuple(ex.get("trace_candidates") or ()),
            stats=dict(self._mem.stats()),
        )

    def answer(self, question: str, question_date: str, *,
               max_tokens: int = 512, cot: bool = False) -> Answer:
        """检索 + 调用 ``llm.chat`` 生成答案；返回文本与依据。"""
        if self._llm is None:
            raise RuntimeError(
                "answer() 需要 llm（任意带 .chat(prompt, max_tokens=...）的对象）；"
                "只做检索请用 recall()")
        rec = self.recall(question, question_date)
        inst = SimpleNamespace(question=rec.question,
                               question_date=rec.question_date)
        prompt = build_prompt(inst, rec.context, cot=cot)
        t0 = time.perf_counter()
        text = self._llm.chat(prompt, max_tokens=max_tokens)
        return Answer(text=str(text).strip(), recall=rec,
                      latency_s=round(time.perf_counter() - t0, 3))

    # -- 遗忘 / 观测 ---------------------------------------------------

    def forget(self, **policy) -> dict:
        """遗忘/生命周期：``ttl_days`` / ``capacity`` / ``compact_days``。

        默认不启用；软归档，不影响 ``value_at`` 的历史审计。
        """
        return self._mem.forget(**policy)

    def stats(self) -> dict:
        out = dict(self._mem.stats())
        out["sessions"] = self._sessions
        out["options"] = {
            "premise_proper_only": self._mem.options.premise_proper_only,
            "domain_terms": len(self._mem.options.domain_terms),
            "ablation": {
                "arith": self._mem.options.enable_arith,
                "excerpts": self._mem.options.enable_excerpts,
                "floor": self._mem.options.enable_floor,
            },
        }
        return out

    def save_cache(self, path: str | None = None) -> None:
        """落盘 LLM 调用缓存（离线回放/断点续跑用）。"""
        if path:
            self._mem.cache_path = path
        self._mem.save_cache()

    def reset(self) -> None:
        """清空记忆内容，保留缓存与配置（评测里每题一座独立记忆库）。"""
        self._mem.reset()
        self._sessions = 0

    def __repr__(self) -> str:
        s = self._mem.stats()
        return (f"Memory(sessions={self._sessions}, events={s['events']}, "
                f"evidence={s['evidence']})")
