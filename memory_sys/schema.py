"""记忆数据模型：证据层 / 事件层 / 槽位层 + 确定性存储算子。

设计文档：docs/layer2-data-structure.md
核心原则（第一层确立）：
- LLM 只出现在入口（抽取）与出口（生成）；本模块内全部算子确定性。
- 事件五元组 m=(e,p,v,τ_v,τ_t)；演化只封 τ_v，更正 τ_v、τ_t 都封。
- 冲突判定是几何判定（cardinality + 区间重叠），不由 LLM 决定。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# 封闭集合：谓词表（cardinality 1=单值 2=多值）与 category 表
# 封闭是去重的前提——开放谓词会让 buy / purchase 无法合并。
# --------------------------------------------------------------------------

PREDICATE_CARD: dict[str, int] = {
    # —— 多值事件 ——
    "acquire": 2, "dispose": 2, "visit": 2, "attend": 2, "book": 2,
    "complete": 2, "start": 2, "finish": 2, "cancel": 2, "plan": 2,
    "receive": 2, "give": 2, "cook": 2, "bake": 2, "eat": 2,
    "watch": 2, "read": 2, "play": 2, "buy_for": 2, "repair": 2,
    # —— 状态（单值，可演化）——
    "live_in": 1, "work_at": 1, "work_as": 1, "study_at": 1,
    "have_pet": 2, "own": 2,
    # —— 偏好（多值）——
    "prefer": 2, "like": 2, "dislike": 2, "want": 2,
    # —— 数值/属性（单值）——
    "duration": 1, "score": 1, "age": 1, "birthday": 1,
}
DEFAULT_CARD = 2

CATEGORIES = [
    "plant", "animal", "clothing", "food", "book", "media", "money",
    "event", "activity", "place", "travel", "person", "device",
    "vehicle", "furniture", "tool", "sport", "game", "health",
    "work", "education", "recipe", "video", "budget", "other",
]

_STOP = {"a", "an", "the", "my", "some", "of", "for", "to", "and"}


def norm_value(v: str) -> str:
    """轻量值归一：去重合并的 key 之一。不做词干化，保持可回溯。"""
    v = unicodedata.normalize("NFKC", str(v)).strip().lower()
    v = re.sub(r"[^\w\s./$-]", "", v)
    words = [w for w in v.split() if w not in _STOP]
    out = " ".join(words)
    if out.endswith("ies") and len(out) > 4:
        out = out[:-3] + "y"                       # lilies -> lily
    elif out.endswith("s") and len(out) > 3 and not out.endswith(("ss", "us", "is")):
        out = out[:-1]                             # lugs -> lug；classes 保留
    return out


def norm_amount(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class Evidence:
    session_id: str
    turn_index: int
    role: str
    date: datetime
    content: str


@dataclass
class Event:
    subject: str
    predicate: str
    value: str
    category: str
    valid_from: datetime | None
    valid_to: datetime | None      # None = 开放（现仍有效）
    tx_seq: int
    status: str = "active"         # active | superseded | invalidated
    amount: float | None = None
    date_text: str = ""            # 原始时间表述（回显给出口 LLM）
    corrects: bool = False
    stated_at: datetime | None = None   # 陈述时间（会话时间戳），窗口过滤的兜底锚点
    evidence: list[tuple[str, int]] = field(default_factory=list)
    # ---- v2（fusion）字段：自由文本事实为载体，slot_id 为语义聚类键 ----
    fact: str = ""                      # 完整事实句子（数字/专名逐字保留）
    slot_id: int | None = None          # 槽位簇 id（写入期对齐时继承目标事件）

    def covers(self, t: datetime) -> bool:
        if self.valid_from and t < self.valid_from:
            return False
        if self.valid_to and t >= self.valid_to:
            return False
        return True


@dataclass
class Slot:
    """同一 (subject, predicate) 的版本链，按 valid_from 升序。"""
    subject: str
    predicate: str
    cardinality: int
    events: list[Event] = field(default_factory=list)

    def current(self) -> Event | None:
        """Current(e,p)：取 valid_to 右端最大者（第一层算子）。"""
        act = [e for e in self.events if e.status == "active"]
        if not act:
            return None
        return max(act, key=lambda e: (e.valid_to or datetime.max, e.tx_seq))

    def value_at(self, t: datetime) -> Event | None:
        """Value(e,p,t_v,now)：时刻状态，与 current 是不同算子。"""
        hits = [e for e in self.events if e.covers(t)]
        return hits[-1] if hits else None


# --------------------------------------------------------------------------
# 存储与确定性算子
# --------------------------------------------------------------------------

class MemoryStore:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.evidence: list[Evidence] = []
        self.tx_seq: int = 0
        self._by_key: dict[tuple, Event] = {}
        self.stats = {"extracted": 0, "merged": 0, "superseded": 0,
                      "invalidated": 0, "added": 0}

    def add_evidence(self, session_id: str, date: datetime,
                     turns: list[dict]) -> None:
        for i, t in enumerate(turns):
            self.evidence.append(Evidence(session_id, i, t["role"],
                                          date, t["content"]))

    # ---------- 写入：去重 + 更新语义 ----------

    def add_event(self, subject: str, predicate: str, value: str,
                  category: str, valid_from: datetime | None,
                  valid_to: datetime | None, corrects: bool,
                  amount: float | None, date_text: str,
                  ev_ptr: tuple[str, int],
                  stated_at: datetime | None = None) -> Event:
        self.tx_seq += 1
        subject = (subject or "user").strip().lower() or "user"
        predicate = (predicate or "misc").strip().lower()
        category = (category or "other").strip().lower()
        value = (value or "").strip()
        amount = norm_amount(amount)

        key = (subject, predicate, norm_value(value))
        # 1) 去重：同 (subject, predicate, value) 的 active 事件 → 合并出处
        hit = self._by_key.get(key)
        if hit and hit.status == "active":
            hit.evidence.append(ev_ptr)
            if amount is not None and hit.amount is None:
                hit.amount = amount
            self.stats["merged"] += 1
            return hit

        # 2) 更新语义（仅单值谓词做几何冲突判定）
        card = PREDICATE_CARD.get(predicate, DEFAULT_CARD)
        if card == 1:
            for m in self.events:
                if (m.status == "active" and m.subject == subject
                        and m.predicate == predicate):
                    if corrects:
                        m.status = "invalidated"          # 更正：旧值从未为真
                        self.stats["invalidated"] += 1
                    else:
                        m.status = "superseded"           # 演化：旧值曾为真
                        if valid_from and (m.valid_to is None):
                            m.valid_to = valid_from       # 只封 τ_v
                        self.stats["superseded"] += 1
                    break

        ev = Event(subject, predicate, value, category, valid_from,
                   valid_to, self.tx_seq, "active", amount, date_text,
                   corrects, stated_at, [ev_ptr])
        self.events.append(ev)
        self._by_key[key] = ev
        self.stats["added"] += 1
        return ev

    # ---------- 查询算子 ----------

    def active_events(self, category: str | None = None,
                      predicate: str | None = None,
                      window: tuple[datetime, datetime] | None = None,
                      subject: str | None = None) -> list[Event]:
        out = []
        for e in self.events:
            if e.status != "active":
                continue
            if category and e.category != category:
                continue
            if predicate and e.predicate != predicate:
                continue
            if subject and e.subject != subject:
                continue
            if window:
                anchor = e.valid_from or e.stated_at
                if anchor is None or not (window[0] <= anchor <= window[1]):
                    continue
            out.append(e)
        out.sort(key=lambda e: (e.valid_from or datetime.min, e.tx_seq))
        return out

    def slot(self, subject: str, predicate: str) -> Slot:
        s = Slot(subject, predicate, PREDICATE_CARD.get(predicate, DEFAULT_CARD))
        s.events = [e for e in self.events
                    if e.subject == subject and e.predicate == predicate]
        s.events.sort(key=lambda e: (e.valid_from or datetime.min, e.tx_seq))
        return s

    def mentions_any(self, words: list[str]) -> bool:
        """拒答检查：问题中的实体词是否在记忆库任何位置出现过。"""
        hay = " ".join([e.value + " " + e.subject + " " + e.category
                        for e in self.events]).lower()
        return any(w.lower() in hay for w in words if len(w) > 2)

    def all_events(self) -> list[Event]:
        return list(self.events)
