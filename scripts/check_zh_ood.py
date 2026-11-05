"""中文 OOD 小样本自动判定：按 expected_any 子串匹配，不调用 LLM judge。

中文样本量小、答案是短字符串，用官方英文 judge 反而会引入额外口径误差。
这里只做确定性的包含判定，失败样本仍保留 hyp 与 trace 供人工复盘。
"""
from __future__ import annotations

import argparse
import json
import pathlib


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--hyp", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    data = {e["question_id"]: e
            for e in json.loads(pathlib.Path(args.data).read_text(encoding="utf-8"))}
    hyps = [json.loads(l) for l in pathlib.Path(args.hyp).read_text(
        encoding="utf-8").splitlines() if l.strip()]
    rows = []
    for h in hyps:
        qid = h["question_id"]
        expected = data[qid].get("expected_any") or [str(data[qid]["answer"])]
        text = str(h.get("hypothesis", ""))
        ok = any(str(k).lower() in text.lower() for k in expected)
        rows.append({"question_id": qid, "question": data[qid]["question"],
                     "expected_any": expected, "hypothesis": text,
                     "correct": ok})
    correct = sum(r["correct"] for r in rows)
    print(f"中文 OOD: {correct}/{len(rows)} = {100 * correct / max(1, len(rows)):.1f}%")
    for r in rows:
        print(("✓" if r["correct"] else "✗"), r["question_id"],
              r["hypothesis"][:120].replace("\n", " "))
    result = {"n": len(rows), "correct": correct,
              "accuracy": round(correct / max(1, len(rows)), 4), "rows": rows}
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("报告", args.out)


if __name__ == "__main__":
    main()
