"""词表分层（原则 P6）：语言通用词可硬编码，领域词按数据集注入。

判据：**删掉它、换一个数据集还能不能工作**。

- `LANGUAGE_GENERIC`：代词、助动词、疑问词、量词、时间单位、通用指代
  （thing / item / stuff）——任何英文语料都成立，可以硬编码。
- 领域词（tank / gallon / apartment / undergrad / course / project …）
  默认**不在这里**，由 `RuntimeOptions.domain_terms` 按数据集注入。

为什么要把它们分开：这些词原来混在 `_GENERIC_TOKS` 里，在 LongMemEval 上
提高了守卫与实体索引的精度，但换一份语料就会静默失效——同一条规则在
新数据上悄悄变松或变紧，而没有任何报错。分层之后，默认行为对语料中立，
评测集专属的词汇必须显式传入，读代码的人一眼能看出这笔账。
"""
from __future__ import annotations

# 语言通用：只含任何英文对话都成立的功能词与通用指代。
LANGUAGE_GENERIC = frozenset({
    "a", "am", "an", "and", "are", "at", "can", "could", "current",
    "day", "days", "did", "different", "do", "does",
    "first", "for", "friend", "friends", "had", "has", "have",
    "how", "i", "in", "is", "item", "items", "just", "kind",
    "lead", "leading", "least", "less", "live", "living", "local", "long",
    "many", "me", "month", "months", "more", "most", "much", "my", "new",
    "of", "often", "on", "or", "our", "people", "person", "play", "playing",
    "related", "sort", "start", "started", "stuff",
    "the", "thing", "things", "time", "times", "to", "type", "user",
    "was", "we", "week", "weeks", "were", "what", "when", "where", "which",
    "who", "will", "with", "would", "year", "years", "you", "your",
})

# 领域词默认空。评测某个数据集时，把该语料的高频普通名词显式传入，
# 不要写回 LANGUAGE_GENERIC——那是两层不同的东西。
DOMAIN_TERMS: frozenset[str] = frozenset()
