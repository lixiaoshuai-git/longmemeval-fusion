# LongMemEval Oracle 记忆系统（fusion v2.6）

自研对话记忆系统，在 LongMemEval Oracle 档（500 题）上取得 **86.80%（434/500）**，
与全上下文上界（87.00%，手册附录 F.1 明令禁止的实现）统计打平，
其中 temporal-reasoning **反超上界 +6.76pp**。

## 架构（吸取 Mem0 / Graphiti / Letta / Hindsight 各家之长）

```
写入期（每会话）
  user 轮次 → 逐轮聚焦抽取（事实=自由文本句子，数字/专名逐字保留）
  assistant 专名 → 专项抽取（信息型教学事实必抽，选项列表不抽）
  相对时间 → 规则解析优先（英文数词），模型解析回退，±400 天合法性校验
  新事实 vs 已有记忆（BM25 top-3 相似检索）→ LLM 关系分类
    new / merge / evolve / correct / supplement + 时间锚点
  → 确定性执行器（双时间轴几何：演化只封 τ_v，更正连 τ_t 一起封）
  → 槽位语义聚类（slot_id 写入期继承，不依赖词面相等）

查询期
  问题 → 解析（intent / topic / window / entities）
  → 确定性执行（聚合计数、版本链、时刻查询、总量+增量算术注记）
  → BM25 模糊召回（事实句 / 槽位代表句 / 句子级摘录）
  → 序数词确定性查找（"the 27th parameter" 类长清单）
  → 组装：确定性结果 + 事实清单 + 句子级摘录 + user 轮次地板 + 拒答范围注记

LLM 只出现在三个边界：抽取 / 解析 / 生成；中间全部是确定性算子。
```

## 最终成绩（Oracle 500 题）

| 题型 | fusion v2.6 | 全上下文基线（被禁上界） | Δ |
|---|---|---|---|
| temporal-reasoning | **85.71%** | 78.95% | **+6.76** |
| multi-session | 79.70% | 80.45% | -0.75 |
| knowledge-update | 88.46% | 92.31% | -3.85 |
| single-session-user | 94.29% | 98.57% | -4.28 |
| single-session-assistant | 94.64% | 100.00% | -5.36 |
| single-session-preference | 86.67% | 86.67% | 0 |
| `_abs` 拒答 | 60.00% | 80.00% | -20.00（含代理 judge 噪声） |
| **总体** | **86.80%** | 87.00% | **-0.20（CI 内打平）** |

judge：官方 `evaluate_qa.py` 提示词 + glm-5.2 代跑（正式提交需官方 judge 复跑）。

## 运行方法

依赖：Python 3.10+，`pip install openai tiktoken backoff numpy tqdm`（judge 另需官方仓库依赖）。

1. 配置 `.env`（参考 `.env.example`）：OpenAI 兼容接口的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
2. 数据：LongMemEval 官方仓库（github.com/xiaowu0162/LongMemEval），
   `longmemeval_oracle.json` 放到 `ref/LongMemEval/data/`（国内可用 hf-mirror.com 镜像）。
3. 评测（在 `scripts/` 下）：

```bash
# fusion 记忆系统，全量 500 题
python run_eval.py --workers 4 --tag myrun --strategy fusion

# 对照：全上下文基线（仅作上界探针，F.1 禁止作为交付）
python run_eval.py --workers 4 --tag baselinectl

# 评分（需官方仓库的 src/evaluation 在 import 路径上）
python judge.py results/myrun-hyp.jsonl --tag myrun
```

首次运行会逐题做抽取/关系分类（约 8M token / 55 分钟 / 4 并发）；
LLM 调用结果全部落盘缓存（`results/<tag>-fusion-cache.json`），
改提示词或读取逻辑后重跑只需重生成答案（约 3.5 分钟）。

## 文件清单

| 路径 | 说明 |
|---|---|
| `memory_sys/fusion.py` | **核心**：fusion 记忆策略（写入期对齐 + 确定性执行器 + BM25 召回） |
| `memory_sys/bm25.py` | 纯 Python BM25 与分词（无第三方依赖） |
| `memory_sys/schema.py` | 数据模型：Event（五元组 + fact/slot_id）、Slot 版本链、MemoryStore |
| `memory_sys/llm_io.py` | LLM 边界提示词：抽取 / 关系分类 / 解析 + 稳健 JSON 解析（JSONL 兜底） |
| `memory_sys/structured.py` | v1 结构化策略（留作消融对照） |
| `scripts/lme_core.py` | 数据加载（会话强制按时间排序）、LLM 客户端（思考开关）、官方提示词模板 |
| `scripts/memory.py` | 策略注册与接口（full-context / user-turn-only / structured / fusion） |
| `scripts/run_eval.py` | 评测流水线：分层抽样、断点续跑、缓存落盘 |
| `scripts/judge.py` | 评分（直接 import 官方 `get_anscheck_prompt`，不重写提示词；需官方仓库） |
| `scripts/analyze_oracle.py` 等 | 数据集结构分析工具 |
| `docs/layer2-data-structure.md` | 设计文档：三层 schema、四类流程、v1 失败取证（§8）、v2 实现与验证（§9） |
| `docs/phase1-findings.md` | Phase 1 摸底报告：数据结构、基线、失败模式、成本 |
| `results/*-summary.json` | 各轮评测汇总（全量基线 / fusion / 抽样消融） |

## 已知边界

- Oracle 档架空的是会话级检索（0 干扰会话），轮次级筛选仍在；S/M 档需在
  `StructuredQuery` 执行器前插入混合召回（接口已预留）。
- `_abs` 拒答题与代理 judge 的噪声混在一起，语义正确的回答可能被判错；
  正式评测务必用官方 judge（gpt-4o）复跑。
- 长枚举清单（>12 项）抽取覆盖不全，但序数词查找可确定性命中指定项。
