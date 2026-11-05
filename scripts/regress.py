"""上下文回归基线：把每题的确定性上下文固化成指纹，改动后一键判定漂移。

为什么需要：此前每一轮都要临时写几行 Python 去比「500/500 是否逐字相同」，
口径（是否归一空白、比哪些字段）随手改，容易自欺。这里把口径固定下来。

指纹字段（都可复现，不含任何时间/耗时字段）：
    plan      查询计划（intent / topic / entities / 时间字段）
    ctx       确定性结果的归一文本（空白折叠）
    sections  渲染出的段落标题顺序
    cand      候选事件 (日期, 事实, 分数)
    one_hop   一跳扩展条数
    matched   召回条数
    chars     上下文原始字符数（用于识别「只改格式」）

用法：
    # 建立 / 更新基线
    python regress.py --cache ../results/fusfull38-fusion-cache.json \
        --judged ../results/fusfull38-judged.jsonl \
        --golden ../results/golden-oracle500.json --update

    # 改动后判定
    python regress.py ... --golden ../results/golden-oracle500.json --check

退出码：0 = 无漂移；1 = 有语义漂移 / 题目缺失或新增；2 = 基线文件缺失。

另附两个工具（都不产生模型调用）：
    --dump-cache <path>   只导出本次回放真正命中的缓存键，生成精简夹具
    --drop-parse <path>   生成去掉 parse 键的缓存副本（配合真实评测验证解析改动）
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import pathlib
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import load_instances  # noqa: E402
from memory_sys.fusion import PROMPT_VERSIONS  # noqa: E402
from replay_trace import (DEFAULT_DATA, load_labels, replay_records,  # noqa: E402
                          select_instances)

SEMANTIC_FIELDS = ("plan", "ctx", "sections", "cand", "one_hop", "matched")


class TrackingCache(dict):
    """记录被读取过的键，用于生成「只含必要条目」的精简缓存。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hits: set[str] = set()

    def get(self, key, default=None):
        if key in self:
            self.hits.add(key)
            return self[key]
        return default


def _digest(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def fingerprint(record: dict) -> dict:
    """从一条回放记录算出稳定指纹；缺 trace 时返回 {"missing": True}。"""
    trace = record.get("trace") or {}
    if not trace.get("plan"):
        return {"missing": True}
    ex = trace.get("execution") or {}
    cands = [(c.get("date"), c.get("fact"), round(float(c.get("score") or 0), 4))
             for c in (ex.get("trace_candidates") or [])]
    return {
        "type": record.get("question_type"),
        "plan": _digest(trace.get("plan")),
        "ctx": _digest(_norm(trace.get("deterministic_result"))),
        "sections": _digest(trace.get("sections") or []),
        "cand": _digest(cands),
        "one_hop": int(ex.get("one_hop") or 0),
        "matched": int(ex.get("matched") or 0),
        "chars": int(trace.get("context_chars") or 0),
    }


def diff_one(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """返回 (语义差异字段, 仅格式差异字段)。"""
    semantic = [f for f in SEMANTIC_FIELDS if old.get(f) != new.get(f)]
    fmt = []
    if not semantic and old.get("chars") != new.get("chars"):
        fmt.append("chars")
    return semantic, fmt


def _print_ctx_diff(qid: str, old_ctx: str, new_ctx: str, limit: int = 12) -> None:
    print(f"\n  --- {qid} 的确定性结果差异（前 {limit} 行）")
    lines = list(difflib.unified_diff(
        old_ctx.splitlines(), new_ctx.splitlines(),
        fromfile="golden", tofile="current", lineterm="", n=1))
    for line in lines[:limit]:
        print("  " + line[:160])


def parse_cache_keys(question: str, question_date: str) -> list[str]:
    """复算 read() 里 parse 的缓存键（中英两个 scope 都算）。"""
    base = "parse|" + hashlib.sha1(json.dumps(
        [question, question_date], ensure_ascii=False).encode("utf-8")).hexdigest()
    return [hashlib.sha1((PROMPT_VERSIONS[scope] + "|" + base).encode("utf-8")
                         ).hexdigest()[:16]
            for scope in ("parse", "parse_zh")]


def _print_plan_audit(records: list[dict]) -> None:
    """汇总 plan_issues：按问题码计数，给出占比与示例题号。"""
    from plan import ISSUE_TEXT, entities_not_in_question

    total = len(records)
    counts: dict[str, list[str]] = {}
    stray_ents = 0
    for r in records:
        trace = r.get("trace") or {}
        for code in trace.get("plan_issues") or []:
            counts.setdefault(code, []).append(r["question_id"])
        plan = trace.get("plan") or {}
        if plan and entities_not_in_question(plan, trace.get("question") or ""):
            stray_ents += 1

    print(f"计划审计：{total} 题，有问题 {len({q for v in counts.values() for q in v})} 题")
    print(f"{'问题码':<22}{'题数':>6}{'占比':>8}  示例")
    for code, qids in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        print(f"{code:<22}{len(qids):>6}{len(qids) / max(1, total):>7.1%}  "
              f"{', '.join(qids[:3])}")
    if not counts:
        print("（无问题码）")
    print(f"\n附：entities 里有问题中未出现的词 {stray_ents} 题"
          f"（{stray_ents / max(1, total):.1%}）——上位词/假前提的近似指标")
    print("说明：" + "；".join(f"{k}={v}" for k, v in ISSUE_TEXT.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--cache", required=True)
    ap.add_argument("--judged", default="")
    ap.add_argument("--ids", default="")
    ap.add_argument("--types", default="")
    ap.add_argument("--golden", default="", help="基线文件（--update/--check 必填）")
    ap.add_argument("--update", action="store_true", help="写入 / 覆盖基线")
    ap.add_argument("--check", action="store_true", help="与基线比对")
    ap.add_argument("--dump-cache", default="", help="导出本次命中的缓存子集")
    ap.add_argument("--drop-parse", default="", help="导出删除 parse 键的缓存副本")
    ap.add_argument("--prev-trace", default="",
                    help="上一次的 trace.jsonl，用于打印第一处漂移的文本 diff")
    ap.add_argument("--audit-plans", action="store_true",
                    help="汇总查询计划的静态问题码（只诊断，不做比对）")
    args = ap.parse_args()

    if not (args.update or args.check or args.audit_plans):
        ap.error("必须指定 --update / --check / --audit-plans 之一")
    if (args.update or args.check) and not args.golden:
        ap.error("--update / --check 需要 --golden")

    insts = load_instances(args.data)
    labels = load_labels(args.judged)
    selected = select_instances(insts, labels, args.ids, args.types)

    raw_cache = json.loads(pathlib.Path(args.cache).read_text(encoding="utf-8"))
    cache = TrackingCache(raw_cache)
    records, n_err = replay_records(selected, cache, labels)
    print(f"回放 {len(selected)} 题，cache-miss/异常 {n_err}，命中缓存键 "
          f"{len(cache.hits)} 个")

    current = {r["question_id"]: fingerprint(r) for r in records}
    missing_trace = [q for q, f in current.items() if f.get("missing")]
    if missing_trace:
        print(f"警告：{len(missing_trace)} 题没有 trace：{missing_trace[:5]}")

    if args.dump_cache:
        subset = {k: raw_cache[k] for k in cache.hits if k in raw_cache}
        pathlib.Path(args.dump_cache).write_text(
            json.dumps(subset, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        size_kb = pathlib.Path(args.dump_cache).stat().st_size / 1024
        check, err2 = replay_records(
            [i for i in selected if i.question_id in current],
            dict(subset), labels)
        print(f"精简缓存 → {args.dump_cache}（{len(subset)} 键 / {size_kb:.0f} KB），"
              f"用该缓存重放：{len(check)} 题，异常 {err2}")

    if args.drop_parse:
        dropped = 0
        for inst in selected:
            keys = parse_cache_keys(inst.question, inst.question_date)
            for k in keys:
                if raw_cache.pop(k, None) is not None:
                    dropped += 1
        pathlib.Path(args.drop_parse).write_text(
            json.dumps(raw_cache, ensure_ascii=False), encoding="utf-8")
        print(f"删除 {dropped} 个 parse 键 → {args.drop_parse}"
              f"（剩余 {len(raw_cache)} 键）；下一步用 run_eval --ids 跑真实解析")

    if args.audit_plans:
        _print_plan_audit(records)
        return

    if args.update:
        payload = {
            "meta": {
                "n": len(current),
                "cache": pathlib.Path(args.cache).name,
                "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "ids": "all" if not args.ids else len(selected),
                "note": "确定性上下文指纹；不含耗时字段",
            },
            "questions": current,
        }
        out = pathlib.Path(args.golden)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"基线已写入 {out}（{len(current)} 题）")
        return

    golden_path = pathlib.Path(args.golden)
    if not golden_path.exists():
        print(f"基线文件不存在：{golden_path}", file=sys.stderr)
        raise SystemExit(2)
    old = json.loads(golden_path.read_text(encoding="utf-8"))["questions"]

    only_old = sorted(set(old) - set(current))
    only_new = sorted(set(current) - set(old))
    # 只回放子集时，基线里没被回放的题不算漂移——这是「定点复核」的常用姿势。
    subset_run = bool(args.ids.strip())
    semantic_diffs, format_diffs = [], []
    for qid in sorted(set(old) & set(current)):
        sem, fmt = diff_one(old[qid], current[qid])
        if sem:
            semantic_diffs.append((qid, sem))
        elif fmt:
            format_diffs.append((qid, fmt))

    print(f"\n比对 {len(set(old) & set(current))} 题："
          f"语义漂移 {len(semantic_diffs)} / 仅格式漂移 {len(format_diffs)} / "
          f"缺失 {len(only_old)} / 新增 {len(only_new)}")
    for qid, fields in semantic_diffs[:50]:
        print(f"  SEMANTIC {qid} [{current[qid].get('type')}] 变动字段: {fields}")
    if format_diffs[:5]:
        print(f"  FORMAT   {[q for q, _ in format_diffs[:5]]}")
    if only_old[:5]:
        label = "未回放" if subset_run else "MISSING "
        print(f"  {label}  {only_old[:5]}" + (f" 共 {len(only_old)} 题" if subset_run else ""))
    if only_new[:5]:
        print(f"  NEW      {only_new[:5]}")

    # 第一处语义漂移给出文本 diff，便于定位是召回、算子还是渲染
    if semantic_diffs and args.prev_trace:
        qid = semantic_diffs[0][0]
        prev = {}
        for line in pathlib.Path(args.prev_trace).read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                prev[row["question_id"]] = row
        rec = next((r for r in records if r["question_id"] == qid), None)
        old_text = ((prev.get(qid) or {}).get("trace") or {}).get(
            "deterministic_result") or ""
        if rec is not None and old_text:
            _print_ctx_diff(qid, old_text,
                            (rec.get("trace") or {}).get("deterministic_result") or "")

    bad = semantic_diffs or only_new or missing_trace or (only_old and not subset_run)
    if bad:
        print("\n结论：存在漂移/题目集合变化，需人工确认后才能作为基线。")
        raise SystemExit(1)
    print("\n结论：与基线一致，无语义漂移。")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
