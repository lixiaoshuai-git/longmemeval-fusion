"""LongMemEval Oracle 档数据结构与规模统计。

用途：在动手设计记忆系统之前，先摸清数据的真实形态——
题型构成、每题的会话/轮次规模、token 量、时间跨度、证据标记情况。
这些数字直接约束数据模型与成本估算。

用法:
    python analyze_oracle.py <path/to/longmemeval_oracle.json> [--sample N]
"""
import argparse
import json
import re
import statistics as st
import sys
from collections import Counter
from datetime import datetime

import tiktoken

sys.stdout.reconfigure(encoding="utf-8")
ENC = tiktoken.get_encoding("o200k_base")


def ntokens(text):
    return len(ENC.encode(text))


def parse_dt(s):
    """解析 haystack_dates / question_date。

    实际格式为 '2023/04/10 (Mon) 17:50'。注意必须保留时分——
    只取日期会把同一天内的会话压平，导致顺序检查失效。
    """
    s = re.sub(r"\([A-Za-z]+\)\s*", "", str(s)).strip()
    for f in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def pct_line(name, values):
    values = sorted(values)
    n = len(values)
    def q(p):
        return values[min(n - 1, int(p * n))]
    return (f"  {name:<12} min={values[0]:>8,}  p25={q(.25):>8,}  "
            f"median={q(.50):>8,}  p75={q(.75):>8,}  p95={q(.95):>8,}  "
            f"max={values[-1]:>9,}  mean={st.mean(values):>9,.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--dump", default=None, help="把完整报告写入该文件")
    args = ap.parse_args()

    buf = []
    def out(s=""):
        buf.append(s)
        print(s)

    data = json.load(open(args.data, encoding="utf-8"))
    out(f"# LongMemEval Oracle 数据分析")
    out(f"数据文件: {args.data}")
    out(f"实例总数: {len(data)}")
    out(f"字段: {', '.join(data[0].keys())}")
    out()

    # ---------- 1. 题型 ----------
    out("## 1. 题型构成")
    types = Counter()
    abs_types = Counter()
    for x in data:
        is_abs = x["question_id"].endswith("_abs")
        t = x["question_type"]
        types[t] += 1
        if is_abs:
            abs_types[t] += 1
    total = sum(types.values())
    out(f"  {'题型':<30} {'题数':>6} {'占比':>8}   {'其中 _abs':>9}")
    out("  " + "-" * 62)
    for t, c in types.most_common():
        out(f"  {t:<30} {c:>6} {c/total*100:>7.1f}%   {abs_types.get(t,0):>9}")
    out("  " + "-" * 62)
    out(f"  {'合计':<30} {total:>6} {'100.0%':>8}   {sum(abs_types.values()):>9}")
    out()
    out(f"  拒答题 (_abs 后缀) 共 {sum(abs_types.values())} 道 -> 这是独立的一类能力（前提校验）")
    out()

    # ---------- 2. 规模 ----------
    out("## 2. 每题规模（每个实例 = 一道题的完整 haystack）")
    sess_n, turn_n, tok_n, evsess_n, evturn_n = [], [], [], [], []
    turn_words = []
    for x in data:
        sess = x["haystack_sessions"]
        sess_n.append(len(sess))
        turns = sum(len(s) for s in sess)
        turn_n.append(turns)
        txt = "\n".join(t["content"] for s in sess for t in s)
        tok_n.append(ntokens(txt))
        evsess_n.append(len(x["answer_session_ids"]))
        evturn_n.append(sum(1 for s in sess for t in s if t.get("has_answer")))
        turn_words.extend(ntokens(t["content"]) for s in sess for t in s)

    out(pct_line("会话数", sess_n))
    out(pct_line("轮次数", turn_n))
    out(pct_line("证据会话数", evsess_n))
    out(pct_line("带标记轮次", evturn_n))
    out(pct_line("haystack token", tok_n))
    out(pct_line("单轮 token", turn_words))
    out()
    out(f"  全部 500 题 haystack 合计 token: {sum(tok_n):,}")
    out()

    # ---------- 3. 会话数分布 ----------
    out("## 3. 每题会话数分布（oracle 档关键特征）")
    dist = Counter(sess_n)
    for k in sorted(dist):
        bar = "#" * min(60, int(dist[k] / max(dist.values()) * 60))
        out(f"  {k:>2} 个会话: {dist[k]:>4} 题  {bar}")
    out()
    single = sum(1 for n in sess_n if n == 1)
    out(f"  只有 1 个会话的题: {single} 道 ({single/len(sess_n)*100:.1f}%) -> 不存在跨会话更新")
    out(f"  >=2 个会话的题: {len(sess_n)-single} 道 -> 存在更新/聚合/时序的空间")
    out()

    # ---------- 4. 时间跨度与时序 ----------
    out("## 4. 时间跨度与时序")
    spans_days = []
    for x in data:
        ps = [parse_dt(s) for s in x["haystack_dates"]]
        ps = [p for p in ps if p]
        if len(ps) >= 2:
            spans_days.append((max(ps) - min(ps)).days)
    if spans_days:
        out(pct_line("跨度(天)", spans_days))

    all_qd = [q for q in (parse_dt(x["question_date"]) for x in data) if q]
    out(f"  question_date 最小值: {min(all_qd):%Y/%m/%d}  最大值: {max(all_qd):%Y/%m/%d}")
    out(f"  question_date 取值数: {len(set(all_qd))}")
    out()

    out("### 4.1 haystack_dates 是否按时间升序（只统计 >=2 个会话的题）")
    asc = desc = mixed = single = bad = 0
    worst = []
    for x in data:
        ds = [parse_dt(s) for s in x["haystack_dates"]]
        if any(p is None for p in ds):
            bad += 1
            continue
        if len(ds) < 2:
            single += 1
            continue
        if all(ds[i] <= ds[i + 1] for i in range(len(ds) - 1)):
            asc += 1
        elif all(ds[i] >= ds[i + 1] for i in range(len(ds) - 1)):
            desc += 1
        else:
            mixed += 1
            inv = sum(1 for i in range(len(ds) - 1) if ds[i] > ds[i + 1])
            worst.append((inv / (len(ds) - 1), x["question_id"], len(ds)))
    out(f"  严格升序        : {asc}")
    out(f"  严格降序        : {desc}")
    out(f"  部分乱序        : {mixed}")
    out(f"  单会话题（无关）: {single}")
    out(f"  解析失败        : {bad}")
    out()
    out(f"  >>> 需要重排的题: {desc + mixed} / {asc + desc + mixed} "
        f"({(desc + mixed) / max(1, asc + desc + mixed) * 100:.1f}% of 多会话题)")
    out("  >>> 结论: oracle 档的会话不是按数组顺序排列的。")
    out("      必须按 haystack_dates 排序后再 ingest，否则时间序列错乱，")
    out("      knowledge-update（取最新值）与 temporal-reasoning（顺序推理）会直接错。")
    out()

    # ---------- 4.2 证据轮次 ----------
    out("### 4.2 证据密度（has_answer 标记）")
    ev_ratio = []
    for x in data:
        tot = sum(len(s) for s in x["haystack_sessions"])
        ev = sum(1 for s in x["haystack_sessions"] for t in s if t.get("has_answer"))
        if tot:
            ev_ratio.append(ev / tot)
    out(f"  证据轮次占总轮次比例: 均值 {st.mean(ev_ratio) * 100:.1f}%, "
        f"中位数 {st.median(ev_ratio) * 100:.1f}%")
    out(f"  -> 平均每 100 轮里只有约 {st.mean(ev_ratio) * 100:.0f} 轮是证据，其余为噪声")
    out()

    # ---------- 5. 答案形态 ----------
    out("## 5. 答案形态（决定 reader 的输出格式约束）")
    ans_tok = [ntokens(str(x["answer"])) for x in data]
    ans_chr = [len(str(x["answer"])) for x in data]
    out(pct_line("答案字符数", ans_chr))
    out(pct_line("答案token", ans_tok))
    short = sum(1 for c in ans_chr if c <= 20)
    out(f"  答案 <=20 字符的题: {short} 道 ({short/len(ans_chr)*100:.1f}%) -> 短事实型，可精确匹配")
    out()

    # ---------- 6. 日期格式样本 ----------
    out("## 6. 原始字段样本（第一题）")
    x0 = data[0]
    out(f"  question_id      : {x0['question_id']}")
    out(f"  question_type    : {x0['question_type']}")
    out(f"  question         : {x0['question']}")
    out(f"  answer           : {x0['answer']}")
    out(f"  question_date    : {x0['question_date']}")
    out(f"  haystack_dates[0]: {x0['haystack_dates'][0]}")
    out(f"  会话数           : {len(x0['haystack_sessions'])}")
    out(f"  证据会话 ids     : {x0['answer_session_ids']}")
    out()

    if args.dump:
        open(args.dump, "w", encoding="utf-8").write("\n".join(buf))
        print(f"\n[报告已写入 {args.dump}]")

    # ---------- 7. 完整样例 ----------
    n = args.sample
    if n:
        out("=" * 70)
        out(f"## 完整样例（前 {n} 题）")
        for x in data[:n]:
            out("=" * 70)
            out(f"question_id  : {x['question_id']}")
            out(f"type         : {x['question_type']}")
            out(f"date         : {x['question_date']}")
            out(f"question     : {x['question']}")
            out(f"answer       : {x['answer']}")
            out(f"sessions     : {len(x['haystack_sessions'])}   evidence={x['answer_session_ids']}")
            for sid, sd, sess in zip(x["haystack_session_ids"],
                                     x["haystack_dates"], x["haystack_sessions"]):
                mark = " <== EVIDENCE" if sid in x["answer_session_ids"] else ""
                out(f"  --- {sid}  {sd}{mark}")
                for t in sess:
                    flag = " [has_answer]" if t.get("has_answer") else ""
                    c = t["content"].replace("\n", " ")
                    if len(c) > 160:
                        c = c[:160] + "..."
                    out(f"      {t['role']:<9} {c}{flag}")


if __name__ == "__main__":
    main()
