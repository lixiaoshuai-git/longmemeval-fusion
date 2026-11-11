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


def test_ordinal_relative_after_n_rolled_back():
    # v3.5-B 相对序数已回滚（三轮无正证据）——锁定回滚状态防回归复现
    m = mem_with([{"role": "assistant",
                   "content": "27. move the rook\n28. castle the king"}])
    assert m._ordinal_lookup("What is the move after 27?") is None


def test_ordinal_miss_returns_none():
    m = mem_with([{"role": "assistant", "content": "no numbered lists here"}])
    assert m._ordinal_lookup("What is the 27th parameter?") is None


# ---------------- 形态差异不是假前提（infra-2026-11 G2） ----------------

def test_premise_ignores_inflected_verb():
    """用户说 caught，问题说 catch：旧实现会当成假前提并强迫拒答。"""
    m = mem_with([{"role": "user",
                   "content": "I caught 7 largemouth bass with Alex at Lake Michigan "
                              "on the earlier trip."}])
    assert m._premise_check(
        "How many largemouth bass did I catch with Alex at Lake Michigan?") is None


def test_premise_still_flags_absent_proper_noun_in_same_gram():
    """收紧后仍要抓真专名：Jordan 没出现过。"""
    m = mem_with([{"role": "user",
                   "content": "I caught 7 largemouth bass with Alex at Lake Michigan."}])
    out = m._premise_check(
        "How many largemouth bass did I catch with Jordan at Lake Michigan?")
    assert out and "jordan" in out


# ---------------- SCOPE NOTE 不再强制拒答（infra-2026-11 H2） ----------------

class _PlanClient:
    """只回答 parse：给出一个记忆里不存在的实体。"""

    model = "fake"

    def chat(self, prompt, *args, **kwargs):
        return ('{"intent": "agg_count", "topic": "siblings", '
                '"entities": ["siblings"], "category": "person"}')


def test_scope_note_asks_for_paraphrase_not_refusal():
    m = FusionMemory(client=_PlanClient(), cache={})
    m.store.add_event("user", "state", "I have one brother and three sisters.",
                      "person", datetime(2023, 5, 1), None, False, None, "",
                      ("s1", 0), stated_at=datetime(2023, 5, 1))
    ctx = m.read("How many siblings do I have?", "2023-06-01")
    assert "MEMORY SCOPE NOTE" in ctx
    assert "paraphrase" in ctx            # 先找同义表达
    assert "not enough" in ctx            # 真正缺失时才用拒答措辞
    assert "MUST answer" not in ctx       # 不再强制拒答


# ---------------- 数字是不可改写的硬事实（v3.19） ----------------

def test_premise_flags_numeric_compound_with_wrong_number():
    """用户有 20-gallon，问题问 30-gallon：数字必须原样出现，不能被 gallon 顶替。"""
    m = mem_with([{"role": "user",
                   "content": "I keep 10 neon tetras in my 20-gallon tank."}])
    out = m._premise_check("How many fish are in my 30-gallon tank?")
    assert out and "30-gallon" in out


def test_premise_allows_hyphenated_modifier_paraphrase():
    """art-related 这类修饰词只要部词出现过就算存在（v3.4.4 的宽松保留）。"""
    m = mem_with([{"role": "user",
                   "content": "I bought some art prints for the living room."}])
    assert m._premise_check(
        "Where did I put the art-related purchases?") is None


def test_premise_allows_same_number_hyphenated_compound():
    m = mem_with([{"role": "user",
                   "content": "I keep 10 neon tetras in my 20-gallon tank."}])
    assert m._premise_check("How many fish are in my 20-gallon tank?") is None
