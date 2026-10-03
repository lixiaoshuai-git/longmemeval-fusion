"""结构化记忆策略：写入（抽取→归一→去重）与查询（解析→确定性执行）。

接入 memory.py 的 MemorySystem 接口。上下文构成：
  1. [DETERMINISTIC QUERY RESULT]  程序算出的聚合/版本链（附"以此为准"指令）
  2. [ABSTENTION CHECK]            实体存在性校验的通知（针对 _abs）
  3. [EVENT MEMORY]                全部 active 事件（紧凑清单）
  4. [USER STATEMENTS]             用户发言原文（证据地板，约 10.6% token）
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from llm_io import EXTRACTION_PROMPT, PARSE_PROMPT, extract_json_array, extract_json_object
from schema import CATEGORIES, PREDICATE_CARD, MemoryStore, norm_value

_INTENTS = {"agg_count", "agg_sum", "fact_current", "fact_at_time",
            "list_events", "unknown"}


# --------------------------------------------------------------------------
# 结构化查询
# --------------------------------------------------------------------------

@dataclass
class StructuredQuery:
    intent: str = "unknown"
    subject: str | None = None       # user | assistant（fact 类查询的目标）
    category: str | None = None
    predicate: str | None = None
    window_days: int | None = None
    month: str | None = None
    before_text: str | None = None
    at_text: str | None = None
    entities: list[str] = field(default_factory=list)


def _clean(v, allowed=None):
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.lower() in ("null", "none", "n/a", ""):
        return None
    if allowed and v not in allowed:
        return None
    return v


def parse_question(client, question: str, qdate: datetime) -> StructuredQuery:
    prompt = PARSE_PROMPT.format(cats=CATEGORIES, preds=sorted(PREDICATE_CARD),
                                 date=qdate.strftime("%Y-%m-%d"),
                                 question=question)
    raw = client.chat(prompt, max_tokens=300)
    obj = extract_json_object(raw) or {}
    subj = _clean(obj.get("subject"))
    if subj not in ("user", "assistant"):
        subj = "user"
    sq = StructuredQuery(
        intent=_clean(obj.get("intent"), _INTENTS) or "unknown",
        subject=subj,
        category=_clean(obj.get("category"), set(CATEGORIES)),
        predicate=_clean(obj.get("predicate"), set(PREDICATE_CARD)),
        window_days=obj.get("window_days") if isinstance(obj.get("window_days"), int) else None,
        month=_clean(obj.get("month")),
        before_text=_clean(obj.get("before_text")),
        at_text=_clean(obj.get("at_text")),
        entities=[str(x) for x in obj.get("entities", []) if str(x).strip()][:6],
    )
    return sq


# --------------------------------------------------------------------------
# 时间与窗口
# --------------------------------------------------------------------------

def resolve_window(sq: StructuredQuery, qdate: datetime):
    if sq.month:
        m = datetime.strptime(sq.month, "%Y-%m")
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (m, nxt - timedelta(seconds=1))
    if sq.window_days:
        return (qdate - timedelta(days=sq.window_days), qdate)
    return None


def resolve_before(store: MemoryStore, sq: StructuredQuery) -> datetime | None:
    if not sq.before_text:
        return None
    key = norm_value(sq.before_text)
    best = None
    for e in store.all_events():
        nv = norm_value(e.value)
        if key and (key in nv or nv in key):
            anchor = e.valid_from or e.stated_at
            if anchor and (best is None or anchor < best):
                best = anchor
    return best


def parse_when(when: str, sdate: datetime):
    """抽取产出的 when → (valid_from, valid_to)。带合法性校验。"""
    when = (when or "unknown").strip()
    if when in ("", "unknown", "null", "None"):
        return None, None
    m = re.match(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$", when)
    if m:
        try:
            a = datetime.strptime(m.group(1), "%Y-%m-%d")
            b = datetime.strptime(m.group(2), "%Y-%m-%d")
            if b >= a:
                return a, b + timedelta(days=1)
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", when)
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
    try:
        dt = datetime(y, mo, d)
    except ValueError:
        return None, None
    # 合法性：陈述时间 ±400 天之外的"绝对日期"视为抽取错误，回退为锚定区间
    if not (sdate - timedelta(days=400) <= dt <= sdate + timedelta(days=60)):
        return None, None
    if m.group(3):
        return dt, dt + timedelta(days=1)
    return dt, dt + timedelta(days=1)   # 只有年月 → 视为当月起


# --------------------------------------------------------------------------
# 执行与渲染
# --------------------------------------------------------------------------

def _fmt_d(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "?"


def execute(store: MemoryStore, sq: StructuredQuery,
            qdate: datetime) -> tuple[str, dict]:
    meta = {"intent": sq.intent, "matched": 0}
    lines: list[str] = []

    window = resolve_window(sq, qdate)
    if sq.before_text:
        b = resolve_before(store, sq)
        if b:
            window = (datetime.min, b)

    if sq.intent in ("agg_count", "agg_sum", "list_events"):
        evs = store.active_events(category=sq.category, predicate=sq.predicate,
                                  window=window)
        meta["matched"] = len(evs)
        wtxt = f"{_fmt_d(window[0])}..{_fmt_d(window[1])}" if window else "all time"
        lines.append(f"Query: {sq.intent}  category={sq.category or '-'}  "
                     f"predicate={sq.predicate or '-'}  window={wtxt}")
        if evs:
            lines.append("Matching events (already deduplicated; each listed once):")
            total = 0.0
            has_amount = False
            for e in evs:
                src = ",".join(f"{s}#{t}" for s, t in e.evidence[:3])
                amt = f" amount={e.amount:g}" if e.amount is not None else ""
                lines.append(f"- {_fmt_d(e.valid_from or e.stated_at)} | "
                             f"{e.subject}.{e.predicate}: {e.value}{amt} | src={src}")
                if e.amount is not None:
                    total += e.amount
                    has_amount = True
            if sq.intent == "agg_count":
                lines.append(f"COMPUTED COUNT = {len(evs)}")
                lines.append("(this count is computed deterministically from the "
                             "deduplicated event list; trust it over manual recounting)")
            elif sq.intent == "agg_sum" and has_amount:
                lines.append(f"COMPUTED TOTAL = {total:g}")
                lines.append("(sum computed deterministically; if some events lack "
                             "amounts, say which ones are missing rather than guessing)")
        else:
            lines.append("No matching events in the memory for this query.")
        meta["computed"] = (len(evs) if sq.intent == "agg_count"
                            else (round(total, 2) if sq.intent == "agg_sum" else None))

    elif sq.intent in ("fact_current", "fact_at_time"):
        slot = store.slot(sq.subject or "user", sq.predicate or "")
        cur = slot.current()
        meta["matched"] = len(slot.events)
        lines.append(f"Query: {sq.intent}  slot={sq.subject or 'user'}.{sq.predicate or '-'}")
        if slot.events:
            lines.append("Version chain (oldest -> newest):")
            for e in slot.events:
                rng = (f"[{_fmt_d(e.valid_from)} .. "
                       f"{_fmt_d(e.valid_to) or 'now'}]")
                lines.append(f"- {rng} {e.value} ({e.status})")
            if cur:
                lines.append(f"CURRENT VALUE: {cur.value} (effective {_fmt_d(cur.valid_from)})")
        else:
            lines.append("No events recorded for this slot.")

    if sq.intent == "unknown" or not lines:
        evs = store.active_events()
        meta["matched"] = len(evs)
        lines.append("No specific query structure matched; full event memory follows.")

    return "\n".join(lines), meta


# --------------------------------------------------------------------------
# 策略接入
# --------------------------------------------------------------------------

class StructuredMemory:
    """实现 memory.py 的 MemorySystem 协议（duck typing）。"""

    name = "structured"

    def __init__(self, client, cache: dict | None = None,
                 cache_path: str | None = None, **_):
        self.client = client
        self.cache = cache if cache is not None else {}
        self.cache_path = cache_path
        self.store = MemoryStore()
        self._sess_n = 0
        self._lock = threading.Lock()
        self._dirty = False
        self.last_meta: dict = {}

    # -- 生命周期 --

    def reset(self) -> None:
        self.store = MemoryStore()
        self._sess_n = 0

    def save_cache(self) -> None:
        if self.cache_path and self._dirty:
            with self._lock:
                p = pathlib.Path(self.cache_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(self.cache, ensure_ascii=False),
                             encoding="utf-8")
                self._dirty = False

    def stats(self) -> dict:
        return {"events": len(self.store.events),
                "evidence": len(self.store.evidence),
                **self.store.stats}

    # -- 写入期 --

    def write(self, session: list[dict], session_date: str) -> None:
        from lme_core import parse_dt
        self._sess_n += 1
        sid = f"s{self._sess_n}"
        sdate = parse_dt(session_date) or datetime(2023, 1, 1)

        self.store.add_evidence(sid, sdate, session)

        blob = json.dumps(session, ensure_ascii=False)
        key = hashlib.sha1((session_date + blob).encode("utf-8")).hexdigest()[:16]
        with self._lock:
            raw = self.cache.get(key)
        if raw is None:
            sess_text = "\n".join(f"{t['role']}: {t['content']}"
                                  for t in session)
            prompt = EXTRACTION_PROMPT.format(
                cats="|".join(CATEGORIES),
                preds="|".join(sorted(PREDICATE_CARD)),
                date=sdate.strftime("%Y-%m-%d"), session=sess_text)
            resp = self.client.chat(prompt, max_tokens=1600)
            raw = extract_json_array(resp) or []
            with self._lock:
                self.cache[key] = raw
                self._dirty = True

        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            vf, vt = parse_when(str(item.get("when", "unknown")), sdate)
            ptr = (sid, int(item.get("turn", i) or i))
            ev = self.store.add_event(
                subject=str(item.get("subject", "user")),
                predicate=str(item.get("predicate", "misc")),
                value=str(item.get("value", "")),
                category=str(item.get("category", "other")),
                valid_from=vf, valid_to=vt,
                corrects=bool(item.get("corrects")),
                amount=item.get("amount"),
                date_text=str(item.get("when", "")),
                ev_ptr=ptr, stated_at=sdate)
            self.store.stats["extracted"] += 1

    # -- 查询期 --

    def read(self, question: str, question_date: str) -> str:
        from lme_core import parse_dt
        qdate = parse_dt(question_date) or datetime(2023, 6, 1)

        sq = parse_question(self.client, question, qdate)
        result, meta = execute(self.store, sq, qdate)
        self.last_meta = meta

        parts: list[str] = []
        if sq.intent != "unknown":
            parts.append("[DETERMINISTIC QUERY RESULT]\n" + result)

        # 拒答不加硬性提示：误导向比漏报危害大（v1 曾用词面匹配导致误拒答）。
        # 出口 LLM 依据下方 [USER STATEMENTS] 全文自行判断前提是否存在。

        evs = self.store.active_events()
        if evs:
            lines = ["[EVENT MEMORY] all active events, chronological:"]
            for e in evs[:60]:
                amt = f" amount={e.amount:g}" if e.amount is not None else ""
                lines.append(f"- {_fmt_d(e.valid_from or e.stated_at)} | "
                             f"{e.subject}.{e.predicate}: {e.value}"
                             f" | cat={e.category}{amt}")
            if len(evs) > 60:
                lines.append(f"- ... ({len(evs) - 60} more)")
            parts.append("\n".join(lines))

        user_turns = [t for t in self.store.evidence if t.role == "user"]
        if user_turns:
            lines = ["[USER STATEMENTS] verbatim user turns, chronological "
                     "(floor evidence; assistant restatements are NOT repeated here):"]
            for t in user_turns:
                c = t.content.strip().replace("\n", " ")
                if len(c) > 220:
                    c = c[:220] + "..."
                lines.append(f"- {_fmt_d(t.date)} user: {c}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)
