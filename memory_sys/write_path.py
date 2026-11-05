"""写入路径：一轮对话 → 抽取 → 定时 → 关系判定 → 落库。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from bm25 import BM25, tokenize
from llm_io import (ASSISTANT_FACTS_PROMPT, ASSISTANT_FACTS_PROMPT_ZH,
                    RELATION_PROMPT, RELATION_PROMPT_ZH,
                    TURN_EXTRACTION_PROMPT, TURN_EXTRACTION_PROMPT_ZH,
                    extract_json_array)
from schema import CATEGORIES
from temporal import resolve_when
from textutil import _clip, _fmt_d, _looks_zh


class WriteMixin:
    """由 FusionMemory 组合；所有方法依赖 self.store / self.client / self._cached。"""

    def write(self, session: list[dict], session_date: str) -> None:
        from lme_core import parse_dt
        self._sess_n += 1
        self._eidx = None                    # v3.9：事件池变化，实体倒排失效
        sid = f"s{self._sess_n}"
        sdate = parse_dt(session_date) or datetime(2023, 1, 1)
        self.store.add_evidence(sid, sdate, session)
        zh = _looks_zh(" ".join(str(t.get("content", "")) for t in session))

        # ---- 1) 逐 user 轮次抽取（Mem0 粒度）----
        cands: list[dict] = []
        for i, t in enumerate(session):
            if t["role"] != "user":
                continue
            nxt = session[i + 1] if i + 1 < len(session) and session[i + 1]["role"] == "assistant" else None
            key = "ext|" + hashlib.sha1(
                json.dumps([session_date, t["content"]],
                           ensure_ascii=False).encode("utf-8")).hexdigest()
            raw = self._cached(key, lambda ut=t, at=nxt: self._extract_turn(
                ut, at, sdate, zh=zh), scope="extract_zh" if zh else "extract")
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
            raw = self._cached(key, lambda: self._extract_assistant(
                asst_turns, sdate, zh=zh),
                scope="assistant_zh" if zh else "assistant")
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
        rel_out = self._classify(cands, existing, sdate, zh=zh)
        for c, act in zip(cands, rel_out):
            self._apply(c, act, sdate, sid)

    def _extract_turn(self, user_turn: dict, asst: dict | None,
                      sdate: datetime, zh: bool = False) -> list:
        tpl = TURN_EXTRACTION_PROMPT_ZH if zh else TURN_EXTRACTION_PROMPT
        prompt = tpl.format(
            date=sdate.strftime("%Y-%m-%d"),
            user_turn=user_turn["content"],
            assistant_turn=(asst["content"] if asst else "(none)"),
            cats="|".join(CATEGORIES))
        resp = self.client.chat(prompt, max_tokens=700)
        return extract_json_array(resp) or []

    def _extract_assistant(self, asst_turns: str, sdate: datetime,
                           zh: bool = False) -> list:
        tpl = ASSISTANT_FACTS_PROMPT_ZH if zh else ASSISTANT_FACTS_PROMPT
        prompt = tpl.format(
            assistant_turns=_clip(asst_turns, 6000), cats="|".join(CATEGORIES))
        resp = self.client.chat(prompt, max_tokens=500)
        return extract_json_array(resp) or []

    # -- 写入期对齐 --

    def _active_events(self) -> list:
        return [e for e in self.store.events if e.status == "active"]

    def _classify(self, cands: list[dict], existing: list, sdate: datetime,
                  zh: bool = False) -> list[dict]:
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
            "\n".join(ex_lines), "\n".join(cd_lines), sdate, zh=zh),
            scope="relation_zh" if zh else "relation")

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

    def _relation_call(self, ex_block: str, cd_block: str, sdate: datetime,
                       zh: bool = False):
        tpl = RELATION_PROMPT_ZH if zh else RELATION_PROMPT
        prompt = tpl.format(date=sdate.strftime("%Y-%m-%d"),
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
