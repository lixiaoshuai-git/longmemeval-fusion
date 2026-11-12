"""按 dev / test 切分汇总一次评测（P8：dev 找问题，test 只报数）。

用法：
    python report_split.py --judged ../results/v320-judged.jsonl
    python report_split.py --judged ... --json ../results/v320-split.json

只读已判分的明细，不调用任何模型。输出：总体、dev、test、分题型，
以及两段之间的差值（用于判断结果是否被某一段主导）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT), str(ROOT / "memory_sys")):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import load_instances  # noqa: E402
from splits import assign  # noqa: E402

DEFAULT_DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


def _acc(rows: list[dict]) -> tuple[int, int, float]:
    n = len(rows)
    ok = sum(1 for r in rows if r.get("label"))
    return ok, n, (ok / n if n else 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judged", required=True)
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    judged = [json.loads(l) for l in
              pathlib.Path(args.judged).read_text(encoding="utf-8").splitlines() if l]
    insts = load_instances(args.data)
    table = assign(insts)
    by_id = {r["question_id"]: r for r in judged}

    split_rows = {"dev": [], "test": []}
    for inst in insts:
        r = by_id.get(inst.question_id)
        if r is not None:
            split_rows[table[inst.question_id]].append(r)

    ok, n, acc = _acc(judged)
    print(f"{'集合':<8}{'正确':>6}{'题数':>6}{'准确率':>9}")
    print(f"{'全部':<8}{ok:>6}{n:>6}{acc:>8.2%}")
    payload = {"overall": {"ok": ok, "n": n, "acc": round(acc, 4)}, "splits": {}}
    for name in ("dev", "test"):
        o, m, a = _acc(split_rows[name])
        print(f"{name:<8}{o:>6}{m:>6}{a:>8.2%}")
        payload["splits"][name] = {"ok": o, "n": m, "acc": round(a, 4)}

    d = payload["splits"]["dev"]["acc"] - payload["splits"]["test"]["acc"]
    print(f"\ndev - test = {d:+.2%}（仅作观察；test 只报数，不据此调参）")

    print("\n分题型（全部 / dev / test）:")
    types: dict[str, dict[str, list[dict]]] = {}
    for name, rows in split_rows.items():
        for r in rows:
            types.setdefault(r["question_type"], {"dev": [], "test": []})[name].append(r)
    payload["per_type"] = {}
    for t, groups in sorted(types.items()):
        parts = []
        for name in ("dev", "test"):
            o, m, a = _acc(groups[name])
            parts.append(f"{a:.0%}({m})" if m else "-")
        print(f"  {t:<30}dev {parts[0]:>10}   test {parts[1]:>10}")
        payload["per_type"][t] = {
            "dev": {"n": len(groups["dev"]),
                    "acc": round(_acc(groups["dev"])[2], 4) if groups["dev"] else None},
            "test": {"n": len(groups["test"]),
                     "acc": round(_acc(groups["test"])[2], 4) if groups["test"] else None},
        }

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("写出", args.json)


if __name__ == "__main__":
    main()
