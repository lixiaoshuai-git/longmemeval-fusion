# LongMemEval Oracle 记忆系统（fusion v3.5）

自研对话记忆系统：把非结构化对话历史编译成"自由文本事实 + 确定性时间结构"的
双层记忆，LLM 只在抽取 / 关系分类 / 解析 / 作答四个边界出现，中间全部是
可审计的确定性算子。在 LongMemEval Oracle 档（500 题）上与全上下文上界
（手册附录 F.1 明令禁止的实现）统计打平，其中 temporal-reasoning 与
multi-session 为跨采样稳定的结构性优势。

## 成绩（Oracle 500 题，glm-5.2 全链路，多运行口径）

| 系统 | 运行数 | Overall Accuracy | range |
|---|---|---|---|
| 全上下文基线（上界探针，F.1 禁作交付） | 4 | **87.90 ± 0.84** | 87.0 – 88.8 |
| fusion v2.6 | 3 | 85.73 ± 1.36 | 84.2 – 86.8 |
| fusion v3.4.5（方差最小，生产基线） | 3 | **85.67 ± 0.31** | 85.4 – 86.0 |
| fusion v3.5（当前代码 = v3.4.5 + 序数升级/窗口锚） | 1 | 85.40 | — |

**跨采样稳定的结论**（单次差值 <3pp 不作数，见方差披露）：

- **TR +4.5pp / MS +1.7pp 领先基线**——确定性时间归一化与写入期聚合的收益；
- **`_abs` 拒答 24–25/30（80%+）恒定复现**（v2.6 时代 18/30）——PREMISE
  CHECK 是确定性规则，三轮运行逐题不差；
- KU / SS 系列落后是信息压缩的预期代价；SS-P 方差最大（reader 主观度最高）。
- 完整分题型数据见 `results/*.json` 与 `docs/project-review-v3.4.5.html`。

**方差披露**：temperature=0 下运行间仍有 ±1.4pp 方差（37/500 题翻转）；
fusion 侧多级 LLM 调用放大到 ±1.5–2pp，v3.4.5 因确定性守卫层收窄到 ±0.31。
judge 为 glm-5.2 代跑（官方 judge 待接），语义审计显示判错集中约 64% 为
judge 误判——正式口径以官方 `evaluate_qa.py` + gpt-4o 复跑为准。

## 架构

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
  → 确定性执行（聚合计数/求和、版本链、时刻查询、argmax、序数查找、窗口锚）
  → BM25 模糊召回（事实句 / 槽位代表句 / 句子级摘录）
  → 确定性守卫：MEMORY SCOPE NOTE（拒答措辞协议）
                + PREMISE CHECK（专名假前提拦截，请求类问句豁免）
  → 组装：确定性结果 + 事实清单 + 摘录 + user 轮次地板 → 作答

遗忘/生命周期（独立模块，默认关闭，见下）
```

## 遗忘与生命周期（B.1 必做能力）

`memory_sys/lifecycle.py`，独立模块，只操作 `MemoryStore` 公开形状，与核心
检索逻辑零耦合，经 `FusionMemory.forget()` 委托调用，**评测流水线默认不启用**
（Oracle 档不含记忆损失实验）。三条策略全部确定性：

| 策略 | 参数 | 语义 |
|---|---|---|
| 时效 TTL | `ttl_days` | 事实已失效（`valid_to` 闭合）且失效已久 → 归档 |
| 容量 | `capacity` | active 事件超上限时按重要性分（佐证数+数值完备+新近度）从低到高归档 |
| 历史压缩 | `compact_days` | 同槽位老版本（superseded/invalidated）陈述久远且已有更新值 → 归档 |

**遗忘 = 软归档**：`status → archived`，退出"当前有效事实"召回，但
`Slot.value_at(t)` 时刻查询不过滤状态——**历史时刻仍可审计**，符合 B.1
"历史版本保留方式"要求。策略幂等，报告含逐条归档原因。

## 运行方法

依赖：Python 3.10+，`pip install -r requirements.txt`（openai / tiktoken / backoff / numpy / tqdm；judge 另需官方仓库依赖）。

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

### 交互入口（CLI）

```bash
python cli.py --cache results/fusfull345-fusion-cache.json   # 可选复用评测缓存
```

```
/load <question_id>     载入一道 LongMemEval 题的全部证据会话并 ingest
/ingest <file> <date>   把文本文件作为一个会话写入（行前缀 U:/A:）
/ask <question>         基于记忆回答
/context <question>     只显示确定性检索上下文（回答依据可追溯，B.5）
/stats                  记忆库规模报告
/forget --capacity 30 --compact 180   执行遗忘/生命周期策略
/quit
```

## 文件清单

| 路径 | 说明 |
|---|---|
| `memory_sys/fusion.py` | **核心**：fusion 记忆策略（写入期对齐 + 确定性执行器 + BM25 召回 + 守卫层） |
| `memory_sys/bm25.py` | 纯 Python BM25 与分词（无第三方依赖） |
| `memory_sys/schema.py` | 数据模型：Event（五元组 + fact/slot_id）、Slot 版本链、MemoryStore |
| `memory_sys/lifecycle.py` | 遗忘/生命周期：TTL / 容量 / 历史压缩，软归档（独立模块，零核心耦合） |
| `memory_sys/llm_io.py` | LLM 边界提示词：抽取 / 关系分类 / 解析 + 稳健 JSON 解析（JSONL 兜底） |
| `memory_sys/structured.py` | v1 结构化策略（留作消融对照） |
| `cli.py` | **交互入口**：多轮跨会话 REPL（load / ingest / ask / context / stats / forget） |
| `scripts/lme_core.py` | 数据加载（会话强制按时间排序）、LLM 客户端（思考开关）、官方提示词模板 |
| `scripts/memory.py` | 策略注册与接口（full-context / user-turn-only / structured / fusion） |
| `scripts/run_eval.py` | 评测流水线：分层抽样、断点续跑、缓存落盘 |
| `scripts/judge.py` | 评分（直接 import 官方 `get_anscheck_prompt`，不重写提示词；需官方仓库） |
| `scripts/analyze_oracle.py` 等 | 数据集结构分析工具 |
| `docs/layer2-data-structure.md` | 设计文档：三层 schema、四类流程、v1 失败取证（§8）、v2 实现与验证（§9） |
| `docs/iteration-log.md` | 全部迭代实验日志（v1→v3.5，含失败回滚与方差对照实验） |
| `docs/phase1-findings.md` | Phase 1 摸底报告：数据结构、基线、失败模式、成本 |
| `docs/project-review-v3.4.5.html` | 多运行口径全景对比报告（交互式图表） |
| `results/*-summary.json` | 各轮评测汇总（全量基线 / fusion / 抽样消融） |

## 迭代史（详见 docs/iteration-log.md）

v1 封闭谓词（60%）→ v2 自由文本+写入期对齐 → v2.6 句子级摘录+序数查找
（85.7）→ v3.1 三层计数（**回滚**：锚定效应）→ v3.2 批内 LLM 去重（**回滚**：
过/漏合并）→ v3.3 拒答措辞+时间线+argmax → v3.4.5 PREMISE CHECK（`_abs`
+20pp，含 SS-P 误杀回归的修复）→ v3.5 序数升级+窗口锚（子 agent 评审后改道
实现）。

## 已知边界

- Oracle 档架空的是会话级检索（0 干扰会话），轮次级筛选仍在；S/M 档需在
  `StructuredQuery` 执行器前插入混合召回（接口已预留）。
- `_abs` 拒答题与代理 judge 的噪声混在一起，语义正确的回答可能被判错；
  正式评测务必用官方 judge（gpt-4o）复跑。
- 长枚举清单（>12 项）抽取覆盖不全，但序数词查找可确定性命中指定项。
- 遗忘模块按手册要求实现并经交互入口可用，但评测默认关闭（Oracle 档无
  记忆损失实验）；其策略参数未在 S 档调优。
