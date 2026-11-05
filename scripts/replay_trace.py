"""零 LLM 回放：用既有 fusion 缓存重建记忆库，导出查询计划与算子轨迹。

用途：失败归因时不重复调用模型。缓存命中覆盖抽取 / assistant / 关系 / 解析
四个边界；任何一次 cache miss 都会被 NoLLM 捕获并记入 error，不会静默重算。

示例：
    python replay_trace.py --cache results/fusfull38-fusion-cache.json \
        --judged results/fusfull38-judged.jsonl --only-wrong \
        --out results/v38-wrong-trace.jsonl

注意：解析（parse）也走缓存，因此回放**不能**验证 `_parse` 的改动——它验证的是
「同一份计划 → 执行 / 渲染 / 上下文」是否漂移。要验证解析改动，先用
`regress.py --drop-parse` 生成去掉 parse 键的缓存副本，再跑真实评测。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import load_instances  # noqa: E402
from memory import build_memory  # noqa: E402

DEFAULT_DATA = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"


class NoLLM:
    """缓存未命中即报错，保证回放不产生模型调用。"""

    model = "no-llm-replay"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("cache miss during replay")


def load_labels(path: str) -> dict:
    """读取 judged.jsonl → {question_id: row}。空路径返回空字典。"""
    if not path:
        return {}
    out = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["question_id"]] = r
    return out


def select_instances(insts, labels: dict, ids: str = "", types: str = "",
                     only_wrong: bool = False, limit: int = 0) -> list:
    """按题号 / 题型 / 只错题过滤，保持原始顺序。"""
    want_ids = {x.strip() for x in ids.split(",") if x.strip()}
    want_types = {x.strip() for x in types.split(",") if x.strip()}
    selected = []
    for inst in insts:
        rec = labels.get(inst.question_id)
        if want_ids and inst.question_id not in want_ids:
            continue
        if want_types and inst.question_type not in want_types:
            continue
        if only_wrong and (rec is None or rec.get("label")):
            continue
        selected.append(inst)
    if limit:
        selected = selected[:limit]
    return selected


def replay_records(insts, cache, labels: dict | None = None,
                   progress: bool = False) -> tuple[list[dict], int]:
    """回放一组题目，返回 (records, 异常题数)。cache 可以是普通 dict 或
    带 get 钩子的映射（regress.py 用来统计实际命中的键）。"""
    labels = labels or {}
    records: list[dict] = []
    n_err = 0
    for i, inst in enumerate(insts, 1):
        client = NoLLM()
        mem = build_memory("fusion", client=client, cache=cache)
        err = None
        try:
            mem.reset()
            for sess, date in zip(inst.sessions, inst.session_dates):
                mem.write(sess, date)
            mem.read(inst.question, inst.question_date)
        except Exception as ex:  # noqa: BLE001
            n_err += 1
            err = f"{type(ex).__name__}: {ex}"
        rec = labels.get(inst.question_id, {})
        records.append({
            "question_id": inst.question_id,
            "question_type": inst.question_type,
            "label": rec.get("label"),
            "question": inst.question,
            "answer": rec.get("answer"),
            "hypothesis": rec.get("hypothesis"),
            "stats": mem.stats(),
            "trace": getattr(mem, "last_trace", None),
            "error": err,
        })
        if progress:
            flag = "ERR" if err else ("✓" if rec.get("label", True) else "✗")
            print(f"[{i:>3}/{len(insts)}] {flag} {inst.question_id} "
                  f"{inst.question_type}", flush=True)
    return records, n_err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--cache", required=True)
    ap.add_argument("--judged", default="")
    ap.add_argument("--only-wrong", action="store_true")
    ap.add_argument("--ids", default="", help="逗号分隔的 question_id")
    ap.add_argument("--types", default="", help="逗号分隔的 question_type")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "results" / "replay-trace.jsonl"))
    args = ap.parse_args()

    insts = load_instances(args.data)
    labels = load_labels(args.judged)
    selected = select_instances(insts, labels, args.ids, args.types,
                                args.only_wrong, args.limit)

    cache = json.loads(pathlib.Path(args.cache).read_text(encoding="utf-8"))
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records, n_err = replay_records(selected, cache, labels, progress=True)

    with out_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n写出 {out_path}  题目 {len(selected)}  cache-miss/异常 {n_err}")


if __name__ == "__main__":
    main()
