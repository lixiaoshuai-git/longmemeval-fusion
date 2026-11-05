"""中文 / 真实对话 OOD 回归：确定性读路径不依赖英文分词。

注意：这里只验证「已抽取的中文事实 → BM25 → 上下文」，不验证中文抽取
prompt 本身（抽取仍是 LLM 边界，需另跑中文端到端样本）。
"""
from datetime import datetime

from memory_sys.bm25 import BM25, tokenize
from memory_sys.fusion import FusionMemory, resolve_when_expr


class FakeParseClient:
    def chat(self, *args, **kwargs):
        return ('{"intent":"agg_count","topic":"我现在有多少枚硬币",'
                '"entities":["硬币"]}')


def test_english_tokenization_is_unchanged():
    assert tokenize("I have 38 coins") == ["38", "coin"]


def test_cjk_bigram_tokens_enable_chinese_recall():
    tokens = tokenize("我现在有38枚硬币")
    assert "38" in tokens
    assert "硬币" in tokens
    docs = ["我现在有38枚硬币", "我上周买了咖啡豆"]
    idx = BM25([tokenize(d) for d in docs])
    hits = idx.topk("我现在有多少枚硬币", k=1, min_score=0.0)
    assert hits and hits[0][0] == 0


def test_fusion_read_recalls_chinese_facts():
    mem = FusionMemory(client=FakeParseClient(), cache={})
    mem.store.add_event("user", "event", "我现在有38枚硬币", "money",
                        datetime(2023, 5, 1), None, False, None, "",
                        ("s1", 0), stated_at=datetime(2023, 5, 1))
    context = mem.read("我现在有多少枚硬币？", "2023-06-01")
    assert "硬币" in context
    assert mem.last_trace["execution"]["trace_candidates"]


def test_chinese_relative_time_rules():
    d = datetime(2023, 6, 15)
    assert resolve_when_expr("两周前", d) == (datetime(2023, 6, 1),
                                             datetime(2023, 6, 2))
    assert resolve_when_expr("上个月", d) == (datetime(2023, 5, 1),
                                             datetime(2023, 6, 1))
    assert resolve_when_expr("2023年5月1日", d) == (datetime(2023, 5, 1),
                                                   datetime(2023, 5, 2))
    assert resolve_when_expr("三个月前", d) == (datetime(2023, 3, 15),
                                               datetime(2023, 3, 16))


def test_english_time_rules_still_win():
    d = datetime(2023, 6, 15)
    assert resolve_when_expr("two weeks ago", d) == (datetime(2023, 6, 1),
                                                    datetime(2023, 6, 2))
    assert resolve_when_expr("last month", d) == (datetime(2023, 5, 1),
                                                  datetime(2023, 6, 1))


def test_language_routing_and_prompt_isolation():
    from memory_sys.fusion import FusionMemory, _looks_zh
    from lme_core import build_prompt
    from types import SimpleNamespace

    assert _looks_zh("我现在有多少枚硬币？")
    assert not _looks_zh("How many coins do I have?")
    en = build_prompt(SimpleNamespace(question="How many coins?",
                                      question_date="2023-06-01"), "ctx")
    zh = build_prompt(SimpleNamespace(question="我现在有多少枚硬币？",
                                      question_date="2023-06-01"), "ctx")
    assert en.startswith("I will give you")
    assert zh.startswith("下面给你")

    class Recorder:
        def __init__(self):
            self.prompts = []

        def chat(self, prompt, *args, **kwargs):
            self.prompts.append(prompt)
            return '{"intent":"agg_count","topic":"硬币"}'

    rec = Recorder()
    mem = FusionMemory(client=rec, cache={})
    mem._parse("我现在有多少枚硬币？", datetime(2023, 6, 1), zh=True)
    mem._parse("How many coins?", datetime(2023, 6, 1), zh=False)
    assert "把问题转换成" in rec.prompts[0]
    assert "Convert the question" in rec.prompts[1]

    cache = {}
    mem2 = FusionMemory(client=Recorder(), cache=cache)
    assert mem2._cached("same", lambda: "en", scope="extract") == "en"
    assert mem2._cached("same", lambda: "zh", scope="extract_zh") == "zh"
    assert len(cache) == 2
