"""纯 Python BM25 与轻量分词（无第三方依赖）。

用途（对应设计文档 §8.5 迭代方向 #2）：
- 写入期：新候选事实 vs 已有事实的相似检索（Mem0 式写入期对齐的检索底座）
- 查询期：topic → 事实句子 / 槽位代表句 / 证据轮次的模糊召回（精确键匹配失败时的回退）

设计约束：Oracle 档每题记忆规模在千级 token，纯 Python 完全够用；
换 S/M 档时只需把本模块替换为向量库实现，接口不变。
"""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_SPLIT = re.compile(r"[a-z0-9]+")

# 极简停用词：只挡高频虚词，保留领域名词
_STOP = {
    "a", "an", "the", "my", "me", "i", "we", "our", "you", "your", "he",
    "she", "it", "they", "them", "is", "are", "was", "were", "be", "been",
    "am", "do", "does", "did", "have", "has", "had", "will", "would", "can",
    "could", "to", "of", "for", "in", "on", "at", "by", "with", "from",
    "and", "or", "but", "so", "if", "then", "than", "as", "that", "this",
    "these", "those", "there", "here", "what", "which", "who", "how", "when",
    "some", "any", "just", "also", "very", "about", "into", "over", "after",
}


# 词形归一：跨词根的同义对（born/gave birth、kid/child 这类评测常见表述差）
_SYNONYM = {
    "birth": "born", "births": "born", "gave": "give",
    "kids": "kid", "children": "child", "childrens": "child",
    "bought": "buy", "purchased": "buy", "got": "get",
    "visited": "visit", "attended": "attend", "watched": "watch",
    "moved": "move", "relocated": "move",
}


def tokenize(text: str) -> list[str]:
    """小写分词 + 停用词过滤 + 轻量后缀折叠（-ies→y，-s）+ 同义词归并。"""
    out: list[str] = []
    for w in _TOKEN_SPLIT.findall(str(text).lower()):
        if w in _STOP or len(w) < 2:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and len(w) > 3 and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        w = _SYNONYM.get(w, w)
        out.append(w)
    return out


class BM25:
    """Okapi BM25。docs 传 token 列表（由 tokenize 产出）。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs)
        self.tfs: list[Counter] = [Counter(d) for d in docs]
        self.dls = [len(d) for d in docs]
        self.avgdl = (sum(self.dls) / self.N) if self.N else 0.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        self.idf = {
            w: math.log(1.0 + (self.N - c + 0.5) / (c + 0.5))
            for w, c in df.items()
        }

    def score(self, query_tokens: list[str]) -> list[float]:
        out = [0.0] * self.N
        if not self.N:
            return out
        for w in query_tokens:
            idf = self.idf.get(w)
            if idf is None:
                continue
            for i, tf in enumerate(self.tfs):
                f = tf.get(w, 0)
                if not f:
                    continue
                dl = self.dls[i] or 1
                norm = 1 - self.b + self.b * dl / (self.avgdl or 1.0)
                out[i] += idf * f * (self.k1 + 1) / (f + self.k1 * norm)
        return out

    def topk(self, query: str, k: int = 10,
             min_score: float = 0.0) -> list[tuple[int, float]]:
        """返回 [(idx, score)]，按分数降序。query 传原文，内部分词。"""
        scores = self.score(tokenize(query))
        pairs = [(i, s) for i, s in enumerate(scores) if s > min_score]
        pairs.sort(key=lambda x: -x[1])
        return pairs[:k]
