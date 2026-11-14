"""多轮评测的方差汇总（判分只读，不调用任何模型）。

为什么单独写：`report_split.py` 只看一轮，`pair_report.py` 只比两轮。
对外报数需要回答两个问题——"这个总分换一次采样还成立吗"、"波动是
答案漂移还是判分漂移"。本脚本把同一版本的 N 轮判分明细放在一起，给出

1. 逐轮总分 / dev / test / 拒答子集；
2. 分题型的均值与极差（跨轮）；
3. 题级稳定度：稳定对、稳定错、摇摆（并列出摇摆题号）；
4. 两两一致率：区分「同一批 hypothesis 多次判分」（判分噪声）
   与「重新作答再判分」（端到端噪声）。

用法::

    python variance.py results/v320-judged.jsonl results/r2-judged.jsonl ...
    python variance.py --json results/variance.json results/*-judged.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT), str(ROOT / "memory_sys")):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import load_instances  # noqa: E402
from splits import assign  # noqa: E402

DEFAULT_DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


def load(path: str | pathlib.Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["question_id"]] = r
    return out


def pct(k: int, n: int) -> float:
    return 100.0 * k / n if n else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("judged", nargs="+", help="判分明细 JSONL（每轮一个）")
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--json", default="", help="把汇总写成 JSON")
    ap.add_argument("--flaky-limit", type=int, default=20)
    args = ap.parse_args()

    insts = load_instances(args.data)
    split_of = assign(insts)
    rounds = [(pathlib.Path(p).stem.replace("-judged", ""), load(p))
              for p in args.judged]
    ids = sorted(set().union(*[set(r) for _t, r in rounds]))
    n = len(ids)
    print(f"轮次 {len(rounds)}  题目 {n}（并集）")
    print()

    # ---- 1. 逐轮 ----
    print("| 轮次 | Overall | dev | test | 拒答(_abs) | judge 错误 |")
    print("|---|---|---|---|---|---|")
    per_round = {}
    for tag, rows in rounds:
        ok = sum(1 for i in ids if rows.get(i, {}).get("label"))
        dev = [i for i in ids if split_of.get(i) == "dev"]
        tst = [i for i in ids if split_of.get(i) == "test"]
        dev_ok = sum(1 for i in dev if rows.get(i, {}).get("label"))
        tst_ok = sum(1 for i in tst if rows.get(i, {}).get("label"))
        abs_ids = [i for i in ids if i.endswith("_abs")]
        abs_ok = sum(1 for i in abs_ids if rows.get(i, {}).get("label"))
        errs = sum(1 for i in ids if rows.get(i, {}).get("judge_error"))
        per_round[tag] = {
            "overall": pct(ok, n), "correct": ok, "n": n,
            "dev": pct(dev_ok, len(dev)), "dev_correct": dev_ok,
            "dev_n": len(dev),
            "test": pct(tst_ok, len(tst)), "test_correct": tst_ok,
            "test_n": len(tst),
            "abstention": pct(abs_ok, len(abs_ids)),
            "abstention_correct": abs_ok, "abstention_n": len(abs_ids),
            "judge_errors": errs,
        }
        print(f"| {tag} | {pct(ok, n):.2f} ({ok}/{n}) | "
              f"{pct(dev_ok, len(dev)):.2f} | {pct(tst_ok, len(tst)):.2f} | "
              f"{pct(abs_ok, len(abs_ids)):.2f} | {errs} |")

    vals = [v["overall"] for v in per_round.values()]
    tvals = [v["test"] for v in per_round.values()]
    dvals = [v["dev"] for v in per_round.values()]
    print()
    print(f"Overall  mean {statistics.mean(vals):.2f}  "
          f"sd {statistics.stdev(vals):.2f}  "
          f"range {min(vals):.2f}–{max(vals):.2f}")
    print(f"test     mean {statistics.mean(tvals):.2f}  "
          f"sd {statistics.stdev(tvals):.2f}  "
          f"range {min(tvals):.2f}–{max(tvals):.2f}")
    print(f"dev      mean {statistics.mean(dvals):.2f}  "
          f"sd {statistics.stdev(dvals):.2f}  "
          f"range {min(dvals):.2f}–{max(dvals):.2f}")

    # ---- 2. 分题型 ----
    types = sorted({rows[i].get("question_type", "?")
                    for _t, rows in rounds for i in rows})
    print()
    print("| 题型 | 轮数 | mean | min–max |")
    print("|---|---|---|---|")
    per_type_summary = {}
    for t in types:
        tids = [i for i in ids if rounds[0][1].get(i, {}).get("question_type") == t
                or any(r.get(i, {}).get("question_type") == t for _tg, r in rounds)]
        accs = []
        for _tag, rows in rounds:
            sub = [i for i in tids if i in rows]
            if sub:
                accs.append(pct(sum(1 for i in sub if rows[i].get("label")), len(sub)))
        if accs:
            per_type_summary[t] = {
                "n": len(tids), "mean": round(statistics.mean(accs), 2),
                "min": round(min(accs), 2), "max": round(max(accs), 2),
            }
            print(f"| {t} | {len(accs)} | {statistics.mean(accs):.2f} | "
                  f"{min(accs):.2f}–{max(accs):.2f} |")

    # ---- 3. 题级稳定度 ----
    stable_ok, stable_bad, flaky = [], [], []
    for i in ids:
        labels = [bool(rows.get(i, {}).get("label")) for _t, rows in rounds
                  if i in rows]
        if all(labels):
            stable_ok.append(i)
        elif not any(labels):
            stable_bad.append(i)
        else:
            flaky.append(i)
    print()
    print(f"题级稳定度（{len(rounds)} 轮）：稳定对 {len(stable_ok)}"
          f"（{pct(len(stable_ok), n):.1f}%）  稳定错 {len(stable_bad)}"
          f"（{pct(len(stable_bad), n):.1f}%）  摇摆 {len(flaky)}"
          f"（{pct(len(flaky), n):.1f}%）")
    if flaky:
        shown = flaky[: args.flaky_limit]
        print(f"摇摆题（前 {len(shown)}/{len(flaky)}）：" + ", ".join(shown))
    # 摇摆题的理论影响：每轮最多 n 题的差距来自这里
    print(f"摇摆题对单轮总分的最大影响：±{pct(len(flaky) / 2, n):.2f}pp"
          f"（若随机一半判对）")

    # ---- 4. 两两一致率 ----
    print()
    print("| A | B | 共同题 | 同判 | 一致率 |")
    print("|---|---|---|---|---|")
    pair = {}
    tags = [t for t, _ in rounds]
    for a in range(len(rounds)):
        for b in range(a + 1, len(rounds)):
            ta, ra = rounds[a]
            tb, rb = rounds[b]
            common = sorted(set(ra) & set(rb))
            same = sum(1 for i in common
                       if bool(ra[i].get("label")) == bool(rb[i].get("label")))
            pair[f"{ta} vs {tb}"] = {
                "common": len(common), "same": same,
                "agreement": round(pct(same, len(common)), 2),
            }
            print(f"| {ta} | {tb} | {len(common)} | {same} | "
                  f"{pct(same, len(common)):.2f}% |")

    if args.json:
        blob = {
            "rounds": tags, "n": n, "per_round": per_round,
            "mean": {
                "overall": round(statistics.mean(vals), 2),
                "overall_sd": round(statistics.stdev(vals), 2),
                "test": round(statistics.mean(tvals), 2),
                "test_sd": round(statistics.stdev(tvals), 2),
                "dev": round(statistics.mean(dvals), 2),
                "dev_sd": round(statistics.stdev(dvals), 2),
            },
            "per_type": per_type_summary,
            "stability": {
                "stable_correct": len(stable_ok), "stable_wrong": len(stable_bad),
                "flaky": len(flaky), "flaky_ids": flaky,
            },
            "pairwise": pair,
        }
        p = pathlib.Path(args.json)
        p.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {p}")


if __name__ == "__main__":
    main()
