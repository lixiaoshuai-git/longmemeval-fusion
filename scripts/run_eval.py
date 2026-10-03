"""LongMemEval 答题流水线（生成 hypothesis JSONL，供官方 evaluate_qa.py 评分）。

用法示例::

    # 冒烟测试：前 20 题，关闭思考，全量上下文基线
    python run_eval.py --n 20 --tag smoke

    # 全量 500 题，4 并发
    python run_eval.py --workers 4 --tag full-context

输出写入 ``results/<tag>-hyp.jsonl``，每行 ``{"question_id", "hypothesis"}``。
支持断点续跑：已完成的 question_id 会被跳过。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_ROOT = HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lme_core import LLMClient, build_prompt, load_env, load_instances  # noqa: E402
from memory import build_memory  # noqa: E402

DEFAULT_DATA = HERE.parent / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"
RESULTS_DIR = HERE.parent / "results"


def stratified_sample(insts, n, seed=42):
    """按 (question_type, is_abstention) 分层抽样，保持与全集相近的构成。

    数据集是按题型排序存放的，直接取前 N 题会抽到单一题型（前 20 题全是
    temporal-reasoning），得到的准确率不能代表全集。
    """
    if n <= 0 or n >= len(insts):
        return list(insts)
    rng = random.Random(seed)
    buckets = defaultdict(list)
    for x in insts:
        buckets[(x.question_type, x.is_abstention)].append(x)

    quota = {}
    for key, items in buckets.items():
        quota[key] = min(len(items), max(1, round(n * len(items) / len(insts))))

    picked = []
    for key, k in quota.items():
        picked.extend(rng.sample(buckets[key], k))

    # 配额取整后可能多于或少于目标，用随机增删补齐
    all_left = [x for x in insts if x not in picked]
    while len(picked) > n:
        picked.pop(rng.randrange(len(picked)))
    while len(picked) < n and all_left:
        picked.append(all_left.pop(rng.randrange(len(all_left))))

    picked.sort(key=lambda x: x.question_id)
    return picked


def answer_one(inst, client: LLMClient, strategy: str, mem_kwargs: dict,
               cot: bool, max_tokens: int) -> dict:
    mem = build_memory(strategy, **mem_kwargs)
    mem.reset()
    for sess, date in zip(inst.sessions, inst.session_dates):
        mem.write(sess, date)
    context = mem.read(inst.question, inst.question_date)
    prompt = build_prompt(inst, context, cot=cot)

    t0 = time.time()
    hypothesis = client.chat(prompt, max_tokens=max_tokens)
    dt = time.time() - t0

    return {
        "question_id": inst.question_id,
        "hypothesis": hypothesis,
        "question_type": inst.question_type,
        "is_abstention": inst.is_abstention,
        "reordered": inst.reordered,
        "context_chars": len(context),
        "latency_s": round(dt, 3),
        "empty": hypothesis == "",
        "mem_meta": getattr(mem, "last_meta", None),
        "mem_stats": mem.stats() if hasattr(mem, "stats") else {},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--strategy", default="full-context")
    ap.add_argument("--tag", required=True, help="输出文件名标签")
    ap.add_argument("--n", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument("--stratified", type=int, default=0, metavar="N",
                    help="按题型分层抽样 N 题（优先于 --n，推荐用于快速验证）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--thinking", action="store_true",
                    help="开启模型思考（默认关闭，与 gpt-4o 基线更可比）")
    ap.add_argument("--cot", action="store_true", help="使用官方 CoT 提示词模板")
    ap.add_argument("--max-tokens", type=int, default=500)
    ap.add_argument("--useronly", action="store_true", help="只喂 user 轮次")
    args = ap.parse_args()

    env = load_env()
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if not env.get(k):
            sys.exit(f"缺少配置 {k}，请检查 {HERE.parent / '.env'}")

    insts = load_instances(args.data)
    if args.offset:
        insts = insts[args.offset:]
    if args.stratified:
        insts = stratified_sample(insts, args.stratified, args.seed)
    elif args.n:
        insts = insts[: args.n]

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{args.tag}-hyp.jsonl"

    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done.add(json.loads(line)["question_id"])
                except Exception:
                    pass
    todo = [x for x in insts if x.question_id not in done]

    n_reordered = sum(1 for x in insts if x.reordered)
    dist = defaultdict(int)
    for x in insts:
        dist[x.question_type + ("_abs" if x.is_abstention else "")] += 1
    print(f"策略        : {args.strategy}"
          + (f" (useronly)" if args.useronly else ""))
    print(f"模型        : {env['LLM_MODEL']}   思考: "
          f"{'开' if args.thinking else '关'}   CoT: {'开' if args.cot else '关'}")
    print(f"题目        : 选中 {len(insts)} 题，已完成 {len(done)}，待跑 {len(todo)}")
    if args.stratified:
        print(f"抽样        : 分层抽样 seed={args.seed}")
        print(f"题型构成    : " + "  ".join(
            f"{k}={v}" for k, v in sorted(dist.items())))
    print(f"时序重排    : {n_reordered} 题（原始顺序非升序，已按日期重排）")
    print(f"输出        : {out_path}")
    print("-" * 68)

    if not todo:
        print("无待跑题目。")
        return

    client = LLMClient(
        base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"],
        model=env["LLM_MODEL"],
        timeout=int(env.get("LLM_TIMEOUT", 180)),
        max_retry=int(env.get("LLM_MAX_RETRY", 4)),
        thinking=args.thinking,
    )
    if args.strategy in ("structured", "fusion"):
        cache_path = RESULTS_DIR / f"{args.tag}-{'event' if args.strategy == 'structured' else 'fusion'}-cache.json"
        event_cache = (json.loads(cache_path.read_text(encoding="utf-8"))
                       if cache_path.exists() else {})
        mem_kwargs = {"client": client, "cache": event_cache,
                      "cache_path": str(cache_path)}
    else:
        mem_kwargs = {"useronly": True} if args.useronly else {}

    t_start = time.time()
    results: list[dict] = []
    n_err = 0

    def work(inst):
        return answer_one(inst, client, args.strategy, mem_kwargs,
                          args.cot, args.max_tokens)

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(work, x): x for x in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                inst = futs[fut]
                try:
                    r = fut.result()
                    results.append(r)
                    flag = "  [EMPTY]" if r["empty"] else ""
                    print(f"[{i:>3}/{len(todo)}] {inst.question_id:<28} "
                          f"{r['latency_s']:>6.2f}s{flag}")
                except Exception as e:  # noqa: BLE001
                    n_err += 1
                    print(f"[{i:>3}/{len(todo)}] {inst.question_id:<28} "
                          f"ERROR {type(e).__name__}: {str(e)[:80]}")
    else:
        for i, inst in enumerate(todo, 1):
            try:
                r = work(inst)
                results.append(r)
                flag = "  [EMPTY]" if r["empty"] else ""
                print(f"[{i:>3}/{len(todo)}] {inst.question_id:<28} "
                      f"{r['latency_s']:>6.2f}s{flag}", flush=True)
            except Exception as e:  # noqa: BLE001
                n_err += 1
                print(f"[{i:>3}/{len(todo)}] {inst.question_id:<28} "
                      f"ERROR {type(e).__name__}: {str(e)[:80]}", flush=True)

    with out_path.open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"question_id": r["question_id"],
                                "hypothesis": r["hypothesis"]},
                               ensure_ascii=False) + "\n")

    # 明细另存，便于失败模式分析
    detail_path = RESULTS_DIR / f"{args.tag}-detail.jsonl"
    with detail_path.open("w", encoding="utf-8") as f:
        for r in sorted(results, key=lambda x: x["question_id"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 结构化/融合策略：落盘 LLM 调用缓存，重跑不重复计费
    if args.strategy in ("structured", "fusion"):
        cache_path = RESULTS_DIR / f"{args.tag}-{'event' if args.strategy == 'structured' else 'fusion'}-cache.json"
        cache_path.write_text(json.dumps(event_cache, ensure_ascii=False),
                              encoding="utf-8")

    elapsed = time.time() - t_start
    usage = client.usage.as_dict()
    lat = [r["latency_s"] for r in results] or [0]
    empties = sum(1 for r in results if r["empty"])

    print("-" * 68)
    print(f"完成        : {len(results)} 题，失败 {n_err}，空回答 {empties}")
    print(f"耗时        : {elapsed:.1f}s  平均 {elapsed / max(1, len(results)):.2f}s/题"
          f"  最慢 {max(lat):.2f}s")
    print(f"Token       : prompt {usage['prompt_tokens']:,} / "
          f"completion {usage['completion_tokens']:,}"
          f"（其中思考 {usage['reasoning_tokens']:,}）")
    print(f"hypothesis  : {out_path}")
    print(f"明细        : {detail_path}")

    if empties:
        print(f"\n[!] 有 {empties} 条空回答。若在关闭思考时仍出现，"
              f"检查 max_tokens 是否过小。")

    # 生成评测命令
    ref = pathlib.Path(args.data).name
    print(f"\n评分命令:\n  cd {HERE.parent / 'ref' / 'LongMemEval' / 'src' / 'evaluation'}")
    print(f"  python evaluate_qa.py <judge_model> {out_path} ../../data/{ref}")


if __name__ == "__main__":
    main()
