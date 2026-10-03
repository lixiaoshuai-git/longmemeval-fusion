"""对比 Oracle / S / M 三档的规模差异。

token 统计默认只抽样 N 题——S 档全量约 5700 万 token，全部编码耗时过长。

用法::

    python compare_tiers.py --oracle <path> --s <path> [--m <path>] [--sample 50]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics as st
import sys
from collections import Counter
from datetime import datetime

import tiktoken

sys.stdout.reconfigure(encoding="utf-8")
ENC = tiktoken.get_encoding("o200k_base")
_DOW = re.compile(r"\([A-Za-z]{3}\)\s*")


def parse_dt(s):
    s = _DOW.sub("", str(s)).strip()
    for f in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def analyse(path, label, sample_n=50):
    data = json.load(open(path, encoding="utf-8"))
    n = len(data)

    sess, turns, spans = [], [], []
    ev_ratio = []
    for e in data:
        ss = e["haystack_sessions"]
        sess.append(len(ss))
        t = sum(len(x) for x in ss)
        turns.append(t)
        ev = sum(1 for x in ss for y in x if y.get("has_answer"))
        if t:
            ev_ratio.append(ev / t)
        ds = [parse_dt(d) for d in e["haystack_dates"]]
        ds = [d for d in ds if d]
        if len(ds) >= 2:
            spans.append((max(ds) - min(ds)).days)

    # token：抽前 sample_n 题
    step = max(1, n // sample_n)
    toks = []
    for e in data[::step][:sample_n]:
        txt = "\n".join(y["content"] for x in e["haystack_sessions"] for y in x)
        toks.append(len(ENC.encode(txt)))

    def med(v):
        return st.median(v) if v else 0

    types = Counter(e["question_type"] for e in data)
    n_abs = sum(1 for e in data if e["question_id"].endswith("_abs"))

    return {
        "label": label,
        "path": str(path),
        "n_q": n,
        "sessions_med": med(sess), "sessions_max": max(sess), "sessions_mean": st.mean(sess),
        "turns_med": med(turns), "turns_mean": st.mean(turns), "turns_max": max(turns),
        "tok_med": med(toks), "tok_mean": st.mean(toks),
        "tok_max": max(toks), "tok_total_est": st.mean(toks) * n,
        "span_med_days": med(spans), "span_max_days": max(spans) if spans else 0,
        "ev_density": st.mean(ev_ratio) if ev_ratio else 0,
        "types": dict(types), "n_abs": n_abs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--s", dest="s_path", required=True)
    ap.add_argument("--m", dest="m_path", default=None)
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()

    rows = [analyse(args.oracle, "Oracle", args.sample),
            analyse(args.s_path, "S (Small)", args.sample)]
    if args.m_path:
        rows.append(analyse(args.m_path, "M (Medium)", args.sample))

    def k(n):
        if n >= 1e9: return f"{n/1e9:.2f}B"
        if n >= 1e6: return f"{n/1e6:.2f}M"
        if n >= 1e3: return f"{n/1e3:.1f}k"
        return f"{n:,.0f}"

    print("=" * 88)
    print("LongMemEval 三档规模对比")
    print("=" * 88)
    metrics = [
        ("题目数",              "n_q",            lambda v: f"{v}"),
        ("每会话数 中位",        "sessions_med",   lambda v: f"{v:.0f}"),
        ("每会话数 均值",        "sessions_mean",  lambda v: f"{v:.1f}"),
        ("每会话数 最大",        "sessions_max",   lambda v: f"{v}"),
        ("每轮次数 中位",        "turns_med",      lambda v: f"{v:.0f}"),
        ("每轮次数 均值",        "turns_mean",     lambda v: f"{v:,.0f}"),
        ("每轮次数 最大",        "turns_max",      lambda v: f"{v:,}"),
        ("haystack token 中位",  "tok_med",        k),
        ("haystack token 均值",  "tok_mean",       k),
        ("haystack token 最大",  "tok_max",        k),
        ("全集 token 估计",      "tok_total_est",  k),
        ("时间跨度 中位(天)",    "span_med_days",  lambda v: f"{v:.0f}"),
        ("时间跨度 最大(天)",    "span_max_days",  lambda v: f"{v}"),
        ("证据密度",            "ev_density",     lambda v: f"{v*100:.1f}%"),
        ("拒答题数",            "n_abs",          lambda v: f"{v}"),
    ]
    head = f"{'指标':<22}" + "".join(f"{r['label']:>20}" for r in rows)
    print(head)
    print("-" * 88)
    for name, key, fmt in metrics:
        line = f"{name:<22}"
        for r in rows:
            line += f"{fmt(r[key]):>20}"
        print(line)
    print("-" * 88)
    print()
    print("倍数关系（相对 Oracle）")
    o = rows[0]
    for r in rows[1:]:
        def ratio(a, b):
            return a / b if b else float("inf")
        print(f"  {r['label']}")
        print(f"    会话数   ×{ratio(r['sessions_mean'], o['sessions_mean']):>8.1f}")
        print(f"    轮次数   ×{ratio(r['turns_mean'], o['turns_mean']):>8.1f}")
        print(f"    token    ×{ratio(r['tok_mean'], o['tok_mean']):>8.1f}")
        print(f"    时间跨度 ×{ratio(r['span_med_days'], o['span_med_days']):>8.1f}")
        print(f"    证据密度 {o['ev_density']*100:.1f}% -> {r['ev_density']*100:.1f}%"
              f"（稀释 ×{ratio(o['ev_density'], r['ev_density']):.1f}）")
        print()

    # 单轮 token 对比
    print("推算（均值）")
    for r in rows:
        if r["turns_mean"]:
            print(f"  {r['label']:<12} 单轮平均 {r['tok_mean']/r['turns_mean']:.0f} token"
                  f"   上下文装入需求 {k(r['tok_mean'])} token/题")


if __name__ == "__main__":
    main()
