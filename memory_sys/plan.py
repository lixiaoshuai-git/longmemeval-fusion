"""查询计划：显式契约 + 确定性审计（不调用模型）。

`read_path._parse` 产出的计划此前是裸 dict，键缺失、类型错、时间没解析出来
都只能等执行阶段表现为「召回为空」。这里把计划变成 dataclass，并提供
`audit_plan()` —— 只做静态检查，不改变执行结果，问题以代码形式记进
`last_trace["plan_issues"]`，供 `regress.py --audit-plans` 汇总。

设计约束：
- `QueryPlan.to_dict()` 必须与旧 dict 逐键一致（缓存里存的就是 dict，
  改形状会让全部解析缓存失效）。
- 审计只读，不修补字段；发现问题不静默改计划。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 审计问题码 → 一句话解释（报告里直接展示）
ISSUE_TEXT = {
    "intent_unknown": "解析没给出可识别的 intent",
    "topic_missing": "topic 为空",
    "topic_is_question": "topic 回退成了整句问题（等于没抽主题）",
    "window_days_invalid": "window_days 非正整数或超出 10 年",
    "month_invalid": "month 不是合法的 YYYY-MM",
    "at_text_unresolved": "at_text 无法解析成时间窗",
    "before_unresolved": "before_text 无法解析成时间点",
    "agg_without_anchor": "聚合意图但既无 entities 也无 topic",
}


@dataclass(frozen=True)
class QueryPlan:
    """查询计划的最小契约；字段与 PARSE_PROMPT 的输出一一对应。"""

    intent: str
    topic: str
    category: str | None = None
    count_kind: str | None = None
    window_days: int | None = None
    month: str | None = None
    at_text: str | None = None
    before_text: str | None = None
    entities: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, d: dict) -> "QueryPlan":
        return cls(
            intent=str(d.get("intent") or "unknown"),
            topic=str(d.get("topic") or ""),
            category=d.get("category"),
            count_kind=d.get("count_kind"),
            window_days=d.get("window_days"),
            month=d.get("month"),
            at_text=d.get("at_text"),
            before_text=d.get("before_text"),
            entities=tuple(d.get("entities") or ()),
        )

    def to_dict(self) -> dict:
        """保持旧 dict 的键集合与类型（entities 仍是 list）。"""
        return {
            "intent": self.intent,
            "topic": self.topic,
            "category": self.category,
            "count_kind": self.count_kind,
            "window_days": self.window_days,
            "month": self.month,
            "at_text": self.at_text,
            "before_text": self.before_text,
            "entities": list(self.entities),
        }


def audit_plan(plan: dict | QueryPlan, question: str,
               qdate: datetime) -> list[str]:
    """静态检查计划，返回问题码列表（空列表 = 通过）。不产生任何副作用。"""
    from temporal import resolve_when_expr

    p = plan if isinstance(plan, QueryPlan) else QueryPlan.from_dict(plan)
    issues: list[str] = []

    if p.intent in ("unknown", "", None):
        issues.append("intent_unknown")
    if not p.topic.strip():
        issues.append("topic_missing")
    elif p.topic.strip() == str(question).strip():
        issues.append("topic_is_question")

    if p.window_days is not None:
        if not isinstance(p.window_days, int) or not 0 < p.window_days <= 3650:
            issues.append("window_days_invalid")

    if p.month and not _MONTH_RE.match(p.month):
        issues.append("month_invalid")

    if p.at_text and not resolve_when_expr(p.at_text, qdate):
        issues.append("at_text_unresolved")

    if p.before_text and not resolve_when_expr(p.before_text, qdate):
        issues.append("before_unresolved")

    if (p.intent in ("agg_count", "agg_sum")
            and not p.entities and not p.topic.strip()):
        issues.append("agg_without_anchor")

    return issues


def entities_not_in_question(plan: dict | QueryPlan, question: str) -> list[str]:
    """问题里没有出现的 entities —— 用于诊断假前提/上位词（非审计问题码）。"""
    p = plan if isinstance(plan, QueryPlan) else QueryPlan.from_dict(plan)
    low = str(question).lower()
    return [e for e in p.entities if str(e).lower() not in low]
