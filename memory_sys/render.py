"""上下文渲染：事实清单 / 台账 / 偏好 / 摘录 / 地板 / 守卫。"""
from __future__ import annotations

import re
from datetime import datetime

from bm25 import BM25, tokenize
from config import _FACT_CAP, _TURN_CHARS, _VERBATIM_CHARS
from lexicon import LANGUAGE_GENERIC
from textutil import _clip, _first_number, _fmt_d


# 前提校验只报"专名信号"（大写/数字/连字符）是默认行为，开关在
# `RuntimeOptions.premise_proper_only`（options.py），随实例而不是模块。
# 开启原因：旧实现会把普通动词也算进 suspects，于是 "did I catch … with Alex"
# 因为用户说的是 caught（形态不同）被误判成假前提，整题被迫拒答
# （10e09553，金标 7）。审计：非拒答题触发 91 → 36（55 题不再触发，其中
# 46 题原本就答对，9 题为错题）；拒答题 11 题触发数不变——收紧不削弱保护。


class RenderMixin:
    """由 FusionMemory 组合；所有方法依赖 self.store / self.client / self._cached。"""

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
                    sentences.append((_fmt_d(t.date), sent))
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
    def _generic_toks(self) -> frozenset[str]:
        """语言通用词 + 实例注入的领域词（P6 分层；领域词默认为空）。

        旧实现是 111 个词的模块级集合，其中混入了评测语料专属的高频普通
        名词（容器容量、居住类型、学历、课程名等）：在本题库上提高了守卫与
        实体索引的精度，换一份语料就静默失效。现在语言通用词硬编码在
        `lexicon.LANGUAGE_GENERIC`，语料词由 `options.domain_terms` 注入。
        """
        return LANGUAGE_GENERIC | self.options.domain_terms

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
        suspect_proper: dict[str, bool] = {}
        generic = self._generic_toks()
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
                has_specific = [w for w in toks if w not in generic]
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
                        # tokenize 可能折叠后缀（catch→catche），两种形态都查
                        suspect_proper[w] = bool(
                            proper_flags.get(w) or proper_flags.get(w.rstrip("e")))
        # 单词级：只认原文数字/连字符的专名（"pre-1920" 类）；存在性按 tokenize
        # 结果判——**任一部词**（"art" of "art-related"）在 user 语料即算存在。
        # v3.4.4 教训：all-部词判定被 "related/different" 这类措辞形容词卡死；
        # 连字符复合词的本质是修饰（art-related=与 art 相关），部词在即存在。
        #
        # v3.19 收紧：**数字是不可改写的硬事实**。旧规则下 "30-gallon" 会因
        # 用户说过 "20-gallon" 里含 gallon 而被判存在——数字被同义部词顶替，
        # 真正的假前提（用户没有 30 加仑的缸）反而漏报。现在含数字的部词必须
        # 原样出现，非数字部词仍按"任一存在即可"（保留 art-related 的宽松）。
        for w_raw, w in ((r, r.strip(".,?!").lower()) for r in raw_words):
            is_proper = (any(ch.isdigit() for ch in w) or "-" in w)
            if is_proper and len(w) >= 4 and w not in generic:
                sub = [t for t in tokenize(w) if t not in generic]
                numeric = [t for t in sub if any(ch.isdigit() for ch in t)]
                if numeric:
                    exists = all(t in user_toks for t in numeric)
                else:
                    exists = bool(sub) and any(t in user_toks for t in sub)
                if exists:
                    continue
                if w not in suspects:
                    suspects.append(w)
                    suspect_proper[w] = True
        if self.options.premise_proper_only:
            suspects = [w for w in suspects if suspect_proper.get(w, False)]
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
