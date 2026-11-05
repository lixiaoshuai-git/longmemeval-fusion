"""回归基线单测：15 题代表性样本的确定性上下文必须与 golden 逐字段一致。

夹具（全部在 tests/fixtures/，自包含，不依赖 ref/ 里的 15 MB 数据集）：
    golden_instances.json.gz  15 题的 question / session / date（gzip，193 KB）
    golden_cache.json         这 15 题真正命中的 340 个缓存键（203 KB）
    golden_context.json       指纹基线（plan / ctx / sections / cand / one_hop / matched）

覆盖：6 种题型 × 7 种 intent，含 1 道拒答题、1 道历史计数错题、
1 道曾出现 one-hop 跨进程漂移的题（gpt4_a1b77f9c）。回放不产生模型调用。

维护：提示词或抽取格式变更后，用 regress.py 重新生成三个夹具
（--update --dump-cache），并在 iteration-log 里注明新基线日期。
"""
from __future__ import annotations

import gzip
import json
import pathlib
from types import SimpleNamespace

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
FIELDS = ("plan", "ctx", "sections", "cand", "one_hop", "matched")


def _load():
    golden = json.loads((FIX / "golden_context.json").read_text(encoding="utf-8"))
    cache = json.loads((FIX / "golden_cache.json").read_text(encoding="utf-8"))
    with gzip.open(FIX / "golden_instances.json.gz", "rt", encoding="utf-8") as f:
        insts = [SimpleNamespace(**d) for d in json.load(f)["instances"]]
    return golden["questions"], cache, insts


def test_golden_context_has_no_drift():
    from regress import fingerprint
    from replay_trace import replay_records

    golden, cache, insts = _load()
    assert {i.question_id for i in insts} == set(golden)

    records, n_err = replay_records(insts, cache)
    assert n_err == 0, "夹具缓存不足以零调用回放（提示词版本可能已变更）"
    got = {r["question_id"]: fingerprint(r) for r in records}

    drift = {q: [f for f in FIELDS if got[q].get(f) != golden[q].get(f)]
             for q in golden}
    drift = {q: v for q, v in drift.items() if v}
    assert not drift, f"确定性上下文发生漂移：{drift}"


def test_parse_keys_match_fixture_cache():
    """drop-parse 工具靠复算缓存键工作；键算错会让工具静默失效。"""
    from regress import parse_cache_keys

    golden, cache, insts = _load()
    for inst in insts:
        keys = parse_cache_keys(inst.question, inst.question_date)
        assert any(k in cache for k in keys), f"{inst.question_id} 的 parse 键没命中"
