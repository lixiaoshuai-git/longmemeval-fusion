"""LongMemEval 评分：复用官方 judge 提示词，适配本项目的模型端点。

为什么单独写而不用官方 ``evaluate_qa.py``：

1. 官方脚本的 ``model_zoo`` 只认 gpt-4o / gpt-4o-mini / llama-3.1-70b，
   本项目用的是 glm-5.2，无法直接传入。
2. 官方脚本硬编码 ``max_tokens=10``。推理模型会把这 10 个 token 全部用于
   思考、使 ``content`` 返回空串，导致所有题被判为 no。本脚本关闭思考
   并把预算提到足够容纳一个 yes/no。

**提示词不重新实现，直接 import 官方函数**，保证与论文口径一致。

重要：自测用的 judge 模型与项目组公布的（默认 GPT-4o）不同，
正式提交的评估报告中必须披露这一差异。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OFFICIAL_EVAL_DIR = (HERE.parent / "ref" / "LongMemEval" / "src" / "evaluation")
sys.path.insert(0, str(OFFICIAL_EVAL_DIR))

from lme_core import LLMClient, load_env  # noqa: E402

from evaluate_qa import get_anscheck_prompt  # noqa: E402  官方提示词，不重写


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("hyp", help="hypothesis JSONL（run_eval.py 的产物）")
    ap.add_argument("--data", default=str(
        HERE.parent / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    env = load_env()
    ref = {e["question_id"]: e
           for e in json.load(open(args.data, encoding="utf-8"))}
    hyps = [json.loads(l) for l in open(args.hyp, encoding="utf-8") if l.strip()]
    if args.limit:
        hyps = hyps[: args.limit]

    judge = LLMClient(
        base_url=env.get("JUDGE_BASE_URL") or env["LLM_BASE_URL"],
        api_key=env.get("JUDGE_API_KEY") or env["LLM_API_KEY"],
        model=env.get("JUDGE_MODEL") or env["LLM_MODEL"],
        timeout=int(env.get("LLM_TIMEOUT", 180)),
        max_retry=int(env.get("LLM_MAX_RETRY", 4)),
        thinking=False,          # 关键：关闭思考，否则 yes/no 被判空
    )

    print(f"judge 模型 : {judge.model}（官方默认 gpt-4o，此处为代跑，需在报告中披露）")
    print(f"题目数     : {len(hyps)}")
    print("-" * 68)

    per_type: dict[str, list[int]] = defaultdict(list)
    abst: list[int] = []
    logs = []
    n_err = 0

    for i, h in enumerate(hyps, 1):
        qid = h["question_id"]
        if qid not in ref:
            print(f"[{i:>3}] {qid} 不在参考数据中，跳过")
            continue
        e = ref[qid]
        is_abs = qid.endswith("_abs")
        prompt = get_anscheck_prompt(
            e["question_type"], e["question"], e["answer"],
            h["hypothesis"], abstention=is_abs)

        try:
            resp = judge.chat(prompt, max_tokens=24)
            label = "yes" in resp.lower()
        except Exception as ex:  # noqa: BLE001
            n_err += 1
            label = False
            resp = f"<ERROR {type(ex).__name__}>"

        per_type[e["question_type"]].append(1 if label else 0)
        if is_abs:
            abst.append(1 if label else 0)
        logs.append({"question_id": qid, "question_type": e["question_type"],
                     "is_abstention": is_abs, "question": e["question"],
                     "answer": e["answer"], "hypothesis": h["hypothesis"],
                     "judge_raw": resp, "label": label})
        mark = "✓" if label else "✗"
        print(f"[{i:>3}/{len(hyps)}] {mark} {qid:<28} {e['question_type']}")

    total = [v for vs in per_type.values() for v in vs]
    overall = sum(total) / max(1, len(total))

    print()
    print("=" * 68)
    print(f"Overall Accuracy : {overall * 100:.2f}%  ({sum(total)}/{len(total)})")
    print("-" * 68)
    print(f"{'题型':<30}{'准确率':>10}{'对/总':>12}")
    for t in sorted(per_type, key=lambda k: -len(per_type[k])):
        vs = per_type[t]
        print(f"{t:<30}{sum(vs) / len(vs) * 100:>9.2f}%{sum(vs):>7}/{len(vs):<5}")
    if abst:
        print("-" * 68)
        print(f"{'拒答题 (_abs) 子集':<30}{sum(abst) / len(abst) * 100:>9.2f}%"
              f"{sum(abst):>7}/{len(abst):<5}")
    print("=" * 68)
    u = judge.usage.as_dict()
    print(f"judge token: prompt {u['prompt_tokens']:,} / completion "
          f"{u['completion_tokens']:,}  调用 {u['calls']} 次  失败 {n_err}")

    out = HERE.parent / "results" / f"{args.tag}-judged.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in logs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"明细: {out}")

    summary = {
        "tag": args.tag, "judge_model": judge.model, "n": len(total),
        "overall_accuracy": round(overall, 4),
        "per_type": {t: {"acc": round(sum(v) / len(v), 4), "n": len(v),
                         "correct": sum(v)} for t, v in per_type.items()},
        "abstention": ({"acc": round(sum(abst) / len(abst), 4), "n": len(abst)}
                       if abst else None),
        "note": "judge 为 glm-5.2 代跑，官方口径默认 GPT-4o，正式报告须披露",
    }
    sp = HERE.parent / "results" / f"{args.tag}-summary.json"
    sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"汇总: {sp}")


if __name__ == "__main__":
    main()
