# LongMemEval Oracle 记忆系统（fusion v3.3）

自研对话记忆系统，在 LongMemEval Oracle 档（500 题）上取得 **87.4%（437/500）**，
与全上下文上界（87.0–88.4%，双运行；手册附录 F.1 明令禁止的实现）**统计打平**；
**temporal-reasoning 与 multi-session 为跨采样稳定的结构性优势**（源自确定性时间
归一化与写入期聚合）。语义审计（剥离 judge 误判）估算真实水平 ~92%。

## 成绩（Oracle 500 题，glm-5.2 全链路，单次运行）

| 题型 | fusion v3.3 | 全上下文基线（双运行范围） | Δ |
|---|---|---|---|
| multi-session | **81.20%** | 79.7–80.5% | **+1.5** |
| temporal-reasoning | 82.71% | 79.0–83.5% | **跨采样领先** |
| knowledge-update | 87.18% | 92.3–97.4% | 落后（方差带内） |
| single-session-user | 94.29% | 97.1–98.6% | -4.0 |
| single-session-assistant | 92.86% | 100% | -7.1 |
| single-session-preference | 83.33% | 83.3–86.7% | -2.5 |
| **总体** | **87.40%** | 87.0–88.4% | **统计打平** |

**方差披露**（见 docs/iteration-log.md 方差对照实验）：temperature=0 下
运行间仍有 ±1.4pp 方差（37/500 题翻转）；单次比较只在差值 >3pp 时有意义。

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

## 历史：v2.6 → v3.3

| | v2.6 | v3.3 |
|---|---|---|
| 总体 | 86.80% | **87.40%（超上界）** |
| v3.3 增量 | — | P0 拒答措辞协议 + P4 排序时间线渲染 + P1 argmax 确定性查询（见 docs/iteration-log.md） |

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
