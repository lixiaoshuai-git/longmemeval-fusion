"""读取侧确定性守卫单测（fusion._premise_check / _ordinal_lookup）。

不经过 read()/LLM——手工构造 store 后直调方法。
语义锚点（v3.4.5 / v3.5-B）：事实性疑问的专名前提必须由 user 自己的
发言支撑；请求类问句豁免；序数查找是纯文本定位。
"""
from datetime import datetime

from memory_sys.fusion import FusionMemory


def mem_with(turns):
    m = FusionMemory(client=object())
    m.store.add_evidence("s1", datetime(2023, 5, 1), turns)
    return m


# ---------------- PREMISE CHECK ----------------

def test_premise_flags_unsupported_proper_noun():
    # assistant 闲聊提及不算（本 store 只有 user 发言，且无 Shinjuku）
    m = mem_with([{"role": "user",
                   "content": "I live in an apartment in Harajuku."}])
    out = m._premise_check(
        "How long have I been living in my current apartment in Shinjuku?")
    assert out and "PREMISE CHECK" in out and "shinjuku" in out


def test_premise_silent_when_supported():
    m = mem_with([{"role": "user",
                   "content": "I live in an apartment in Harajuku."}])
    assert m._premise_check(
        "How long have I been living in my current apartment in Harajuku?") is None


def test_premise_skips_request_questions():
    # v3.4.4 教训：请求/推荐类问句被误杀 → SS-P 83→57
    m = mem_with([{"role": "user", "content": "I like cooking on weekends."}])
    assert m._premise_check("Any advice on a slow cooker for a beginner?") is None
    assert m._premise_check("Can you recommend a good board game?") is None


def test_premise_flags_numeric_attribute():
    # "30-gallon" 类数字属性是 _abs 的典型假前提
    m = mem_with([{"role": "user", "content": "I keep fish in a tank at home."}])
    out = m._premise_check("How many fish can my 30-gallon tank hold?")
    assert out and "PREMISE CHECK" in out


def test_premise_tolerates_wording_paraphrase():
    # acquire↔bought 是措辞改写不是假前提（v3.4.3 教训）
    m = mem_with([{"role": "user",
                   "content": "I got a peace lily and a succulent from the nursery."}])
    assert m._premise_check("How many plants did I buy in total?") is None


# ---------------- ORDINAL LOOKUP ----------------

def test_ordinal_absolute_hits_verbatim():
    content = "\n".join(f"{i}. parameter-{i} handles mode {i}" for i in range(1, 30))
    m = mem_with([{"role": "assistant", "content": content}])
    out = m._ordinal_lookup("What does the 27th parameter do?")
    assert out and "ORDINAL LOOKUP" in out and "parameter-27" in out


def test_ordinal_relative_after_n():
    m = mem_with([{"role": "assistant",
                   "content": "27. move the rook\n28. castle the king"}])
    out = m._ordinal_lookup("What is the move after 27?")
    assert out and "castle the king" in out and "28" in out


def test_ordinal_miss_returns_none():
    m = mem_with([{"role": "assistant", "content": "no numbered lists here"}])
    assert m._ordinal_lookup("What is the 27th parameter?") is None
