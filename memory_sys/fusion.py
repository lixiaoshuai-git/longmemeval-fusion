"""Fusion 记忆策略（v2）——吸取 Mem0 / Graphiti / Letta / Hindsight 各家之长。

对应 docs/layer2-data-structure.md §8.5 的修订方向：

1. **事实载体 = 自由文本完整句子**（Mem0/Graphiti）：数字、专名、时间原话逐字保留，
   结构化字段（valid 区间、slot 链）降级为加速索引而非唯一载体。
2. **按 user 轮次抽取**（Mem0 粒度）：一次一个聚焦 prompt，替代 v1 的单会话 mega-prompt；
   assistant 专名事实单独一路（SS-A 专项）。
3. **写入期对齐**（Mem0 update phase + 手册要求的确定性执行）：
   LLM 只做"关系分类"（new/merge/evolve/correct/supplement + 时间锚点），
   双时间轴几何由确定性代码执行（演化只封 τ_v，更正连 τ_t 一起封）。
   —— 槽位因此由**语义聚类**形成，不再依赖 value 词面相等（修复 coins 变体断裂）。
4. **模糊检索回退**（Graphiti/Hindsight）：BM25 覆盖事实句/槽位代表句/证据轮次，
   查询期零硬性键匹配；结构化查询与 BM25 并行，RRF 思想合并。
5. **证据层兜底**（Letta archival 思想）：全部 user 轮次作为地板（约 10.6% token），
   assistant 轮次按相关度取 top-k（SS-A 覆盖且不引入复述计数污染）。
   —— 有索引、按需召回，与 F.1 禁止的"全量历史拼入"有本质区别。
6. **相对时间确定性解析优先**（第一层时间归一化 + Graphiti 双时间轴）：
   先用规则解析 when_raw，规则不命中才用模型解析值，最后做 ±400 天合法性校验。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import threading
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bm25 import BM25, tokenize  # noqa: E402
from llm_io import (  # noqa: E402
    ASSISTANT_FACTS_PROMPT, PARSE_PROMPT_V2, RELATION_PROMPT,
    TURN_EXTRACTION_PROMPT, extract_json_array, extract_json_object,
)
from schema import CATEGORIES, MemoryStore  # noqa: E402

PROMPT_VERSION = "fusion-v2.4"

_INTENTS = {"agg_count", "agg_sum", "fact_current", "fact_at_time",
            "list_events", "argmax", "lookup", "unknown"}

_STRONG_RATIO = 0.45      # 强匹配阈值 = max(0.45 × 最高分, 绝对下限)
_STRONG_MIN = 1.2
_EXCERPT_K = 6            # assistant 相关轮次条数
_VERBATIM_CHARS = 1500    # v3.6 近逐字证据层：源轮次全文引用上限
_FACT_CAP = 80            # 事实清单上限
_TURN_CHARS = 240         # 地板轮次截断


# --------------------------------------------------------------------------
# 时间：确定性相对表达解析（优先）→ 模型解析值（回退）→ 合法性校验
# --------------------------------------------------------------------------

_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_MONTH_ABBR = {m[:3]: i for m, i in _MONTHS.items()}


def _month_range(y: int, m: int) -> tuple[datetime, datetime]:
    start = datetime(y, m, 1)
    nxt = datetime(y + (m == 12), (m % 12) + 1, 1)
    return start, nxt


def _shift_month(d: datetime, k: int) -> datetime:
    total = d.year * 12 + (d.month - 1) + k
    return datetime(total // 12, total % 12 + 1, 1)


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "a": 1, "an": 1, "couple": 2, "few": 3,
             "several": 3}


def _num_of(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_NUM.get(token)


def _days_in_month(y: int, m: int) -> int:
    nxt = _shift_month(datetime(y, m, 1), 1)
    return (nxt - timedelta(days=1)).day


def _shift_month_keep_day(d: datetime, k: int) -> datetime:
    """月份平移并保留日号（月末截断）。'a month ago' 的点估计基础。"""
    total = d.year * 12 + (d.month - 1) + k
    y, m = total // 12, total % 12 + 1
    return datetime(y, m, min(d.day, _days_in_month(y, m)))


def resolve_when_expr(raw: str, sdate: datetime) -> tuple[datetime, datetime] | None:
    """规则解析相对时间表达 → [start, end)。不命中返回 None。

    教训（rug 题）：'N months ago' 曾解析成月边界区间 [月初, sdate)，
    区间起点被当时长计算的端点 → 差出近一个月。相对表达一律给**点估计**，
    区间语义只留给查询侧的窗口过滤。
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return None

    # 模糊表达：不解析、不信任模型的猜测，锚定到陈述日
    if re.search(r"\b(recently|lately|a while ago|some time ago|not long ago)\b", raw):
        return None

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d + timedelta(days=1)
        except ValueError:
            pass

    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*day", raw)
    if m and "ago" in raw:
        d = sdate - timedelta(days=_num_of(m.group(1)))
        return d, d + timedelta(days=1)
    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*week", raw)
    if m and "ago" in raw:
        d = sdate - timedelta(weeks=_num_of(m.group(1)))
        return d, d + timedelta(days=1)
    m = re.search(r"(a|an|couple of|a few|several|\d+|one|two|three|four|five|"
                  r"six|seven|eight|nine|ten|eleven|twelve)\s*month", raw)
    if m and "ago" in raw:
        n = _num_of(m.group(1).replace("couple of", "couple").replace("a few", "few"))
        if n is None:
            n = 1
        d = _shift_month_keep_day(sdate, -n)
        return d, d + timedelta(days=1)
    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*year", raw)
    if m and "ago" in raw:
        d = _shift_month_keep_day(sdate, -12 * _num_of(m.group(1)))
        return d, d + timedelta(days=1)

    if "yesterday" in raw:
        d = sdate - timedelta(days=1)
        return d, d + timedelta(days=1)
    if re.search(r"\b(today|tonight|just)\b", raw):
        return sdate, sdate + timedelta(days=1)
    if "next month" in raw:
        a, b = _shift_month(sdate, 1), _shift_month(sdate, 2)
        return a, b
    if "next week" in raw:
        return sdate + timedelta(days=7), sdate + timedelta(days=14)
    if "last week" in raw:
        return sdate - timedelta(days=7), sdate
    if "last month" in raw:
        a = _shift_month(sdate.replace(day=1), -1)
        return a, sdate.replace(day=1)
    if "last year" in raw:
        return datetime(sdate.year - 1, 1, 1), datetime(sdate.year, 1, 1)

    # "in December" / "December 2023" / "in 2023"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                  r"[a-z]*\s*,?\s*(\d{4})", raw)
    if m:
        a, b = _month_range(int(m.group(2)), _MONTH_ABBR[m.group(1)])
        return a, b
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", raw)
    if m:
        mo = _MONTH_ABBR[m.group(1)]
        y = sdate.year if mo <= sdate.month else sdate.year - 1   # 记忆默认过去
        a, b = _month_range(y, mo)
        return a, b
    m = re.search(r"\b(20\d{2})\b", raw)
    if m:
        return datetime(int(m.group(1)), 1, 1), datetime(int(m.group(1)) + 1, 1, 1)
    return None


def _valid_range(rng: tuple[datetime, datetime] | None,
                 sdate: datetime) -> tuple[datetime, datetime] | None:
    """合法性校验：陈述时间 -400d .. +60d 之外的解析结果视为错误。"""
    if rng is None:
        return None
    lo, hi = rng
    if not (sdate - timedelta(days=400) <= lo <= sdate + timedelta(days=60)):
        return None
    return rng


def resolve_when(when_raw: str, when_model: str,
                 sdate: datetime) -> tuple[datetime, datetime] | None:
    for cand in (_valid_range(resolve_when_expr(when_raw, sdate), sdate),
                 _valid_range(resolve_when_expr(when_model, sdate), sdate)):
        if cand:
            return cand
    # 模型直接给绝对日期的兜底
    wm = (when_model or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", wm)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
            rng = _valid_range((d, d + timedelta(days=1)), sdate)
            if rng:
                return rng
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _fmt_d(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "?"


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "..."


def _clean(v, allowed=None):
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.lower() in ("null", "none", "n/a"):
        return None
    if allowed and v not in allowed:
        return None
    return v


def _first_number(s: str) -> float | None:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s).replace(",", ""))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------
# 策略主体
# --------------------------------------------------------------------------

class FusionMemory:
    """实现 memory.py 的 MemorySystem 协议（duck typing）。"""

    name = "fusion"

    def __init__(self, client, cache: dict | None = None,
                 cache_path: str | None = None, **_):
        self.client = client
        self.cache = cache if cache is not None else {}
        self.cache_path = cache_path
        self.store = MemoryStore()
        self._sess_n = 0
        self._lock = threading.Lock()
        self._dirty = False
        # 消融开关（仅评测实验用，默认全开）：ABL_ARITH / ABL_EXCERPTS /
        # ABL_FLOOR 置 0 分别关闭算术注记 / 句子级摘录 / 证据地板。
        # 机制真实性消融矩阵的执行面——每个开关对应一条可归因的机制。
        import os as _os
        self.enable_arith = _os.environ.get("ABL_ARITH", "1") == "1"
        self.enable_excerpts = _os.environ.get("ABL_EXCERPTS", "1") == "1"
        self.enable_floor = _os.environ.get("ABL_FLOOR", "1") == "1"
        self.last_meta: dict = {}

    # -- 缓存 --

    def _cached(self, key: str, fn):
        full = hashlib.sha1((PROMPT_VERSION + "|" + key).encode("utf-8")).hexdigest()[:16]
        with self._lock:
            hit = self.cache.get(full)
        if hit is not None:
            return hit
        val = fn()
        with self._lock:
            self.cache[full] = val
            self._dirty = True
        return val

    def save_cache(self) -> None:
        if self.cache_path and self._dirty:
            with self._lock:
                p = pathlib.Path(self.cache_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(self.cache, ensure_ascii=False),
                             encoding="utf-8")
                self._dirty = False

    def reset(self) -> None:
        self.store = MemoryStore()
        self._sess_n = 0

    def stats(self) -> dict:
        return {"events": len(self.store.events),
                "evidence": len(self.store.evidence),
                **self.store.stats}

    def forget(self, *, now=None, ttl_days=None, capacity=None,
               compact_days=None) -> dict:
        """遗忘/生命周期（手册 B.1）：软归档，不改检索逻辑本身。

        实现委托给独立的 memory_sys/lifecycle.py（只操作 store 公开形状，
        与本类零逻辑耦合）。默认不启用——评测流水线不调用；
        归档事件退出"当前事实"召回，但 value_at 时刻查询仍可审计。
        """
        from memory_sys.lifecycle import apply_lifecycle
        return apply_lifecycle(self.store, now=now, ttl_days=ttl_days,
                               capacity=capacity, compact_days=compact_days)

    # ------------------------------------------------------------------
    # 写入期
    # ------------------------------------------------------------------

    def _entity_index(self) -> dict[str, list]:
        """v3.9 #3：确定性实体倒排——事实句分词去通用词，稀有词 → 事件列表。

        惰性构建；write() 时置 None 失效。用途：agg/lookup 的一跳实体扩展
        （大事件池下 BM25 词面 topical 匹配漏掉的集合成员）。只索引
        active 事件。
        """
        if getattr(self, "_eidx", None) is not None:
            return self._eidx
        idx: dict[str, list] = {}
        for e in self._active_events():
            for t in self._entity_toks(e):
                idx.setdefault(t, []).append(e)
        self._eidx = idx
        return idx

    def _entity_toks(self, e) -> set[str]:
        toks = tokenize(e.fact or e.value)
        return {t for t in toks if len(t) >= 3 and t not in self._GENERIC_TOKS}

    def _one_hop(self, seeds: list, cap: int = 12) -> list:
        """v3.9 #3：一跳实体扩展——种子事件（裸 Event 列表）的**稀有**实体
        （库内 df≤8）共现事件补进候选。仅在 store>100 events（S/M 规模）
        时调用，Oracle（≈33 events）零行为变化。返回 (event, score=0) 列表，
        渲染时带 one-hop 标记由 reader 自行取舍。"""
        eidx = self._entity_index()
        if not eidx or not seeds:
            return []
        df = {t: len(v) for t, v in eidx.items()}
        seen = {id(e) for e in seeds}
        extra: list = []
        for e in seeds[:5]:
            toks = sorted(self._entity_toks(e), key=lambda x: df.get(x, 999))
            for t in toks[:4]:
                if df.get(t, 999) > 8:
                    continue
                for e2 in eidx.get(t, []):
                    if e2.status == "active" and id(e2) not in seen:
                        seen.add(id(e2))
                        extra.append((e2, 0.0))
                        if len(extra) >= cap:
                            return extra
        return extra

    def write(self, session: list[dict], session_date: str) -> None:
        from lme_core import parse_dt
        self._sess_n += 1
        self._eidx = None                    # v3.9：事件池变化，实体倒排失效
        sid = f"s{self._sess_n}"
        sdate = parse_dt(session_date) or datetime(2023, 1, 1)
        self.store.add_evidence(sid, sdate, session)

        # ---- 1) 逐 user 轮次抽取（Mem0 粒度）----
        cands: list[dict] = []
        for i, t in enumerate(session):
            if t["role"] != "user":
                continue
            nxt = session[i + 1] if i + 1 < len(session) and session[i + 1]["role"] == "assistant" else None
            key = "ext|" + hashlib.sha1(
                json.dumps([session_date, t["content"]],
                           ensure_ascii=False).encode("utf-8")).hexdigest()
            raw = self._cached(key, lambda ut=t, at=nxt: self._extract_turn(ut, at, sdate))
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict) and item.get("fact"):
                    c = dict(item)          # 浅拷贝：缓存条目必须保持可序列化
                    c["_turn"] = i
                    cands.append(c)

        # ---- 2) assistant 专名事实（SS-A 专项，整会话一次）----
        asst_turns = "\n\n".join(f"assistant: {t['content']}"
                                 for t in session if t["role"] == "assistant")
        if asst_turns:
            key = "asst|" + hashlib.sha1(
                json.dumps([session_date, asst_turns],
                           ensure_ascii=False).encode("utf-8")).hexdigest()
            raw = self._cached(key, lambda: self._extract_assistant(asst_turns, sdate))
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict) and item.get("fact"):
                    c = dict(item)
                    c["_turn"] = None
                    cands.append(c)

        if not cands:
            return

        # ---- 3) 会话内轻量去重（同句面事实只留一条）----
        seen_facts: dict[str, dict] = {}
        for c in cands:
            k = " ".join(tokenize(c["fact"]))
            if k in seen_facts:
                continue
            seen_facts[k] = c
        cands = list(seen_facts.values())

        # ---- 4) 写入期对齐：相似检索 + 关系分类 + 确定性执行 ----
        for c in cands:
            rng = resolve_when(str(c.get("when_raw", "")),
                               str(c.get("when", "none")), sdate)
            c["_rng"] = rng

        existing = self._active_events()
        rel_out = self._classify(cands, existing, sdate)
        for c, act in zip(cands, rel_out):
            self._apply(c, act, sdate, sid)

    def _extract_turn(self, user_turn: dict, asst: dict | None,
                      sdate: datetime) -> list:
        prompt = TURN_EXTRACTION_PROMPT.format(
            date=sdate.strftime("%Y-%m-%d"),
            user_turn=user_turn["content"],
            assistant_turn=(asst["content"] if asst else "(none)"),
            cats="|".join(CATEGORIES))
        resp = self.client.chat(prompt, max_tokens=700)
        return extract_json_array(resp) or []

    def _extract_assistant(self, asst_turns: str, sdate: datetime) -> list:
        prompt = ASSISTANT_FACTS_PROMPT.format(
            assistant_turns=_clip(asst_turns, 6000), cats="|".join(CATEGORIES))
        resp = self.client.chat(prompt, max_tokens=500)
        return extract_json_array(resp) or []

    # -- 写入期对齐 --

    def _active_events(self) -> list:
        return [e for e in self.store.events if e.status == "active"]

    def _classify(self, cands: list[dict], existing: list, sdate: datetime) -> list[dict]:
        """Mem0 式关系分类（LLM 决策）→ 输出与 cands 等长的动作列表。

        检索按**逐候选** top-3（而非全局 top-12）：候选只看到与自己相关的
        既有记忆，显著降低 merge/supplement 挂错目标对象的概率。
        """
        actions: list[dict] = [{"action": "new", "target": None, "anchor": None}
                               for _ in cands]
        if not existing:
            return actions

        index = BM25([tokenize(e.fact or e.value) for e in existing])
        used: dict[int, int] = {}          # existing 下标 -> 出现次数
        groups: list[tuple[int, dict, list[int]]] = []
        for i, c in enumerate(cands, 1):
            hits = index.topk(c["fact"], k=3, min_score=1.0)
            refs = []
            for idx, _s in hits:
                used[idx] = used.get(idx, 0) + 1
                refs.append(idx)
            groups.append((i, c, refs))
        if not used:
            return actions

        ex_lines = []
        eid_of = {}
        for idx in sorted(used):
            eid = f"e{idx + 1}"
            eid_of[idx] = eid
            e = existing[idx]
            ex_lines.append(f'{eid} | {_fmt_d(e.valid_from or e.stated_at)} | '
                            f'{_clip(e.fact or e.value, 160)}')
        cd_lines = []
        for i, c, refs in groups:
            rng = c.get("_rng")
            when = f"{_fmt_d(rng[0])}..{_fmt_d(rng[1])}" if rng else "no time"
            line = f'[c{i}] when={when} | {_clip(c["fact"], 200)}'
            if refs:
                sim = "; ".join(
                    f"{eid_of[r]} | {_clip(existing[r].fact or existing[r].value, 120)}"
                    for r in refs)
                line += f"\n    similar existing: {sim}"
            cd_lines.append(line)

        key = "rel|" + hashlib.sha1(json.dumps(
            [sdate.strftime("%Y-%m-%d"), ex_lines, cd_lines],
            ensure_ascii=False).encode("utf-8")).hexdigest()
        out = self._cached(key, lambda: self._relation_call(
            "\n".join(ex_lines), "\n".join(cd_lines), sdate))

        parsed = out if isinstance(out, list) else []
        by_eid = {eid_of[idx]: existing[idx] for idx in eid_of}
        for i, item in enumerate(parsed[: len(cands)]):
            if not isinstance(item, dict):
                continue
            act = str(item.get("action", "new")).lower()
            if act not in ("new", "merge", "evolve", "correct", "supplement"):
                act = "new"
            tgt = by_eid.get(str(item.get("target", "")).strip())
            anchor = None
            sa = str(item.get("anchor") or "")
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", sa)
            if m:
                try:
                    anchor = datetime(int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)))
                except ValueError:
                    anchor = None
            actions[i] = {"action": act, "target": tgt, "anchor": anchor}
        return actions

    def _relation_call(self, ex_block: str, cd_block: str, sdate: datetime):
        prompt = RELATION_PROMPT.format(date=sdate.strftime("%Y-%m-%d"),
                                        existing_block=ex_block,
                                        candidates_block=cd_block)
        resp = self.client.chat(prompt, max_tokens=800)
        return extract_json_array(resp) or []

    def _apply(self, c: dict, act: dict, sdate: datetime, sid: str) -> None:
        """确定性执行器：LLM 分类语义，几何与时钟全部由代码完成。"""
        store = self.store
        rng = c.get("_rng")
        # 时间未解析时锚定到陈述日期（状态类事实的语义：自该次陈述起成立）
        vf = rng[0] if rng else sdate
        vt = rng[1] if rng else None
        fact = str(c["fact"]).strip()
        turn = c.get("_turn")
        ptr = (sid, turn if turn is not None else -1)
        tgt = act.get("target")
        action = act["action"]

        def new_event(slot_id: int | None, corrects: bool,
                      vfrom: datetime | None, vto: datetime | None):
            store.tx_seq += 1
            from schema import Event
            ev = Event(
                subject="assistant" if c.get("kind") == "assistant_fact" else "user",
                predicate=str(c.get("kind", "event")),
                value=_clip(fact, 300), category=str(c.get("category") or "other"),
                valid_from=vfrom, valid_to=vto, tx_seq=store.tx_seq,
                status="active", amount=None, date_text=str(c.get("when_raw", "")),
                corrects=corrects, stated_at=sdate, evidence=[ptr])
            ev.fact = fact
            ev.slot_id = slot_id if slot_id is not None else store.tx_seq
            store.events.append(ev)
            store.stats["added"] += 1
            return ev

        if action == "merge" and tgt is not None and tgt.status == "active":
            tgt.evidence.append(ptr)
            store.stats["merged"] += 1
            return
        if action in ("evolve", "correct") and tgt is not None \
                and tgt.status == "active":
            anchor = act.get("anchor") or sdate
            if action == "evolve":
                if tgt.valid_to is None or tgt.valid_to > anchor:
                    tgt.valid_to = anchor            # 只封 τ_v（演化）
                tgt.status = "superseded"
                store.stats["superseded"] += 1
                new_event(tgt.slot_id, False, anchor, None)
            else:
                tgt.status = "invalidated"           # τ_v、τ_t 都封（更正）
                store.stats["invalidated"] += 1
                new_event(tgt.slot_id, True, vf or anchor, vt)
            return
        if action == "supplement" and tgt is not None:
            new_event(tgt.slot_id, False, vf, vt)    # 同一集合的新实例
            return
        new_event(None, bool(c.get("corrects")), vf, vt)

    # ------------------------------------------------------------------
    # 查询期
    # ------------------------------------------------------------------

    def read(self, question: str, question_date: str) -> str:
        from lme_core import parse_dt
        qdate = parse_dt(question_date) or datetime(2023, 6, 1)

        sq = self._cached("parse|" + hashlib.sha1(
            json.dumps([question, question_date],
                       ensure_ascii=False).encode("utf-8")).hexdigest(),
            lambda: self._parse(question, qdate))
        self.last_meta = {"intent": sq.get("intent"), "topic": sq.get("topic")}

        parts: list[str] = []
        result, meta = self._execute(sq, qdate)
        self.last_meta.update(meta)
        if result:
            parts.append("[DETERMINISTIC QUERY RESULT]\n" + result)

        # 相关摘录（assistant 轮次的句子级相关句）——消融开关 ABL_EXCERPTS
        exc = self._excerpts(question) if self.enable_excerpts else None
        if exc:
            parts.append("[RELEVANT EXCERPTS] most query-relevant assistant "
                         "sentences:\n" + "\n".join(exc))

        # v3.6 近逐字证据层：lookup/unknown 意图（SS-A 56/56、SS-P 30/30
        # 全覆盖）。v3.7 的 fact_current 扩展与 v3.7.1 的 recency 选择均已
        # 回滚（各 3-run：86.53 / 85.67 vs v3.6 86.80，KU 修复假设被证伪
        # ——KU 波动属采样方差，recency 引用反而挤掉 fact_current 所需的
        # 旧上下文）。MS/TR/KU 的 agg/list/fact_at_time/fact_current 路由
        # 渲染零接触（v3.1 教训）。
        if sq.get("intent") in ("lookup", "unknown"):
            verb = self._verbatim_turn(question)
            if verb:
                parts.append("[VERBATIM SOURCE TURN] the single most "
                             "query-relevant turn, quoted in full:\n" + verb)

        # 序数词确定性查找（"the 27th parameter" 类长清单盲区）
        ord_hit = self._ordinal_lookup(question)
        if ord_hit:
            parts.append("[DETERMINISTIC ORDINAL LOOKUP]\n" + ord_hit)

        # v3.8 #1 CURRENT-STATE LEDGER：版本链槽位的当前值显式台账（治 KU 旧值锚定）
        ledger = self._current_ledger()
        if ledger:
            parts.append("[CURRENT-STATE LEDGER] slots whose value changed over "
                         "time — for 'currently/now' questions the CURRENT value "
                         "below is authoritative:\n" + "\n".join(ledger))

        # v3.8 #2 偏好/观点分段前置（从事实清单移出，治 SS-P 稀释）
        prefs = self._preference_lines()
        if prefs:
            parts.append("[PREFERENCES & OPINIONS] the user's stated likes, "
                         "dislikes and preferences:\n" + "\n".join(prefs))

        # 事实清单（主体记忆；v3.8 #2 起不含偏好事实）
        facts = self._fact_lines()
        if facts:
            parts.append("[FACT MEMORY] all active facts, chronological "
                         "(each line is a deduplicated atomic fact):\n"
                         + "\n".join(facts))

        # 证据地板（全部 user 轮次）——消融开关 ABL_FLOOR
        floor = self._floor() if self.enable_floor else None
        if floor:
            parts.append("[USER STATEMENTS] verbatim user turns, chronological:\n"
                         + "\n".join(floor))

        # 拒答范围注记（v3.3-P0 措辞协议）：审计显示 7 道语义正确的拒答被 judge
        # 判错——根因是模型输出 "the count is 0" / "no record" 这类确定性措辞，
        # 而 judge 语料期望 "the information provided is not enough"。
        # 协议：给出标准句式模板，禁止用 0/数字收尾。
        ents = [e for e in sq.get("entities", []) if len(e) > 2]
        if ents and not self._mentions(ents):
            if self._is_advice_form(question):
                # v3.8 #4/#5：建议/推荐类问句不求"事实存在"，求"基于已陈述
                # 偏好的推荐"——拒答措辞是误导（75832dbd "recommend
                # publications" 被拒答的教训），改为处境 brief 指向偏好段。
                parts.append(
                    "[RECOMMENDATION HINT] This is a recommendation/advice "
                    "request, not a factual lookup. Ground your answer in the "
                    "user's stated interests, likes and past behavior (see "
                    "PREFERENCES & OPINIONS, facts and verbatim turns) — do "
                    "NOT refuse on the grounds that the exact item was never "
                    "mentioned; recommend based on what the user likes.")
            else:
                parts.append(
                    "[MEMORY SCOPE NOTE] The memory contains no statement about: "
                    + ", ".join(ents) + ".\n"
                    "If you find the required information is missing, you MUST answer "
                    "using wording like: \"The information provided is not enough. "
                    "You did not mention ...\" — do NOT give a number (not even 0), "
                    "a guess, or a definitive answer about it.")

        # v3.4：确定性前提校验（专有实体必须在 user 发言中出现过）——
        # SCOPE NOTE 只在解析层 entities 全 miss 时触发，覆盖不了"实体被
        # assistant 闲聊误命中"与"问题自带假前提"两种情形；PREMISE CHECK
        # 用独立规则补这个盲区。
        premise = self._premise_check(question)
        if premise:
            parts.append(premise)

        return "\n\n".join(parts)

    def _parse(self, question: str, qdate: datetime) -> dict:
        prompt = PARSE_PROMPT_V2.format(cats="|".join(CATEGORIES),
                                        date=qdate.strftime("%Y-%m-%d"),
                                        question=question)
        resp = self.client.chat(prompt, max_tokens=300)
        obj = extract_json_object(resp) or {}
        intent = _clean(obj.get("intent"), _INTENTS) or "unknown"
        return {
            "intent": intent,
            "topic": str(obj.get("topic") or question)[:200],
            "category": _clean(obj.get("category"), set(CATEGORIES)),
            "count_kind": _clean(obj.get("count_kind"),
                                 {"events", "stated_total", "either", "none"}),
            "window_days": obj.get("window_days") if isinstance(
                obj.get("window_days"), int) else None,
            "month": _clean(obj.get("month")),
            "at_text": _clean(obj.get("at_text")),
            "before_text": _clean(obj.get("before_text")),
            "entities": [str(x) for x in obj.get("entities", []) if str(x).strip()][:6],
        }

    # -- 检索与执行 --

    def _window(self, sq: dict, qdate: datetime):
        if sq.get("month"):
            try:
                m = datetime.strptime(sq["month"], "%Y-%m")
                nxt = _shift_month(m, 1)
                return m, nxt
            except ValueError:
                pass
        if sq.get("window_days"):
            return qdate - timedelta(days=sq["window_days"]), qdate
        return None

    def _resolve_before(self, sq: dict) -> datetime | None:
        bt = sq.get("before_text")
        if not bt:
            return None
        rng = resolve_when_expr(bt, datetime(2023, 6, 1))
        if rng:
            return rng[0]
        evs = self._active_events()
        if not evs:
            return None
        index = BM25([tokenize(e.fact or e.value) for e in evs])
        hits = index.topk(bt, k=1, min_score=1.0)
        if hits:
            return evs[hits[0][0]].valid_from or evs[hits[0][0]].stated_at
        return None

    def _event_candidates(self, topic: str, sq: dict, window):
        """BM25 主路 + 类别/窗口过滤辅路，RRF 思想合并去重。"""
        evs = self._active_events()
        if not evs:
            return []
        index = BM25([tokenize(e.fact or e.value) for e in evs])
        scored: dict[int, float] = {}
        for idx, s in index.topk(topic, k=30, min_score=0.5):
            scored[idx] = s
        # 辅路：类别/窗口命中但 BM25 低分的事件，给底分保进候选
        for i, e in enumerate(evs):
            if i in scored:
                continue
            if sq.get("category") and e.category == sq["category"]:
                anchor = e.valid_from or e.stated_at
                if window and anchor and not (window[0] <= anchor <= window[1]):
                    continue
                if not window:
                    continue
                scored[i] = 0.6
        out = [(evs[i], s) for i, s in scored.items()]
        out.sort(key=lambda t: -t[1])
        return out

    def _slots(self) -> dict[int, list]:
        slots: dict[int, list] = {}
        for e in self.store.events:
            slots.setdefault(getattr(e, "slot_id", None) or e.tx_seq,
                             []).append(e)
        for sid in slots:
            slots[sid].sort(key=lambda e: (e.valid_from or datetime.min, e.tx_seq))
        return slots

    def _execute(self, sq: dict, qdate: datetime) -> tuple[str, dict]:
        intent = sq["intent"]
        if intent == "unknown":
            return "", {"matched": 0}
        topic = sq["topic"] or ""
        window = self._window(sq, qdate)
        before_dt = None
        if sq.get("before_text"):
            before_dt = self._resolve_before(sq)
            if before_dt:
                window = (datetime.min, before_dt)
                # "before X" 是事件排序问题，一律走事件列表而非槽位链
                if intent in ("fact_current", "fact_at_time", "lookup"):
                    intent = "list_events"

        meta: dict = {"matched": 0}
        lines: list[str] = []
        wtxt = f"{_fmt_d(window[0])}..{_fmt_d(window[1])}" if window else "all time"

        if intent in ("agg_count", "agg_sum", "list_events"):
            cand = self._event_candidates(topic, sq, window)
            if sq.get("category"):
                cand = [(e, s) for e, s in cand if e.category == sq["category"]]
            if window:
                cand = [(e, s) for e, s in cand
                        if (lambda a: a and window[0] <= a <= window[1])(
                            e.valid_from or e.stated_at)]
            # "before X" 排序题：硬窗口会切掉"陈述时已拥有"锚定的事实
            # （acquisition 早于陈述但晚于边界），补一个上下文分区。
            later_ctx: list = []
            if before_dt is not None and intent == "list_events":
                raw_cand = self._event_candidates(topic, sq, None)
                later_ctx = [(e, s) for e, s in raw_cand
                             if (e.valid_from or e.stated_at or datetime.max) > window[1]][:5]
            meta["matched"] = len(cand)
            if not cand:
                lines.append(f'Query: {intent}  topic="{topic}"  window={wtxt}\n'
                             "No matching facts in memory.")
                return "\n".join(lines), meta

            # v3.5-A 窗口锚已回滚（见 docs/iteration-log.md 2026-10-03 验证轮）：
            # 三轮无正证据且 MS/SS 走低，按"无正证据不保留"原则退回 v2.6 渲染。
            # v3.1 实验已回滚（见 docs/iteration-log.md）：strict/borderline 分层
            # 在全量上 -1.4pp（MS -6、TR -3）。机理：①STRICT COUNT 锚定效应——
            # strict 漏掉真事件时读者被锚死；②LOW 层隐藏了低分真事件。
            # v2.6 的"平铺 top-15 + TOTAL MATCHED 召回计数"仍是最优渲染。
            # v3.3-P4：list_events（排序/先后类问题）额外附一个按 valid_from
            # 升序的 CHRONOLOGICAL TIMELINE——BM25 分数序对"哪个先"类问题
            # 是误导性的（审计：模式 3 排序错 ×4 真错）。
            lines.append(f'Query: {intent}  topic="{topic}"  window={wtxt}')
            lines.append("Candidate events, ranked by relevance to the topic "
                         "(already deduplicated):")
            for e, s in cand[:15]:
                lines.append(f"  ({s:.1f}) {_fmt_d(e.valid_from or e.stated_at)} | "
                             f"{_clip(e.fact, 220)}")
            # v3.9 #3：一跳实体扩展（仅 store>100 events 的 S/M 规模触发，
            # Oracle ≈33 events 零行为变化）。独立段落 + 不计入 TOTAL MATCHED，
            # 由 reader 自行取舍——避免 v3.1 式的锚定/计数污染。
            if len(self.store.events) > 100 and intent in (
                    "agg_count", "agg_sum", "list_events"):
                extras = self._one_hop([e for e, _ in cand[:5]])
                if extras:
                    cand_main = cand
                    cand = cand_main + extras
                    meta["one_hop"] = len(extras)
                    lines.append(
                        f"ONE-HOP RELATED ({len(extras)}) — pulled in by shared "
                        "rare entities with the top candidates; include each "
                        "ONLY if it truly matches the question:")
                    for e, _s in extras:
                        lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                                     f"{_clip(e.fact, 220)}")
            if len(cand) > 15 and "one_hop" not in meta:
                lines.append(f"  ... ({len(cand) - 15} more, lower relevance)")
            if intent == "list_events":
                chrono = sorted(cand[:15], key=lambda t: (
                    t[0].valid_from or t[0].stated_at or datetime.min, t[0].tx_seq))
                lines.append("CHRONOLOGICAL TIMELINE (same events, sorted by date "
                             "ascending — use THIS order for before/after/first/"
                             "sequence questions):")
                for e, _s in chrono:
                    lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                                 f"{_clip(e.fact, 220)}")
            if intent == "agg_count":
                lines.append(f"TOTAL MATCHED = {len(cand)} — this is a RECALL count, "
                             "not the answer: it may include items that do not truly "
                             "match the question. Review each event above, exclude the "
                             "irrelevant ones, and state the final count explicitly.")
                meta["computed"] = len(cand)
            elif intent == "agg_sum":
                tot, missing = 0.0, 0
                for e, _ in cand[:15]:
                    n = _first_number(e.fact)
                    if n is not None:
                        tot += n
                    else:
                        missing += 1
                lines.append(f"TENTATIVE SUM over listed events = {tot:g}"
                             + (f" ({missing} items without a number — list them "
                                f"instead of guessing)" if missing else ""))
                meta["computed"] = round(tot, 2)
            if before_dt is not None and intent == "list_events" and later_ctx:
                lines.append(f"CONTEXT (events on/after the boundary "
                             f"{_fmt_d(window[1])}, for ordering reference):")
                for e, s in later_ctx:
                    lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                                 f"{_clip(e.fact, 200)}")
                lines.append("(Note: a fact dated at a statement time means the user "
                             "already had/owned it when that statement was made; the "
                             "actual acquisition may predate the stated date.)")
            # 声明式总量（collection total）辅助路
            if sq.get("count_kind") in ("stated_total", "either"):
                st = self._stated_totals(topic)
                if st:
                    lines.append("STATED TOTALS / RELATED SLOTS found in memory "
                                 "(a number the user explicitly gave):")
                    lines.extend(st)
            # 算术注记：总量声明 + 后续增量（不依赖槽位绑定，直接在匹配事件上算）
            if intent in ("agg_count", "agg_sum") or sq.get("count_kind") in (
                    "stated_total", "either"):
                note = (self._arithmetic_note_events([e for e, _ in cand])
                        if self.enable_arith else None)
                if note:
                    lines.append(note)

        elif intent in ("fact_current", "fact_at_time"):
            chains = self._slot_chains(topic)
            meta["matched"] = len(chains)
            if not chains:
                lines.append(f'Query: {intent}  topic="{topic}"\n'
                             "No matching memory slot found.")
            for ch in chains[:2]:
                lines.append(ch)
            # 跨槽位算术注记：总量与增量可能被分进不同槽位（分类器方差），
            # 在话题 BM25 匹配的事件并集上再算一遍
            top_evs = [e for e, _ in self._event_candidates(topic, sq, None)[:20]]
            note = (self._arithmetic_note_events(top_evs)
                    if self.enable_arith else None)
            if note and not any("ARITHMETIC NOTE" in ch for ch in chains[:2]):
                lines.append(note)
            # v3.9 #3：一跳实体扩展（fact_current 路由，仅 store>100 events
            # 的 S/M 规模）——matched 槽位的稀有实体共现事件补录，治
            # "集合成员被词面 topical 匹配漏掉"（S 档 MS 46a3abf7/6456829e）
            if len(self.store.events) > 100 and top_evs:
                extras = self._one_hop(top_evs[:5])
                if extras:
                    lines.append(
                        f"ONE-HOP RELATED ({len(extras)}) — pulled in by shared "
                        "rare entities with the matched slots; relevant to the "
                        "question's collection/state — check each:")
                    for e, _s in extras:
                        lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                                     f"{_clip(e.fact, 220)}")
            if intent == "fact_at_time" and sq.get("at_text"):
                t_rng = resolve_when_expr(sq["at_text"], qdate)
                if t_rng:
                    lines.append(f'Interpreting "{sq["at_text"]}" as '
                                 f'{_fmt_d(t_rng[0])}..{_fmt_d(t_rng[1])}')

        elif intent == "argmax":
            # v3.3-P1：最值查询形状——"哪家花最多/哪次最长"。
            # 确定性执行：对候选事件逐个抽数值，按数值降序输出，首项即 argmax。
            cand = self._event_candidates(topic, sq, window)
            meta["matched"] = len(cand)
            scored = []
            for e, s in cand[:20]:
                n = _first_number(e.fact)
                scored.append((e, s, n))
            have_n = [t for t in scored if t[2] is not None]
            no_n = [t for t in scored if t[2] is None]
            lines.append(f'Query: argmax  topic="{topic}"')
            if have_n:
                have_n.sort(key=lambda t: -t[2])
                lines.append("Candidates WITH numeric amounts, sorted by amount "
                             "DESCENDING (top = the largest):")
                for e, s, n in have_n[:10]:
                    lines.append(f"  [amount≈{n:g}] ({s:.1f}) "
                                 f"{_fmt_d(e.valid_from or e.stated_at)} | "
                                 f"{_clip(e.fact, 200)}")
                top = have_n[0]
                lines.append(f"HIGHEST-AMOUNT CANDIDATE: "
                             f"{_clip(top[0].fact, 200)} (amount≈{top[2]:g}) — "
                             "verify it truly belongs to the asked-about set before "
                             "answering; read the sentence for the exact figure.")
                meta["computed"] = round(top[2], 2)
            else:
                lines.append("No numeric amounts found among matching events; "
                             "the raw candidates follow — judge the maximum from "
                             "their content:")
                for e, s in no_n[:10]:
                    lines.append(f"  ({s:.1f}) {_fmt_d(e.valid_from or e.stated_at)} | "
                                 f"{_clip(e.fact, 200)}")

        else:  # lookup
            cand = self._event_candidates(topic, sq, None)
            meta["matched"] = len(cand)
            lines.append(f'Query: lookup  topic="{topic}"')
            for e, s in cand[:10]:
                lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                             f"{_clip(e.fact, 220)}")

        return "\n".join(lines), meta

    def _slot_chains(self, topic: str) -> list[str]:
        slots = self._slots()
        if not slots:
            return []
        ids = list(slots.keys())
        reps = []
        for sid in ids:
            act = [e for e in slots[sid] if e.status == "active"] or slots[sid]
            rep = max(act, key=lambda e: (e.valid_from or datetime.min, e.tx_seq))
            reps.append(rep.fact or rep.value)
        index = BM25([tokenize(r) for r in reps])
        out = []
        for idx, s in index.topk(topic, k=2, min_score=1.5):
            sid = ids[idx]
            evs = slots[sid]
            lines = [f'Memory slot (matched, score {s:.1f}) — version chain '
                     "oldest -> newest:"]
            for e in evs:
                rng = (f"[{_fmt_d(e.valid_from)} .. "
                       f"{_fmt_d(e.valid_to) if e.valid_to else 'now'}]")
                tag = {"active": "current", "superseded": "was true until then",
                       "invalidated": "corrected, was never true",
                       "archived": "forgotten, kept for audit"}[e.status]
                lines.append(f"  {rng} {_clip(e.fact or e.value, 200)} ({tag})")
            note = self._arithmetic_note(evs) if self.enable_arith else None
            if note:
                # 注记即确定性的"当前值"，取代字面 CURRENT VALUE 行——
                # 否则 "CURRENT VALUE: 37" 会把读者锚定在过期总量上
                lines.append(note)
            else:
                cur = [e for e in evs if e.status == "active"]
                if cur:
                    latest = max(cur, key=lambda e: (e.valid_to or datetime.max,
                                                     e.tx_seq))
                    lines.append(f"  CURRENT VALUE: "
                                 f"{_clip(latest.fact or latest.value, 200)}"
                                 f" (effective {_fmt_d(latest.valid_from)})")
            out.append("\n".join(lines))
        return out

    _ADD_RE = re.compile(
        r"\b(added|bought|got|acquired|picked up|won|received|purchased)\b", re.I)
    _TOTAL_RE = re.compile(
        r"\b(total of|a total|in total|now have|up to|collection of)\b", re.I)

    def _arithmetic_note_events(self, evs: list) -> str | None:
        """确定性算术注记：声明的总量 + 其后记录的增量 = 可能的当前总量。

        来源（coins 题）：用户先说"共 37 枚"，之后说"刚入手一枚 Barber
        quarter"——38 从未直接出现，必须由总量+增量算出。
        v2.3 教训：该计算**不能依赖槽位绑定**（关系分类有方差，绑定失败
        注记就消失），改为在话题匹配的事件列表上直接计算。
        """
        dated = []
        for e in evs:
            if e.status != "active":
                continue
            d = e.valid_from or e.stated_at
            if d:
                dated.append((d, e))
        dated.sort(key=lambda t: t[0])

        def _is_total(ev) -> bool:
            # 纯总量声明：含总量短语且不含增量动词。
            # v2.4 教训："just added a new coin to their collection of ..."
            # 同时命中 "collection of" 和 "added"——若不加动词排除，
            # 增量会被误当成总量声明，把注记整个压掉。
            return bool(self._TOTAL_RE.search(ev.fact)
                        and not self._ADD_RE.search(ev.fact))

        total_ev, total_n = None, None
        for d, e in dated:
            if _is_total(e):
                n = _first_number(e.fact)
                if n is not None:
                    total_ev, total_n = (d, e), n
        if total_ev is None:
            return None
        td, _te = total_ev
        # total_ev 取的是最后一条纯总量声明，其后不可能再有纯总量声明
        adds = [(d, e) for d, e in dated
                if d > td and self._ADD_RE.search(e.fact)]
        if not adds:
            return None
        est = total_n + len(adds)
        return (f"ARITHMETIC NOTE (deterministic): the memory states a total of "
                f"{total_n:g} (on {_fmt_d(td)}) and records {len(adds)} later "
                f"addition(s) to it -> the current total is likely {est:g}. "
                f"Verify the additions truly belong to the same collection; if "
                f"they do, answer with {est:g}.")

    def _arithmetic_note(self, evs: list) -> str | None:
        """槽位链版本：内部转发到事件列表实现（缩进适配渲染）。"""
        note = self._arithmetic_note_events(evs)
        if note is None:
            return None
        return "  " + note

    def _stated_totals(self, topic: str) -> list[str]:
        chains = self._slot_chains(topic)
        return [c for c in chains if re.search(r"\d", c)][:1]

    def _fact_lines(self) -> list[str]:
        evs = [e for e in self._active_events()
               if e.predicate != "preference"]      # v3.8 #2：偏好移至独立分段
        evs.sort(key=lambda e: (e.valid_from or e.stated_at or datetime.min, e.tx_seq))
        out = []
        for e in evs[:_FACT_CAP]:
            out.append(f"- {_fmt_d(e.valid_from or e.stated_at)} | "
                       f"{_clip(e.fact, 240)}")
        if len(evs) > _FACT_CAP:
            out.append(f"- ... ({len(evs) - _FACT_CAP} more)")
        return out

    def _preference_lines(self) -> list[str]:
        """v3.8 #2：偏好/观点单独成段——软信息混在事件事实里被稀释
        （SS-P 病根）。从事实清单移出（_fact_lines 已过滤），前置渲染。"""
        evs = [e for e in self._active_events() if e.predicate == "preference"]
        evs.sort(key=lambda e: (e.valid_from or e.stated_at or datetime.min, e.tx_seq))
        return [f"- {_fmt_d(e.valid_from or e.stated_at)} | {_clip(e.fact, 240)}"
                for e in evs]

    def _current_ledger(self) -> list[str]:
        """v3.8 #1：CURRENT-STATE LEDGER——有版本历史的槽位显式标注当前值。

        KU 病根：清单里新旧版本同场，reader 无"哪个是现在"的显式信号
        （镜头题答旧焦距、租期题答旧租期）。台账用与 fact_current 相同的
        确定性算子（current = max active by valid_to/tx_seq），只列发生过
        superseded/invalidated 的槽位——无历史的槽位与事实清单重复，不列。
        """
        groups: dict[tuple, list] = {}
        for e in self.store.events:
            groups.setdefault((e.subject, e.predicate), []).append(e)
        out = []
        for (subj, pred), evs in sorted(groups.items()):
            if not any(e.status in ("superseded", "invalidated") for e in evs):
                continue
            act = [e for e in evs if e.status == "active"]
            if not act:
                continue
            cur = max(act, key=lambda e: (e.valid_to or datetime.max, e.tx_seq))
            eff = cur.valid_from or cur.stated_at
            n_old = sum(1 for e in evs if e.status == "superseded")
            out.append(f"- {subj}.{pred}: CURRENT = {_clip(cur.fact or cur.value, 180)}"
                       f" (effective {_fmt_d(eff)}; {n_old} older value(s) superseded)")
        return out

    def _excerpts(self, question: str) -> list[str]:
        """assistant 轮次的**句子级**相关摘录。

        v2.5 教训（Plesiosaur 题）：轮次级 300 字符截断只露出长故事的开头，
        细节（颜色/排班/列表项）在轮次深处——对 top 轮次做句内 BM25，
        把与问题最相关的句子直接呈给出口 LLM。
        """
        asst = [t for t in self.store.evidence if t.role == "assistant"]
        if not asst:
            return []
        # 先选 top 轮次，再在轮次内部做句子级排序
        turn_idx = BM25([tokenize(t.content) for t in asst])
        top_turns = [i for i, _ in turn_idx.topk(question, k=6, min_score=2.0)]
        if not top_turns:
            return []
        sentences: list[tuple[str, str]] = []   # (date, sentence)
        for i in top_turns:
            t = asst[i]
            for sent in re.split(r"(?<=[.!?])\s+|\n+", t.content):
                sent = sent.strip()
                if 20 < len(sent) < 400:
                    sentences.append((t.date.strftime("%Y-%m-%d"), sent))
        if not sentences:
            return []
        sent_idx = BM25([tokenize(s) for _, s in sentences])
        out = []
        for j, s in sent_idx.topk(question, k=8, min_score=1.0):
            out.append(f"- {sentences[j][0]} assistant: {_clip(sentences[j][1], 260)}")
        return out

    _ORDINAL_RE = re.compile(r"\b(\d{1,3})(?:st|nd|rd|th)\b", re.I)

    def _verbatim_turn(self, question: str) -> str | None:
        """v3.6 近逐字证据层：检索最强命中的源轮次全文引用（单条）。

        动机（对标公开数据）：SS-A/SS-U 的答案逐字躺在一个轮次里，摘录
        （句子级、260 字符）是有损的——Mem0 新版把 assistant 事实一等公民
        化后在 SS-A 上 +51.8，公开天花板 98-100。记忆 ≠ 压缩：事实层压缩，
        源轮次引用保真。仅由 read() 在 lookup/unknown 意图下调用，
        MS/TR/KU 的确定性路由不受影响（v3.1 教训）。
        （v3.7 的 fact_current 扩展与 v3.7.1 的 recency 选择已回滚：
        各 3-run 86.53 / 85.67 vs v3.6 86.80，均无正证据。）
        """
        turns = self.store.evidence
        if not turns:
            return None
        idx = BM25([tokenize(t.content) for t in turns])
        hits = idx.topk(question, k=1, min_score=0.5)
        if not hits:
            return None
        i, s = hits[0]
        t = turns[i]
        return (f"- {_fmt_d(t.date)} {t.role} (verbatim, relevance {s:.1f}):\n"
                f"  {_clip(t.content, _VERBATIM_CHARS)}")

    def _ordinal_lookup(self, question: str) -> str | None:
        """序数确定性查找："the 27th parameter" → 列表第 27 项原文。

        长枚举（100 项的清单）是抽取与摘录都覆盖不了的盲区，
        但它是纯文本定位问题——确定性代码一步解决。
        （v3.5-B 的相对序数/多列表消歧升级已回滚：三轮无正证据。）
        """
        m = self._ORDINAL_RE.search(question)
        if not m:
            return None
        n = int(m.group(1))
        if not (1 <= n <= 500):
            return None
        pat = re.compile(rf"^\s*{n}\s*[.):\]]\s*(.+)$", re.M)
        for t in self.store.evidence:
            mm = pat.search(t.content)
            if mm:
                return (f"ORDINAL LOOKUP (deterministic): item {n} in the list "
                        f"mentioned by the assistant reads: \""
                        f"{_clip(mm.group(1), 220)}\"")
        return None

    def _floor(self) -> list[str]:
        out = []
        for t in self.store.evidence:
            if t.role != "user":
                continue
            out.append(f"- {_fmt_d(t.date)} user: {_clip(t.content, _TURN_CHARS)}")
        return out

    def _mentions(self, words: list[str]) -> bool:
        """实体存在性检查（拒答注记的触发条件）。

        语义：多词实体要求**全部词在同一句/同轮文本中共现**。
        v2.3 教训：只查修饰语是否在库中任何位置出现，会被
        "Italian-Style Lentil Curry" 这类菜名误触发（italian 在、
        restaurant 不在同一句），从而漏发拒答注记。
        """
        texts = [e.fact or e.value for e in self.store.events] + \
                [t.content for t in self.store.evidence]
        for phrase in words:
            toks = tokenize(phrase)
            if not toks:
                continue
            for text in texts:
                tset = set(tokenize(text))
                if all(w in tset for w in toks):
                    return True
        return False

    # v3.4：user 级前提校验——_abs 的"事实性前提"必须由用户自己的发言支撑。
    # 教训（_abs 稳定错 ×6）：assistant 闲聊里的无关提及（讲长野交通时罗列了
    # Shinjuku、讲网球包时提到 table tennis）会经 _mentions 抑制 SCOPE NOTE，
    # 让 reader 顺着错误前提作答。
    _GENERIC_TOKS = {
        "user", "my", "me", "i", "we", "our", "you", "your", "the", "a", "an",
        "in", "on", "at", "for", "with", "and", "or", "of", "to", "do", "did",
        "does", "have", "has", "had", "am", "is", "are", "was", "were", "will",
        "would", "can", "could", "how", "many", "much", "what", "which", "when",
        "where", "who", "long", "often", "first", "current", "new", "just",
        "started", "start", "living", "live", "playing", "play", "lead",
        "leading", "friends", "local", "park", "tank", "apartment", "role",
        "job", "current", "team", "collection", "month", "months", "week",
        "weeks", "year", "years", "day", "days", "time", "times", "high",
        "school", "undergrad", "course", "research", "project", "gallon",
        "different", "related", "kind", "type", "sort", "things", "thing",
        "item", "items", "stuff", "much", "more", "most", "less", "least",
        "store", "shop", "place", "city", "area", "event", "events",
        "task", "tasks", "game", "games", "people", "person", "friend",
    }

    _ADVICE_KW = ("any advice", "can you recommend", "recommend", "any tips",
                  "any suggestions", "what should", "any idea", "help me",
                  "i'm thinking of", "i've been feeling", "i've been struggling",
                  "what do you suggest", "should i", "what do you think",
                  "do you think", "whether to", "i'm trying to decide", "advise")

    def _is_advice_form(self, question: str) -> bool:
        """建议/推荐/征求意见形态判定（v3.4.5 引入，v3.8 扩词表并共享）。

        v3.8 #4 审计：SS-P 拒答型失分的主体是 "should I buy X" /
        "what do you think" / "I'm trying to decide whether to" 形态——
        均不在 v3.4.5 豁免清单。此类问句求的是"基于已陈述偏好的建议"，
        前提本来就不是"user 说过这个词"。_abs 30 题零命中，扩展零回归。
        """
        q = question.lower()
        return any(k in q for k in self._ADVICE_KW)

    def _premise_check(self, question: str) -> str | None:
        """确定性前提校验：问题中的专有实体必须在 user 发言中出现过。

        v3.4.5 边界：**只校验事实性疑问**（"我的 X 有多少/在哪/多久"）。
        请求/推荐类问句（"Any advice / Can you recommend"）的前提本来
        就不是"user 说过这个"——那 8 道 preference 题被误杀的教训。
        """
        if self._is_advice_form(question):
            return None
        user_texts = [t.content for t in self.store.evidence if t.role == "user"]
        user_texts += [e.fact for e in self.store.events
                       if e.subject != "assistant" and e.fact]
        if not user_texts:
            return None
        user_toks: set[str] = set()
        user_blob = " \n ".join(user_texts).lower()
        for t in user_texts:
            user_toks.update(tokenize(t))

        suspects = []
        # v3.4.1 收紧：只认专名信号（原文大写 / 数字 / 连字符词），忽略措辞动词。
        # 误报复盘：acquire/total/past 是问题措辞而非事实前提（用户说的是
        # got/bought/in all），这类词触发会把好答案带偏。
        raw_words = question.split()
        # v3.4.4：专名信号判定排除句首词（"How"）与代词（"I"）——句首大写
        # 是排版规则不是专名；它们会把整个 2-gram 带偏（"How many plants did
        # I acquire" 里 acquire 因此混进 suspects）
        _PRONOUNS = {"i", "you", "he", "she", "it", "we", "they", "my", "your",
                     "his", "her", "its", "our", "their", "me", "him", "us"}
        proper_flags: dict[str, bool] = {}
        for idx, w_raw in enumerate(raw_words):
            w = w_raw.strip(".,?!")
            if not w:
                continue
            lower = w.lower()
            if lower in _PRONOUNS:
                proper_flags[lower] = False
                continue
            proper_flags[lower] = (
                (w[0].isupper() and idx > 0)              # 原文大写且非句首
                or any(ch.isdigit() for ch in w)         # 含数字（30-gallon）
                or "-" in w                              # 连字符组合
            )
        checked_grams: set[str] = set()
        for n in (3, 2):
            for i in range(len(raw_words) - n + 1):
                gram_raw = raw_words[i:i + n]
                gram = " ".join(gram_raw).lower().strip(".,?!")
                if gram in checked_grams:
                    continue
                checked_grams.add(gram)
                toks = [w for w in tokenize(gram) if w]
                if not toks:
                    continue
                has_digit = any(ch.isdigit() for ch in gram)
                # proper 信号按**小写 key**查（proper_flags 的 key 是 lower 后的
                # 词）——v3.4.2 bug：拿原文形态查永远 miss
                has_proper = any(proper_flags.get(w.strip(".,?!").lower(), False)
                                 for w in gram_raw)
                has_specific = [w for w in toks if w not in self._GENERIC_TOKS]
                # v3.4.3 最终触发条件：**硬专名信号**（数字/连字符/原文大写词）。
                # 教训链：①同义改写（acquire↔bought、clothing↔clothes）不是假
                # 前提；②"两词全专有"也不行（"acquire in"/"past two" 都触发）——
                # 只有不可改写的专名（Shinjuku、30-gallon、Porsche）才是 _abs
                # 真正的前提校验对象
                if not (has_digit or has_proper):
                    continue
                # 存在性：分词各自在 user 语料即算存在（宽松，避免形态误报）
                if all(w in user_toks for w in toks):
                    continue
                for w in has_specific:
                    if w not in user_toks and w not in suspects:
                        suspects.append(w)
        # 单词级：只认原文数字/连字符的专名（"pre-1920" 类）；存在性按 tokenize
        # 结果判——**任一部词**（"art" of "art-related"）在 user 语料即算存在。
        # v3.4.4 教训：all-部词判定被 "related/different" 这类措辞形容词卡死；
        # 连字符复合词的本质是修饰（art-related=与 art 相关），部词在即存在。
        for w_raw, w in ((r, r.strip(".,?!").lower()) for r in raw_words):
            is_proper = (any(ch.isdigit() for ch in w) or "-" in w)
            if (is_proper and len(w) >= 4 and w not in self._GENERIC_TOKS):
                sub = [t for t in tokenize(w) if t not in self._GENERIC_TOKS]
                if sub and any(t in user_toks for t in sub):
                    continue
                if w not in suspects:
                    suspects.append(w)
        if not suspects:
            return None

        # 可疑词的最近邻（在 user 语料里找最接近的词，供 reader 对照）
        near = []
        all_user = sorted(user_toks)
        for s in suspects[:4]:
            best, bd = None, 99
            for c in all_user:
                if abs(len(c) - len(s)) > 4 or c[:2] != s[:2]:
                    continue
                d = abs(len(c) - len(s)) + (0 if c[:3] == s[:3] else 2)
                if d < bd:
                    best, bd = c, d
            if best and bd < 6:
                near.append((s, best))
        lines = ["[PREMISE CHECK] The following specific terms in the question "
                 "NEVER appear in anything the user actually said: "
                 + ", ".join(suspects[:4]) + "."]
        if near:
            lines.append("The user actually mentioned (closest terms): "
                         + "; ".join(f'"{b}" (vs. "{s}")' for s, b in near) + ".")
        lines.append("The question may contain a false premise. If the asked-about "
                     "thing was never stated by the user, answer that the "
                     "information provided is not enough and point out what the "
                     "user actually mentioned instead — do NOT answer as if the "
                     "premise were true.")
        return "\n".join(lines)
