"""对照两版抽取提示词的事实集（P5 去泄漏的验收工具）。

用法：
    python compare_extraction.py --old ../results/llm_io_before_l1l3.py --n 6

做法：同一批 user 轮次，分别用「旧提示词快照」与「当前提示词」各抽一次，
逐题比较事实句集合。**只看抽取，不跑关系分类/作答**，所以调用量约为全量
重建的 1/N，几十分钟内能给出「改写是否伤抽取」的结论。

判据（事先约定）：
- 事实句精确一致率 ≥ 85%，且没有整题丢事实 → 可以进入全量重建；
- 出现系统性漏抽（某类数字/专名连续丢失）→ 先改提示词再重测。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT), str(ROOT / "memory_sys")):
    if p not in sys.path:
        sys.path.insert(0, p)

import write_path  # noqa: E402
from fusion import FusionMemory  # noqa: E402
from lme_core import LLMClient, load_env, load_instances, parse_dt  # noqa: E402
from textutil import _looks_zh  # noqa: E402
from bm25 import tokenize  # noqa: E402

DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


def load_old_prompts(path: pathlib.Path) -> tuple[str, str]:
    spec = importlib.util.spec_from_file_location("llm_io_old", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod.TURN_EXTRACTION_PROMPT, mod.ASSISTANT_FACTS_PROMPT


def extract_all(mem, inst, client) -> list[str]:
    """逐 user 轮次抽取，返回事实句列表（不落库、不判关系）。"""
    facts: list[str] = []
    for sess, date in zip(inst.sessions, inst.session_dates):
        sdate = parse_dt(date) or parse_dt("2023-01-01")
        for i, t in enumerate(sess):
            if t.get("role") != "user":
                continue
            nxt = sess[i + 1] if i + 1 < len(sess) and sess[i + 1].get("role") == "assistant" else None
            mem.cache.clear()
            for d in mem._extract_turn(t, nxt, sdate):
                f = str(d.get("fact", "")).strip()
                if f:
                    facts.append(f)
    return facts


def _fuzzy_cover(src: list[str], dst: list[str], thr: float = 0.6) -> tuple[float, list[str]]:
    """src 里有多少条能在 dst 找到近义匹配（词元 Jaccard ≥ thr）。"""
    if not src:
        return 1.0, []
    dst_sets = [set(tokenize(x)) for x in dst]
    hit, miss = 0, []
    for f in src:
        s_toks = set(tokenize(f))
        best = max((len(s_toks & d) / max(1, len(s_toks | d)) for d in dst_sets),
                   default=0.0)
        if best >= thr:
            hit += 1
        else:
            miss.append(f)
    return hit / len(src), miss


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="旧提示词模块快照（.py）")
    ap.add_argument("--n", type=int, default=6, help="抽样题数（按题型各取一题）")
    ap.add_argument("--ids", default="", help="指定 question_id，逗号分隔")
    ap.add_argument("--out", default="")
    ap.add_argument("--control", action="store_true",
                    help="对照噪声：旧提示词跑两遍，测同提示词的重采样一致性")
    args = ap.parse_args()

    old_turn, old_asst = load_old_prompts(pathlib.Path(args.old))
    new_turn, new_asst = write_path.TURN_EXTRACTION_PROMPT, write_path.ASSISTANT_FACTS_PROMPT
    assert old_turn != new_turn, "旧快照与当前提示词相同，无法对照"

    env = load_env()
    client = LLMClient(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"],
                       model=env["LLM_MODEL"],
                       timeout=int(env.get("LLM_TIMEOUT", 180)),
                       max_retry=int(env.get("LLM_MAX_RETRY", 4)), thinking=False)
    insts = load_instances(DATA)

    if args.ids:
        keep = {x.strip() for x in args.ids.split(",") if x.strip()}
        picked = [i for i in insts if i.question_id in keep]
    else:
        seen, picked = set(), []
        for inst in insts:                       # 每个题型取第一题
            if inst.question_type not in seen:
                seen.add(inst.question_type)
                picked.append(inst)
            if len(picked) >= args.n:
                break

    rows = []
    for inst in picked:
        mem = FusionMemory(client=client, cache={})

        write_path.TURN_EXTRACTION_PROMPT = old_turn
        write_path.ASSISTANT_FACTS_PROMPT = old_asst
        old_facts = extract_all(mem, inst, client)

        if args.control:
            new_facts = extract_all(mem, inst, client)      # 同一提示词再跑一遍
        else:
            write_path.TURN_EXTRACTION_PROMPT = new_turn
            write_path.ASSISTANT_FACTS_PROMPT = new_asst
            new_facts = extract_all(mem, inst, client)

        so, sn = set(old_facts), set(new_facts)
        overlap = len(so & sn)
        cover_old, missed = _fuzzy_cover(old_facts, new_facts)
        cover_new, _ = _fuzzy_cover(new_facts, old_facts)
        rows.append({
            "question_id": inst.question_id,
            "type": inst.question_type,
            "old_n": len(old_facts), "new_n": len(new_facts),
            "exact_overlap": overlap,
            "exact_jaccard": round(overlap / max(1, len(so | sn)), 3),
            "cover_old": round(cover_old, 3),     # 旧事实被新结果覆盖的比例
            "cover_new": round(cover_new, 3),
            "same_rate": round(cover_old, 3),     # 主指标：新提示词是否漏掉旧事实
            "missed": missed[:5],
            "only_new": sorted(sn - so)[:5],
            "old_facts": old_facts,
            "new_facts": new_facts,
        })
        print(f"{inst.question_id:<16}{inst.question_type:<26}"
              f"old={len(old_facts):>3} new={len(new_facts):>3} "
              f"cover_old={cover_old:.2f} cover_new={cover_new:.2f} "
              f"exact={rows[-1]['exact_jaccard']:.2f}")

    write_path.TURN_EXTRACTION_PROMPT = new_turn
    write_path.ASSISTANT_FACTS_PROMPT = new_asst

    avg = sum(r["same_rate"] for r in rows) / max(1, len(rows))
    print(f"\n平均 cover_old = {avg:.3f}（判据 ≥0.85 可进全量重建）")
    payload = {"mode": "control" if args.control else "old_vs_new",
               "avg_cover_old": round(avg, 3), "rows": rows}
    out = args.out or str(ROOT / "results" / "extraction-compare.json")
    pathlib.Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    print("写出", out)


if __name__ == "__main__":
    main()
