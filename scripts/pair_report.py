"""两次评分结果的配对报告（迭代纪律自动化）。

输出：共同题目上的 base/cand 正确数、净变化、修好/修坏题号、
分题型变化；若 base 是全集而 cand 是子集，给出把子集替换后的
预计全量准确率。judge 调用失败单独计数，不冒充答错。

用法：
    python pair_report.py results/base-judged.jsonl results/cand-judged.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict


def load(path: str | pathlib.Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["question_id"]] = r
    return out


def compare(base: dict[str, dict], cand: dict[str, dict]) -> dict:
    ids = sorted(set(base) & set(cand))
    base_ok = [i for i in ids if bool(base[i].get("label"))]
    cand_ok = [i for i in ids if bool(cand[i].get("label"))]
    fix = sorted(set(cand_ok) - set(base_ok))
    brk = sorted(set(base_ok) - set(cand_ok))
    per_type: dict[str, dict] = defaultdict(lambda: {"n": 0, "base": 0, "cand": 0})
    for i in ids:
        t = base[i].get("question_type", cand[i].get("question_type", "?"))
        per_type[t]["n"] += 1
        per_type[t]["base"] += int(bool(base[i].get("label")))
        per_type[t]["cand"] += int(bool(cand[i].get("label")))

    result = {
        "n": len(ids),
        "base_correct": len(base_ok),
        "cand_correct": len(cand_ok),
        "delta": len(cand_ok) - len(base_ok),
        "delta_pp": round(100 * (len(cand_ok) - len(base_ok)) / max(1, len(ids)), 2),
        "fix": fix,
        "break": brk,
        "judge_errors": {
            "base": sum(1 for r in base.values() if r.get("judge_error")),
            "cand": sum(1 for r in cand.values() if r.get("judge_error")),
        },
        "per_type": dict(per_type),
    }
    if len(base) > len(ids):
        all_base_ok = sum(1 for r in base.values() if bool(r.get("label")))
        projected = all_base_ok - len(base_ok) + len(cand_ok)
        result["projected_full"] = {
            "n": len(base),
            "base_correct": all_base_ok,
            "candidate_correct": projected,
            "accuracy": round(projected / len(base), 4),
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    r = compare(load(args.base), load(args.cand))
    print(f"配对题目 {r['n']}  基线 {r['base_correct']}  候选 {r['cand_correct']}  "
          f"净变化 {r['delta']:+d} ({r['delta_pp']:+.2f}pp)")
    print(f"修好 {len(r['fix'])}: {', '.join(r['fix']) or '-'}")
    print(f"修坏 {len(r['break'])}: {', '.join(r['break']) or '-'}")
    print(f"judge 错误 base={r['judge_errors']['base']} cand={r['judge_errors']['cand']}")
    print("-" * 68)
    for t, v in sorted(r["per_type"].items()):
        print(f"{t:<30} {v['base']:>3}/{v['n']:<3} -> {v['cand']:>3}/{v['n']:<3}  "
              f"{v['cand'] - v['base']:+d}")
    if "projected_full" in r:
        p = r["projected_full"]
        print("-" * 68)
        print(f"预计全量 {p['candidate_correct']}/{p['n']} = {p['accuracy'] * 100:.2f}%")
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"报告 {args.out}")


if __name__ == "__main__":
    main()
