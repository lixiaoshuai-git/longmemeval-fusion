# 记忆系统接口（Memory API）

> 本文只讲**对外接口**：它由什么组成、每个动作做什么、怎么接自己的模型。
> 项目整体介绍、评测成绩与复现步骤见仓库根目录的 [README](../README.md)。
>
> 版本：v3.21 · 稳定面：`memory_sys.Memory` / `Recall` / `Answer` ·
> 内部模块（`write_path` / `read_path` / `render` / `temporal` …）不承诺稳定。

## 目录

1. [接口由什么组成](#1-接口由什么组成)
2. [数据结构](#2-数据结构)
3. [五分钟上手](#3-五分钟上手)
4. [接真实模型](#4-接真实模型)
5. [方法参考](#5-方法参考)
6. [配置项](#6-配置项)
7. [缓存与离线回放](#7-缓存与离线回放)
8. [错误与边界](#8-错误与边界)
9. [与评测流水线的关系](#9-与评测流水线的关系)
10. [扩展与限制](#10-扩展与限制)

---

## 1. 接口由什么组成

对外只有一个门面 `Memory`，它包装内部的 `FusionMemory`。五个动作分别落到不同的层：

```
Memory（memory_sys/api.py，对外稳定）
│
├─ add_session / add_sessions
│    └─ FusionMemory.write  →  WriteMixin
│         抽取 → 时间解析 → BM25 找相似 → 关系分类 → 确定性落库
│         └─ MemoryStore：Event（事实）/ Slot（版本链）/ Evidence（逐字轮次）
│
├─ recall
│    └─ FusionMemory.read
│         _plan()            解析问题，产出查询计划（走缓存）
│         _render()          确定性算子 + BM25 三层召回 + 渲染
│         _guard_sections()  SCOPE NOTE / 推荐提示 / 前提校验
│
├─ answer
│    └─ recall() + 官方答题模板 + llm.chat
│
├─ forget
│    └─ lifecycle.apply_lifecycle（TTL / 容量 / 历史压缩，软归档）
│
└─ stats / save_cache / reset
```

| 层 | 文件 | 职责 | 是否对外稳定 |
| --- | --- | --- | --- |
| 门面 | `memory_sys/api.py` | 五个动作、入参校验、数据结构 | **是** |
| 组合根 | `memory_sys/fusion.py` | 缓存、生命周期、三个 mixin 的组装 | 否 |
| 写入 | `memory_sys/write_path.py` | 抽取 → 定时 → 关系 → 落库 | 否 |
| 读取 | `memory_sys/read_path.py` | 计划 → 算子 → 召回 → 渲染 | 否 |
| 渲染与守卫 | `memory_sys/render.py` | 事实清单 / 台账 / 偏好 / 摘录 / 地板 / 守卫 | 否 |
| 数据模型 | `memory_sys/schema.py` | `Event` / `Slot` / `MemoryStore` | 否 |
| 选项 | `memory_sys/options.py` | `RuntimeOptions`（不可变，随实例） | 半稳定 |

设计约束（来自项目手册 B.3）：

- **与 LLM 解耦**：记忆的存储格式是结构化事实，不是 prompt 字符串；换模型只换 `llm`。
- **边界清晰**：写入 / 更新 / 检索 / 遗忘在接口上是分开的四个动作。
- **可观测**：`recall()` 返回查询计划与候选事实，`stats()` 返回规模指标。

---

## 2. 数据结构

### `Recall`（`recall()` 的返回）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `question` | `str` | 原问题 |
| `question_date` | `str` | 提问时间（相对时间的锚点） |
| `context` | `str` | **给 LLM 的记忆块**（确定性结果 + 事实清单 + 证据） |
| `plan` | `Mapping` | 查询计划：`intent / topic / category / entities / at_text …` |
| `sections` | `tuple[str, ...]` | 本次渲染出的段落标题，顺序即上下文顺序 |
| `candidates` | `tuple[Mapping, ...]` | 命中的候选事实（日期 / 分数 / 原文） |
| `stats` | `Mapping` | 当时的记忆规模 |

`Recall` 是不可变 dataclass，`str(recall)` 等价于 `recall.context`，因此老代码可以把返回值直接当上下文用。

### `Answer`（`answer()` 的返回）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `text` | `str` | 模型答案 |
| `recall` | `Recall` | 这次答案依据的那次检索（可审计） |
| `latency_s` | `float` | 仅 `llm.chat` 的耗时，不含检索 |

---

## 3. 五分钟上手

仓库自带一个不联网的示例（用假模型）：

```bash
python examples/quickstart.py
```

输出：

```
写入 2 个会话 → 新增 2 条事件
检索：intent=agg_sum，候选 1 条，段落 4 个
上下文前 260 字：
  [DETERMINISTIC QUERY RESULT]
  Query: agg_sum  topic="coins"  window=all time
  Candidate events, ranked by relevance to the topic (already deduplicated):
    (0.8) 2023-05-01 | I have 30 coins.
  TENTATIVE SUM over listed events = 30
  ...
答案：You have 30 coins.（0.000s）
统计： {'sessions': 2, 'events': 2, 'evidence': 2}
```

> 说明：示例故意写入两条事实。记忆库只有一条事件时，BM25 的 idf 偏低，
> 分数会低于检索阈值（`min_score=0.5`），`candidates` 会是空的——这不是 bug，
> 而是"小库 + 单条命中"的边界；真实评测里每题约 30 条事件，不会碰到。

最小代码：

```python
from memory_sys import Memory

mem = Memory(llm=client)                     # client 见第 4 节
mem.add_session([{"role": "user", "content": "I have 30 coins."}], "2023-05-01")

r = mem.recall("How many coins do I have?", "2023-06-01")
print(r.plan["intent"])      # agg_sum
print(r.context)             # 传给模型的记忆块

a = mem.answer("How many coins do I have?", "2023-06-01")
print(a.text)
```

---

## 4. 接真实模型

`llm` 只需要一个方法：

```python
class Client:
    def chat(self, prompt: str, max_tokens: int = 512) -> str: ...
```

任意 OpenAI 兼容端点都可以这样包一层：

```python
from openai import OpenAI

class OpenAIClient:
    def __init__(self, base_url, api_key, model):
        self._cli = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    def chat(self, prompt: str, max_tokens: int = 512) -> str:
        r = self._cli.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0,
        )
        return (r.choices[0].message.content or "").strip()

mem = Memory(llm=OpenAIClient("https://your-endpoint/v1", "sk-...", "your-model"))
```

仓库里的 `scripts/lme_core.LLMClient` 就是这样一个适配器（带重试与用量统计），
评测流水线用的就是它。

> 记忆逻辑不依赖具体模型；更换模型后**不需要**改动 `memory_sys/` 下任何代码。
> 唯一会变的是解析 / 抽取的质量，以及缓存需要按新模型重建。

---

## 5. 方法参考

### `Memory(...)`

```python
Memory(llm=None, *, cache=None, cache_path=None, options=None, strategy="fusion")
```

| 参数 | 说明 |
| --- | --- |
| `llm` | 任意带 `.chat(prompt, max_tokens=...)` 的对象；只做检索且缓存命中时可为 `None` |
| `cache` | 传入已有缓存 dict（离线回放）；不传则新建 |
| `cache_path` | 缓存落盘路径；配合 `save_cache()` |
| `options` | `RuntimeOptions`，见第 6 节 |
| `strategy` | 目前只支持 `"fusion"`；基线策略请用 `scripts/memory.py` 的 `build_memory()` |

### `add_session(turns, date) -> int`

写入一个会话，返回**本次新增的事件数**。

- `turns`：`[{"role": "user"|"assistant"|"system", "content": str}, ...]`，不能为空；
- `date`：会话日期，支持 `2023-05-01` 与数据集格式 `2023/05/01 (Mon) 17:50`；
- 内部会做抽取、相对时间解析、与既有记忆的关系判定（新增 / 合并 / 演进 / 纠正 / 补充）。

### `add_sessions(pairs) -> int`

批量写入 `[(turns, date), ...]`，返回新增事件总数。

### `recall(question, question_date) -> Recall`

只检索，不生成答案。缓存未命中时会调用一次 `llm` 解析查询计划。

### `answer(question, question_date, *, max_tokens=512, cot=False) -> Answer`

检索 + 作答。`cot=True` 时使用逐步推理模板。无 `llm` 时抛 `RuntimeError`。

### `forget(**policy) -> dict`

| 参数 | 含义 |
| --- | --- |
| `ttl_days` | 超过该天数的 **state** 事件归档 |
| `capacity` | active 事件超上限时，按重要性（佐证数 + 数值完备 + 新近度）从低到高归档 |
| `compact_days` | 同槽位老版本陈述久远且已有新值 → 归档 |

返回逐条归档原因。**软归档**：退出"当前事实"召回，但 `value_at(t)` 历史查询仍可审计。

### `stats() -> dict`

```python
{'events': 12, 'evidence': 5, 'extracted': 0, 'merged': 3,
 'superseded': 1, 'invalidated': 0, 'added': 13,
 'sessions': 4, 'options': {...}}
```

### `save_cache(path=None)` / `reset()`

- `save_cache`：把 LLM 调用缓存写到磁盘（断点续跑、离线回放）；
- `reset`：清空记忆内容与计数，保留缓存与配置（评测里每题一座独立记忆库）。

---

## 6. 配置项

`RuntimeOptions` 是不可变 dataclass，随实例存在（不是模块全局）：

```python
from memory_sys import Memory
from memory_sys.options import RuntimeOptions

mem = Memory(llm=client, options=RuntimeOptions(
    premise_proper_only=True,      # 前提校验只报专名信号（默认开）
    enable_excerpts=True,          # 句子级摘录（消融开关）
    enable_floor=True,             # 逐字证据地板（消融开关）
    enable_arith=True,             # 算术注记（消融开关）
    domain_terms=frozenset(),      # 语料专属高频名词，默认空
))
```

| 环境变量 | 对应字段 | 默认 |
| --- | --- | --- |
| `PREMISE_PROPER_ONLY` | `premise_proper_only` | `1` |
| `ABL_ARITH` / `ABL_EXCERPTS` / `ABL_FLOOR` | 三个消融开关 | `1` |
| `DOMAIN_TERMS` | `domain_terms`（逗号分隔） | 空 |

> `domain_terms` 是给"换语料"准备的：语言通用词在 `lexicon.LANGUAGE_GENERIC`，
> 语料专属名词由使用方注入，默认不生效。

---

## 7. 缓存与离线回放

每个 LLM 边界独立版本：`extract / assistant / relation / parse / answer`。
缓存键 = `sha1(版本 + "|" + 输入)`，因此改一个边界不会让其他缓存失效。

```python
import json
from memory_sys import Memory

cache = json.load(open("results/myrun-fusion-cache.json", encoding="utf-8"))
mem = Memory(cache=cache)          # 不传 llm
r = mem.recall("How many coins do I have?", "2023-06-01")   # 解析缓存命中即可离线
```

离线回放的前提是**解析缓存命中**；未见过的问句需要 `llm` 解析一次，
否则会抛 `RuntimeError` 并说明原因。

---

## 8. 错误与边界

| 场景 | 异常 / 行为 |
| --- | --- |
| `turns` 不是列表、为空、缺 `role`/`content`、`role` 非法 | `ValueError`，指出第几条 |
| 日期无法解析 | `ValueError`，给出两种可用格式 |
| `question` 为空 | `ValueError` |
| `answer()` 未传 `llm` | `RuntimeError("answer() 需要 llm…")` |
| `recall()` 未传 `llm` 且解析缓存未命中 | `RuntimeError("recall() 需要 llm…")` |
| `strategy` 不是 `"fusion"` | `ValueError`，提示用 `build_memory()` 取基线 |

---

## 9. 与评测流水线的关系

评测脚本 `scripts/run_eval.py` **不使用门面**：它直接调用内部的
`write()` / `read()`，因为每题需要一座独立记忆库、并且要拿 `last_trace`
写回归指纹。门面是给"把记忆系统嵌进别的应用"准备的入口，两者共用同一套
读写实现，行为一致。

对应关系：

| 评测脚本 | 门面 |
| --- | --- |
| `mem.reset()` + 逐会话 `mem.write()` | `Memory()` 新建 + `add_session()` |
| `mem.read(question, date)` | `recall(question, date).context` |
| `client.chat(build_prompt(...))` | `answer(question, date)` |
| `mem.last_trace` | `recall().plan / .sections / .candidates` |
| `mem.forget(...)` | `forget(...)` |

---

## 10. 扩展与限制

**可以做的：**

- 换模型：实现 `.chat()` 即可；
- 调开关：传 `RuntimeOptions`，两个实例可以并存不同配置；
- 观察：`recall()` 的计划与候选足够定位"是没召回、还是算子算错"。

**当前不支持（有意为之）：**

- 多用户隔离：一个 `Memory` 实例对应一座记忆库；多用户请建多个实例；
- 持久化状态：`save_cache()` 存的是 LLM 调用缓存，不是记忆库快照；
- 非 fusion 策略：门面只封装 fusion；基线（full-context）用于对照实验，
  走 `scripts/memory.py`。

**兼容性承诺：**

- `Memory` 的五个动作、`Recall` / `Answer` 的字段名在 v3.x 内保持稳定；
- 新增字段只增不改；`str(recall)` 恒等于 `recall.context`。
