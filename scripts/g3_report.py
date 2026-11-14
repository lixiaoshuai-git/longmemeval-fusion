"""G3：在固定切分上做「基线 vs 候选」的配对比较（只读判分，不调用模型）。

背景：`docs/exit-criteria.md` 把 G3 定义为「同一 test 切分、同一 judge
下与全上下文基线配对」。此前缺的不是基线代码，而是**逐题判分明细**；
一旦两边都有多轮 `*-judged.jsonl`，比较可以完全离线完成。

口径说明（避免把两种噪声混在一起）：

- 逐轮分数：每一轮各自算准确率，报 mean ± sd（sd 为样本标准差）；
- 题级配对：把每题在 N 轮里的**正确率**作为该题的得分（0…1），
  两侧相减得到题级差；对题做 bootstrap 重采样给 95% CI。
  这样既保留「答案采样」的不确定性，也不需要把某一轮当成真值；
- McNemar：用各轮**多数票**折叠成 0/1 后做精确二项检验，只作辅助。

用法::

    python g3_report.py --base full500 full500-rerun full500-run3 full500-run4 \\
                        --cand v320 r2 r3 r4 --split test \\
                        --json ../results/g3-test.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
from collections import defaultdict
from math import comb

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT), str(ROOT / "memory_sys")):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import load_instances  # noqa: E402
from splits import assign  # noqa: E402

RESULTS = ROOT / "results"
DEFAULT_DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


def load(tag: str) -> dict[str, dict]:
    path = RESULTS / f"{tag}-judged.jsonl"
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["question_id"]] = r
    return out


def acc(rows: dict[str, dict], ids: list[str]) -> float:
    return 100.0 * sum(1 for i in ids if rows[i].get("label")) / len(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", nargs="+", required=True, help="基线各轮 tag")
    ap.add_argument("--cand", nargs="+", required=True, help="候选各轮 tag")
    ap.add_argument("--split", default="test", choices=["all", "dev", "test"])
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--bootstrap", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    insts = load_instances(args.data)
    split_of = assign(insts)
    base = {t: load(t) for t in args.base}
    cand = {t: load(t) for t in args.cand}
    common = set.intersection(*[set(d) for d in list(base.values()) + list(cand.values())])
    ids = sorted(i for i in common
                 if args.split == "all" or split_of.get(i) == args.split)
    if not ids:
        sys.exit("没有共同题，检查 tag 是否正确")

    print(f"切分 {args.split}  题目 {len(ids)}  "
          f"基线 {len(base)} 轮  候选 {len(cand)} 轮")
    print()

    b_scores = [acc(base[t], ids) for t in args.base]
    c_scores = [acc(cand[t], ids) for t in args.cand]
    b_mean, c_mean = statistics.mean(b_scores), statistics.mean(c_scores)
    print(f"基线  {b_mean:.2f} ± {statistics.stdev(b_scores):.2f}  "
          f"[{min(b_scores):.2f}–{max(b_scores):.2f}]")
    print(f"候选  {c_mean:.2f} ± {statistics.stdev(c_scores):.2f}  "
          f"[{min(c_scores):.2f}–{max(c_scores):.2f}]")
    print(f"差值  {c_mean - b_mean:+.2f}pp")

    # 题级正确率（跨轮），配对差
    b_rate = {i: sum(base[t][i].get("label", 0) for t in base) / len(base) for i in ids}
    c_rate = {i: sum(cand[t][i].get("label", 0) for t in cand) / len(cand) for i in ids}
    diffs = [c_rate[i] - b_rate[i] for i in ids]
    rng = random.Random(args.seed)
    boot = []
    for _ in range(args.bootstrap):
        s = [diffs[rng.randrange(len(diffs))] for _ in diffs]
        boot.append(100.0 * statistics.mean(s))
    boot.sort()
    lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]
    print(f"题级配对  {100 * statistics.mean(diffs):+.2f}pp  "
          f"（净 {sum(diffs):+.1f} 题 / {len(ids)}）  "
          f"95% CI {lo:+.2f}…{hi:+.2f}")
    better = sum(1 for i in ids if c_rate[i] > b_rate[i])
    worse = sum(1 for i in ids if c_rate[i] < b_rate[i])
    print(f"逐题：候选更好 {better} / 更差 {worse} / 持平 {len(ids) - better - worse}")

    def majority(rows: dict[str, dict], i: str) -> int:
        return 1 if 2 * sum(rows[t][i].get("label", 0) for t in rows) > len(rows) else 0

    bm = {i: majority(base, i) for i in ids}
    cm = {i: majority(cand, i) for i in ids}
    n01 = sum(1 for i in ids if cm[i] == 1 and bm[i] == 0)
    n10 = sum(1 for i in ids if cm[i] == 0 and bm[i] == 1)
    n = n01 + n10
    p = (2 * sum(comb(n, j) for j in range(min(n01, n10) + 1)) / 2 ** n) if n else 1.0
    print(f"McNemar（多数票）：候选独对 {n01} / 基线独对 {n10}  精确 p = {p:.4f}")
    print(f"多数票准确率：基线 {100 * sum(bm.values()) / len(ids):.2f}  "
          f"候选 {100 * sum(cm.values()) / len(ids):.2f}")

    # 分题型
    print()
    print("| 题型 | n | 基线 | 候选 | Δ |")
    print("|---|---|---|---|---|")
    by_type: dict[str, dict] = {}
    types = sorted({cand[args.cand[0]][i].get("question_type", "?") for i in ids})
    for t in types:
        sub = [i for i in ids if cand[args.cand[0]][i].get("question_type") == t]
        b = statistics.mean([acc(base[x], sub) for x in base])
        c = statistics.mean([acc(cand[x], sub) for x in cand])
        by_type[t] = {"n": len(sub), "base": round(b, 2),
                      "cand": round(c, 2), "delta": round(c - b, 2)}
        print(f"| {t} | {len(sub)} | {b:.2f} | {c:.2f} | {c - b:+.2f} |")
    abst = [i for i in ids if i.endswith("_abs")]
    if abst:
        b = statistics.mean([acc(base[x], abst) for x in base])
        c = statistics.mean([acc(cand[x], abst) for x in cand])
        by_type["_abs"] = {"n": len(abst), "base": round(b, 2),
                           "cand": round(c, 2), "delta": round(c - b, 2)}
        print(f"| _abs 拒答 | {len(abst)} | {b:.2f} | {c:.2f} | {c - b:+.2f} |")

    if args.json:
        blob = {
            "split": args.split, "n": len(ids),
            "base": {"tags": args.base, "scores": [round(x, 2) for x in b_scores],
                     "mean": round(b_mean, 2),
                     "sd": round(statistics.stdev(b_scores), 2)},
            "cand": {"tags": args.cand, "scores": [round(x, 2) for x in c_scores],
                     "mean": round(c_mean, 2),
                     "sd": round(statistics.stdev(c_scores), 2)},
            "pair": {"delta_pp": round(100 * statistics.mean(diffs), 2),
                     "ci95": [round(lo, 2), round(hi, 2)],
                     "net_questions": round(sum(diffs), 1),
                     "better": better, "worse": worse,
                     "mcnemar": {"cand_only": n01, "base_only": n10,
                                 "p_exact": round(p, 4)}},
            "per_type": by_type,
            "note": "判分只读；无 API 调用。sd 为样本标准差，CI 由题级 bootstrap 得到。",
        }
        pth = pathlib.Path(args.json)
        pth.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {pth}")


if __name__ == "__main__":
    main()
