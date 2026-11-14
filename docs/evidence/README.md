# 冻结证据（v3.21，2026-11-14）

这里只放**聚合数字**，不放原始判分明细。原因：明细逐行含题目、标准答案与
模型回答，属于官方数据集内容，仓库不转分发；聚合结果足以复核结论口径。

## 这批数字是什么

在模型端点到期前，对 v3.21 代码做了一次"把不确定度测干净"的批量运行。
所有运行共用同一份抽取缓存（`results/v320-fusion-cache.json`，抽取提示词
版本未变），因此只消耗答题与判分调用。

| 运行 | 内容 | 题数 |
|---|---|---|
| `v320` / `r2` / `r3` / `r4` | 当前代码，四轮独立作答 + 判分 | 500 × 4 |
| `v320j2` / `v320j3` | 同一批 hypothesis 重复判分两次 | 500 × 2 |
| `abl2-noexc` / `noarith` / `nofloor` / `widepremise` | 四组单机制消融 | 500 × 4 |
| `full500` / `-rerun` / `-run3` / `-run4` | 全上下文基线四轮（历史运行） | 500 × 4 |

## 文件

| 文件 | 内容 |
|---|---|
| `v321-variance.json` | 四轮均值/标准差、分题型极差、题级稳定度（稳定对 437 / 稳定错 42 / 摇摆 21） |
| `v321-g3-test.json` | test 249 题上与全上下文基线的配对比较：+0.20pp，95% CI −3.61…+4.12，McNemar p=0.50 |
| `v321-ablations.json` | 四组消融的总分、配对数、分题型变化 |

原始明细（含题目与回答）不入库，本地归档在
`results/frozen-v321-2026-11-14.zip`（2.2 MB，23 个文件，内含 MANIFEST 与 sha256 前缀）。
归档里同时保存了抽取缓存——端点不可用后，`regress --check` 仍可离线回放这 500 题。

## 复现

```bash
# 方差与题级稳定度
python scripts/variance.py results/v320-judged.jsonl results/r2-judged.jsonl \
    results/r3-judged.jsonl results/r4-judged.jsonl

# test 切分配对（G3）
python scripts/g3_report.py --base full500 full500-rerun full500-run3 full500-run4 \
    --cand v320 r2 r3 r4 --split test

# 消融（冷缓存需重跑抽取；此处复用写侧缓存）
ABL_FLOOR=0 python scripts/run_eval.py --strategy fusion --tag abl2-nofloor --workers 8
python scripts/judge.py results/abl2-nofloor-hyp.jsonl --tag abl2-nofloor
```

## 引用时必须带上的限定

- 答题与 judge 均为 **glm-5.2**（judge 为代跑，官方口径默认 GPT-4o）；
- 日期 **2026-11-14**，四轮，Oracle 档；
- judge 重复判分 500/500 完全一致，但这是**可复现性**，不等于判分口径与官方一致；
- 基线为历史四轮（2026-10 上旬），与当前候选共用同一 judge 与数据版本。
