"""固定 dev / test 切分（原则 P8）。

规则：按题型分层，组内按 `md5(question_id)` 排序后交替分配，保证

- 确定性：同一批题永远得到同一划分；
- 分层：每个题型的 dev/test 数量差不超过 1；
- 不可重叠：每题只属于一侧。

用法：
    python splits.py                  # 打印各题型划分
    python splits.py --json out.json  # 导出 {"dev": [...], "test": [...]}

约定：dev 用来找问题、调参、做配对实验；test 只在需要对外报数时跑，
一旦看过 test 的错题，它就不再是 test。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

DEFAULT_DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


def _digest(qid: str) -> str:
    return hashlib.md5(qid.encode("utf-8")).hexdigest()


def assign(instances) -> dict[str, str]:
    """{question_id: 'dev'|'test'}；按题型分层后交替分配。"""
    by_type: dict[str, list[str]] = {}
    for inst in instances:
        by_type.setdefault(inst.question_type, []).append(inst.question_id)
    out: dict[str, str] = {}
    for _t, ids in sorted(by_type.items()):
        for k, qid in enumerate(sorted(ids, key=_digest)):
            out[qid] = "dev" if k % 2 == 0 else "test"
    return out


def ids_of(instances, split: str) -> set[str]:
    if split == "all":
        return {i.question_id for i in instances}
    table = assign(instances)
    return {q for q, s in table.items() if s == split}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from lme_core import load_instances
    insts = load_instances(args.data)
    table = assign(insts)
    by_type: dict[str, list[int]] = {}
    for inst in insts:
        row = by_type.setdefault(inst.question_type, [0, 0])
        row[0 if table[inst.question_id] == "dev" else 1] += 1
    print(f"{'题型':<28}{'dev':>5}{'test':>6}")
    for t, (d, te) in sorted(by_type.items()):
        print(f"{t:<28}{d:>5}{te:>6}")
    n_dev = sum(1 for v in table.values() if v == "dev")
    print(f"{'合计':<28}{n_dev:>5}{len(table) - n_dev:>6}")

    if args.json:
        payload = {
            "rule": "stratified by question_type, md5(qid) order, alternating",
            "dev": sorted(q for q, s in table.items() if s == "dev"),
            "test": sorted(q for q, s in table.items() if s == "test"),
        }
        pathlib.Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写出 {args.json}")


if __name__ == "__main__":
    main()
