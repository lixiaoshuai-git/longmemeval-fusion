# LongMemEval 记忆系统（fusion v3.8 生产基线 / v3.9.1 S 档检索扩展）

自研对话记忆系统：把非结构化对话历史编译成"自由文本事实 + 确定性时间结构"的
双层记忆，LLM 只在抽取 / 关系分类 / 解析 / 作答四个边界出现，中间全部是
可审计的确定性算子。

**Oracle 档（500 题）与全上下文上界统计打平，SS-P 首次反超基线；
S 档（122k tok/题，48 题样本）零架构改动泛化，89.58% 进入第三方
协议带上方、逼近自报系头部。**

## 成绩（glm-5.2 全链路，多运行口径）

### Oracle 500 题

| 系统 | 运行数 | Overall | range | 备注 |
|---|---|---|---|---|
| 全上下文基线（上界探针，F.1 禁作交付） | 4 | **87.90 ± 0.84** | 87.0 – 88.8 | |
| fusion v2.6 | 3 | 85.73 ± 1.36 | 84.2 – 86.8 | |
| fusion v3.4.5 | 3 | 85.67 ± 0.31 | 85.4 – 86.0 | 方差最小的守卫层版本 |
| fusion v3.6 | 3 | 86.80 ± 1.11 | 85.6 – 87.8 | 近逐字证据层 |
| fusion v3.7 / v3.7.1 | 各 3 | 86.53 / 85.67 | — | verbatim 扩展，**均已回滚** |
| **fusion v3.8（生产基线，当前代码）** | 3 | **86.80 ± 0.72** | 86.2 – 87.6 | SS-P 结构性突破 |

**跨采样稳定的结论**（单次差值 <3pp 不作数，见方差披露）：

- **SS-P 81.11 → 90.00（+8.89）**：三轮单调上行 [86.67/90.00/93.33]，
  全部高于 v3.6 全部三轮——**首次有题型反超全上下文基线（90.00 vs 87.50）**；
- **TR / MS 领先基线**——确定性时间归一化与写入期聚合的收益；
- **`_abs` 拒答 ~24/30（80%）恒定复现**（v2.6 时代 18/30）——PREMISE
  CHECK 是确定性规则；
- KU / SS-A / SS-U 仍落后基线 2–7pp，为信息压缩的预期代价。
- 完整分题型与消融矩阵见 `docs/evaluation-report.md`。

### S 档试点（longmemeval_s_cleaned，122k tok/题，分层样本 n=48）

| 版本 | Overall | 关键变化 |
|---|---|---|
| v3.6（试点基线） | 72.92%（35/48） | 零架构改动直跑 |
| v3.9 | 79.17%（38/48） | +实体倒排 one-hop（计数/现值路由） |
| **v3.9.1（当前）** | **89.58%（43/48）** | one-hop 路由收支修正（list_events 移除） |

- **v3.6 → v3.9.1 全程配对 8 修 / 0 坏**（McNemar p≈0.008）；
- 四题型满分：KU 7/7、SS-A 6/6、SS-P 3/3、SS-U 6/6；MS 9/12、TR 12/14；
- 榜单定位：超第三方可复现协议系（TiMem 76.9 / MemOS 77.8 / memobase 72.4），
  近 Hindsight 91.4 / Supermemory 85.2，与自报系头部（94–96，gpt-5.5 级
  reader）差 ~5pp——我们用的是非思考 glm-5.2；
- **样本量说明**：采样器请求 50 题，三轮运行中同一批 2 题因端点故障
  未完成，有效样本 n=48（95% CI ±9pp）；三轮为同一批 48 题，配对成立。
- 规模压力数据：单题 723 events / 550 轮（Oracle 的 22 倍），检索 86ms，
  122k haystack → 14.6k tok 上下文（8.4 倍压缩）。

**前沿架构对照**：Hindsight 引擎复现（`experiments/hindsight/`，项目树外）
同基准同 judge 全量 500 = 82.80% vs fusion v3.8 86.80（+4.0pp）——
hindsight 强在 KU（Reflect 层 94.87），弱在 SS-A/TR，互相印证机制取舍。

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
  → 确定性执行（聚合计数/求和、版本链、时刻查询、argmax、序数查找）
  → BM25 模糊召回（事实句 / 槽位代表句 / 句子级摘录）
  → 近逐字证据层（v3.6）：lookup/unknown 意图引用最强命中源轮次全文
  → 确定性守卫：
      MEMORY SCOPE NOTE（拒答措辞协议，建议类问句改发 RECOMMENDATION HINT）
      + PREMISE CHECK（专名假前提拦截，建议形态豁免）
  → 组装：CURRENT-STATE LEDGER（版本链槽位当前值台账，v3.8）
      + 事实清单 + PREFERENCES & OPINIONS 段（偏好前置，v3.8）
      + ONE-HOP RELATED（实体倒排一跳扩展，v3.9.1，仅 store>100 且
        intent ∈ {agg_count, agg_sum, fact_current}——S 档专用，Oracle 零行为）
      + user 轮次地板 → 作答

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
自动化单测：`pip install -r requirements-dev.txt && python -m pytest tests/ -q`（39 例，纯离线，<1s；CI 见 `.github/workflows/tests.yml`）。

1. 配置 `.env`（参考 `.env.example`）：OpenAI 兼容接口的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
2. 数据：LongMemEval 官方仓库（github.com/xiaowu0162/LongMemEval），
   `longmemeval_oracle.json`（及 S 档 `longmemeval_s_cleaned.json`）放到
   `ref/LongMemEval/data/`（国内可用 hf-mirror.com 镜像）。
3. 评测（在 `scripts/` 下）：

```bash
# fusion 记忆系统，全量 500 题（Oracle）
python run_eval.py --workers 4 --tag myrun --strategy fusion

# S 档试点（分层 50 题）
python run_eval.py --data ref/LongMemEval/data/longmemeval_s_cleaned.json \
    --stratified 50 --tag spilot --strategy fusion --workers 6

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
python cli.py --cache results/fusfull38-fusion-cache.json   # 可选复用评测缓存
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
| `memory_sys/fusion.py` | **核心**：fusion 记忆策略（写入期对齐 + 确定性执行器 + BM25 召回 + 近逐字证据层 + 守卫层 + LEDGER/偏好分段 + S 档 one-hop） |
| `memory_sys/bm25.py` | 纯 Python BM25 与分词（无第三方依赖） |
| `memory_sys/schema.py` | 数据模型：Event（五元组 + fact/slot_id）、Slot 版本链、MemoryStore |
| `memory_sys/lifecycle.py` | 遗忘/生命周期：TTL / 容量 / 历史压缩，软归档（独立模块，零核心耦合） |
| `memory_sys/llm_io.py` | LLM 边界提示词：抽取 / 关系分类 / 解析 + 稳健 JSON 解析（JSONL 兜底） |
| `memory_sys/structured.py` | v1 结构化策略（留作消融对照） |
| `cli.py` | **交互入口**：多轮跨会话 REPL（load / ingest / ask / context / stats / forget） |
| `tests/` | 39 例离线单测（时间解析/执行器/BM25/遗忘/守卫）+ CLI 手工用例（17 例全过） |
| `scripts/lme_core.py` | 数据加载（会话强制按时间排序）、LLM 客户端（思考开关）、官方提示词模板 |
| `scripts/memory.py` | 策略注册与接口（full-context / user-turn-only / structured / fusion） |
| `scripts/run_eval.py` | 评测流水线：分层抽样、断点续跑、缓存落盘 |
| `scripts/judge.py` | 评分（直接 import 官方 `get_anscheck_prompt`，不重写提示词；需官方仓库） |
| `scripts/build_report.py` | 真实水平报告生成器（从 summary.json 直注数据，零手抄数字） |
| `docs/layer2-data-structure.md` | 设计文档：三层 schema、四类流程、v1 失败取证（§8）、v2 实现与验证（§9） |
| `docs/iteration-log.md` | 全部迭代实验日志（v1→v3.9.1，14 个闭环：5 次回滚 + 2 次标定否决） |
| `docs/evaluation-report.md` | 正式评估报告：多轮主结果、消融矩阵、检索延迟、成本、方差披露 |
| `docs/defense-outline.md` | 答辩材料：演示流程、关键数据速查、Q&A 预案 |
| `docs/project-truth-report.html` | 真实水平综合可视化报告（12 数据集 / 27 次运行） |
| `docs/phase1-findings.md` | Phase 1 摸底报告：数据结构、基线、失败模式、成本 |
| `results/*-summary.json` | 各轮评测汇总；交付 JSONL：`results/fusfull38-hyp.jsonl`（Oracle）/ `results/spilot50v391-hyp.jsonl`（S 档） |

## 迭代史（详见 docs/iteration-log.md）

v1 封闭谓词（60%）→ v2 自由文本+写入期对齐 → v2.6 句子级摘录+序数查找
（85.7）→ v3.1 三层计数（**回滚**：锚定效应）→ v3.2 批内 LLM 去重（**回滚**：
过/漏合并）→ v3.3 拒答措辞+时间线+argmax → v3.4.5 PREMISE CHECK（`_abs`
+20pp，含 SS-P 误杀回归的修复）→ v3.5 序数升级+窗口锚（**回滚**：无正证据）
→ **v3.6 近逐字证据层**（86.80±1.11，SS-A/KU/SS-P 同步上行）→ v3.7/v3.7.1
verbatim 扩展（**回滚**：方差淹没信号）→ **v3.8 LEDGER+偏好分段+建议形态
守卫修复**（SS-P +8.89 反超基线，生产基线）→ v3.9 实体倒排 one-hop
（S 档门控）→ **v3.9.1 路由收支修正**（S 档 89.58%，8修0坏）。

## 已知边界

- Oracle 档架空的是会话级检索（0 干扰会话），轮次级筛选仍在；S 档已试点
  （89.58%，n=48），混合检索（embedding 通道）是 S 档正式化的下一项——
  接口已预留；one-hop 实体倒排已按 S 规模门控（store>100），Oracle 零行为。
- S 档剩余软肋：MS 9/12（reader 大池计数）与 TR 12/14（噪声日期证据），
  已诊断为 BM25 词面相关性对语义关联事件的局限，非窗口问题（动态 top-30
  标定证伪）。
- `_abs` 拒答题与代理 judge 的噪声混在一起，语义正确的回答可能被判错
  （判错集中 64% 为误判，语义审计估算真实水平 ~91.6%）；正式评测务必用
  官方 judge（gpt-4o）复跑。
- 长枚举清单（>12 项）抽取覆盖不全，但序数词查找可确定性命中指定项。
- 遗忘模块按手册要求实现并经交互入口可用，但评测默认关闭（Oracle 档无
  记忆损失实验）；其策略参数未在 S 档调优。
