"""读取路径：解析问题 → 确定性算子 → 检索 → 渲染输入。"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta

from bm25 import BM25, tokenize
from llm_io import PARSE_PROMPT_V2, PARSE_PROMPT_V2_ZH, extract_json_object
from config import _INTENTS
from plan import audit_plan
from schema import CATEGORIES
from temporal import _shift_month, resolve_when_expr
from textutil import _clean, _clip, _first_number, _fmt_d, _looks_zh


class ReadMixin:
    """由 FusionMemory 组合；所有方法依赖 self.store / self.client / self._cached。"""

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
            # 同 df 的 token 必须按词面二次排序：set 的迭代顺序随进程哈希种子
            # 变化，只按 df 排会让 one-hop 结果在两次运行间漂移（已复现 3/5 条）。
            toks = sorted(self._entity_toks(e),
                          key=lambda x: (df.get(x, 999), x))
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

    def read(self, question: str, question_date: str) -> str:
        from lme_core import parse_dt
        t0 = time.perf_counter()
        qdate = parse_dt(question_date) or datetime(2023, 6, 1)
        self.last_trace = {"question": question, "question_date": question_date}
        zh = _looks_zh(question)

        tp = time.perf_counter()
        sq = self._cached("parse|" + hashlib.sha1(
            json.dumps([question, question_date],
                       ensure_ascii=False).encode("utf-8")).hexdigest(),
            lambda: self._parse(question, qdate, zh=zh),
            scope="parse_zh" if zh else "parse")
        self.last_trace["plan"] = dict(sq)
        # 静态审计（只读）：计划缺字段 / 时间没解析出来时留下问题码，
        # 供 regress.py --audit-plans 汇总。不修补字段，不影响执行。
        self.last_trace["plan_issues"] = audit_plan(sq, question, qdate)
        self.last_trace["parse_ms"] = round((time.perf_counter() - tp) * 1000, 2)
        self.last_meta = {"intent": sq.get("intent"), "topic": sq.get("topic")}

        parts: list[str] = []
        te = time.perf_counter()
        result, meta = self._execute(sq, qdate)
        self.last_trace["execute_ms"] = round((time.perf_counter() - te) * 1000, 2)
        self.last_trace["execution"] = meta
        self.last_trace["deterministic_result"] = result
        # mem_meta 保持原有的小字段；trace_* 大对象只进独立 trace 文件。
        self.last_meta.update({k: v for k, v in meta.items()
                               if not k.startswith("trace_")})
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

        context = "\n\n".join(parts)
        self.last_trace["sections"] = [p.split("\n", 1)[0] for p in parts]
        self.last_trace["context_chars"] = len(context)
        self.last_trace["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return context

    def _parse(self, question: str, qdate: datetime, zh: bool = False) -> dict:
        tpl = PARSE_PROMPT_V2_ZH if zh else PARSE_PROMPT_V2
        prompt = tpl.format(cats="|".join(CATEGORIES),
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

    def _trace_events(self, cand: list, limit: int = 30) -> list[dict]:
        """把候选事件压成可 JSON 序列化的归因记录，不携带 Event 对象。"""
        return [{
            "date": _fmt_d(e.valid_from or e.stated_at),
            "score": round(float(s), 3),
            "fact": _clip(e.fact or e.value, 220),
            "status": e.status,
            "slot_id": e.slot_id,
        } for e, s in cand[:limit]]

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

        wtxt = f"{_fmt_d(window[0])}..{_fmt_d(window[1])}" if window else "all time"
        meta: dict = {"matched": 0, "intent": intent, "topic": topic,
                      "window": wtxt, "trace_candidates": []}
        lines: list[str] = []

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
            meta["trace_candidates"] = self._trace_events(cand)
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
            # v3.9.1：one-hop 收支按路由拆分（S 档 50 题配对实测）——
            # fact_current +1 / agg_count ±1 打平 / **list_events -1 纯亏**
            # （TR 日期差题需要精确两日期，extras 并进 cand 还会污染
            # chrono 时间线排序）。list_events 移出 one-hop 白名单。
            if len(self.store.events) > 100 and intent in (
                    "agg_count", "agg_sum"):
                extras = self._one_hop([e for e, _ in cand[:5]])
                if extras:
                    cand_main = cand
                    cand = cand_main + extras
                    meta["one_hop"] = len(extras)
                    meta["trace_one_hop"] = self._trace_events(extras)
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
                             "match the question.")
                lines.append("Review each event above, exclude the irrelevant "
                             "ones, and state the final count explicitly.")
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
            meta["trace_candidates"] = self._trace_events(
                [(e, 0.0) for e in top_evs])
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
            meta["trace_candidates"] = self._trace_events(cand)
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
            meta["trace_candidates"] = self._trace_events(cand)
            lines.append(f'Query: lookup  topic="{topic}"')
            for e, s in cand[:10]:
                lines.append(f"  {_fmt_d(e.valid_from or e.stated_at)} | "
                             f"{_clip(e.fact, 220)}")

        return "\n".join(lines), meta
