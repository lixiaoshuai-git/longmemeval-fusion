"""BM25 检索底座单测（memory_sys.bm25）。"""
from memory_sys.bm25 import BM25, tokenize


def test_tokenize_folds_plural_and_stops():
    toks = tokenize("The Peace Lilies are blooming")
    assert "the" not in toks and "are" not in toks
    assert "peace" in toks and "lily" in toks        # -ies → y


def test_tokenize_synonym_merge():
    # 词形归一：bought/purchased → buy（写入期对齐的召回前提）
    assert tokenize("I bought a lamp") == ["buy", "lamp"]
    assert tokenize("purchased two lamps") == ["buy", "two", "lamp"]


def test_relevance_ranking():
    docs = [tokenize("I bought a peace lily for my room"),
            tokenize("The car engine needs repair")]
    idx = BM25(docs)
    top = idx.topk("plants I bought", k=2)
    assert top[0][0] == 0
    assert top[0][1] > 0


def test_topk_min_score_filters_noise():
    docs = [tokenize("unrelated document about cars"),
            tokenize("the peace lily sits by the window")]
    idx = BM25(docs)
    assert idx.topk("xylophone quantum", k=5, min_score=0.0) == []


def test_empty_corpus_safe():
    idx = BM25([])
    assert idx.score(["anything"]) == []
    assert idx.topk("anything") == []
