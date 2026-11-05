# STORY.md — 技术框架讲解

## ① 用户意图对齐

- **目标受众**：技术评审、导师、同学；技术分享/组会场合。
- **核心目标**：讲完后听众能说清「双层记忆 + 四个 LLM 边界 + 确定性算子」的分工，并相信每一个机制都有实验数据支撑。
- **PPT 长度**：16 页（Hero 4 页：封面 / 架构总览 / 成绩 / 结束）。
- **视觉调性**：工程冷静、终端感、克制、可审计。
- **内容边界**：必讲双层记忆、双时间轴、四边界、写入/查询流程、守卫层、S 档 one-hop；不讲评测流水线细节与全部迭代史；禁止夸大分数（一律多运行口径）。

## ② 页面布局骨架

页面总数 16，分 3 章：

| 章 | 目录条目 | 页码区间 | section 扉页 |
|---|---|---|---|
| 01 | 问题与框架 | 04–05 | 第 3 页 |
| 02 | 数据模型与写入 | 07–09 | 第 6 页 |
| 03 | 查询与验证 | 11–15 | 第 10 页 |

目录 3 条 ↔ 3 个 section 扉页，编号连续（01/02/03），标题逐字一致。

Hero 页：01 封面、05 架构总览、15 成绩、16 结束（4/16 = 25%，互不相邻）。
rhythm：1 peak → 2 valley → 3 transition → 4 valley → 5 peak → 6 transition → 7 valley → 8 valley → 9 valley → 10 transition → 11 valley → 12 valley → 13 valley → 14 valley → 15 peak → 16 peak。
连续 ≥3 valley 处（7-9、11-14）已由 transition/peak 打断：9→10 transition，14→15 peak。

非对称版式预算：05（非对称双栏）、07（左标题+右内容）、09（上大图+下方卡片改为左侧流程+右侧要点）、12（左标题+右内容）、13（非对称双栏）、14（左大图+右侧文字）≥ 40%。

## ③ 页面大纲

### 01 封面
- type: cover / role: hero / rhythm: peak / layout: 全幅图+骑线文字（深色渐变底 + 左侧标题）
- visual: SVG 网格底纹（L3）；巨型标题文字作锚点
- visual_role: anchor
- density: 字数约 40 / 图 1 / 留白约 30%
- anti_pattern: 禁止居中对称大标题；禁止装饰性插画
- description: fusion —— 把对话历史编译成可计算的记忆。技术框架讲解 / 2026.10

### 02 目录
- type: catalog / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 三条章节编号（L3）
- visual_role: evidence
- density: 字数约 90 / 图 0 / 留白约 25%
- anti_pattern: 禁止四卡片预览；禁止圆球编号
- description: 三章：问题与框架 / 数据模型与写入 / 查询与验证。

### 03 章节扉页 01
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 01（≥160px）
- visual_role: anchor
- density: 字数约 40 / 图 0 / 留白约 35%
- anti_pattern: 禁止四卡片预览；禁止满版正文
- description: 问题与框架 —— 为什么需要记忆系统，以及它的四个 LLM 边界。

### 04 问题定义
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（40:60）
- visual: 三行「问题→后果」对照（L3 文本结构）
- visual_role: evidence
- density: 字数约 260 / 图 0 / 留白约 20%
- anti_pattern: 禁止等宽三卡横排；禁止只列现象不给判断
- description: LLM 无状态；全上下文方案成本高、噪声大、且被手册 F.1 禁止作为交付——所以需要压缩式记忆。

### 05 架构总览
- type: content / role: hero / rhythm: peak / layout: 非对称双栏（65:35）
- visual: SVG 架构图（写入期 → 事实层/证据层 → 查询期，四边界标注）
- visual_role: anchor
- density: 字数约 220 / 图 1 / 留白约 18%
- anti_pattern: 禁止把架构图缩成角标；禁止罗列技术名词不给数据流
- description: 双层记忆 + 四个 LLM 边界；其余全部确定性算子——这是全系统的设计宪法。

### 06 章节扉页 02
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 02
- visual_role: anchor
- density: 字数约 40
- anti_pattern: 禁止四卡片预览
- description: 数据模型与写入 —— 一条事实如何获得两个时间轴，以及五种关系如何被确定性执行。

### 07 数据模型：Event 与双时间轴
- type: content / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 字段表 + 双时间轴示意（SVG）
- visual_role: evidence
- density: 字数约 280 / 图 1 / 留白约 18%
- anti_pattern: 禁止把字段表做成无标注流水账；禁止省略 τ_v/τ_t 定义
- description: Event 十六字段；有效轴回答「现实何时成立」，陈述轴回答「何时被说出」；Slot 提供 current()/value_at(t) 两个算子。

### 08 演化 vs 更正
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（60:40）
- visual: 两条时间轴对比（SVG）
- visual_role: evidence
- density: 字数约 260 / 图 1 / 留白约 22%
- anti_pattern: 禁止等分双栏；禁止用「更新」一个词含糊带过两种语义
- description: 演化只封有效轴（旧值曾为真），更正两轴都封（旧值从未为真）——这是与开源 ADD-only 系统的关键差别。

### 09 写入期六步
- type: content / role: supporting / rhythm: valley / layout: 上大图+下方卡片（横向步骤条 + 三张要点卡）
- visual: 六步流程 SVG（抽取→时间→检索→分类→执行）
- visual_role: anchor
- density: 字数约 300 / 图 1 / 留白约 15%
- anti_pattern: 禁止六张等宽卡横排；禁止只写步骤名不写输入输出
- description: 逐轮抽取保数字逐字、时间规则优先、关系分类五动作、确定性执行器改写存储。

### 10 章节扉页 03
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 03
- visual_role: anchor
- density: 字数约 40
- anti_pattern: 禁止四卡片预览
- description: 查询与验证 —— 问题如何变成检索计划，以及守卫层如何拦截两类错误。

### 11 查询期流程
- type: content / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 七步流程（SVG 竖向）
- visual_role: anchor
- density: 字数约 300 / 图 1 / 留白约 15%
- anti_pattern: 禁止单栏线性列表；禁止把「解析」和「执行」混为一谈
- description: 解析出 8 种意图；确定性执行器按意图路由；BM25 召回；verbatim 引用；守卫；九段渲染；作答。

### 12 BM25 与近逐字证据层
- type: content / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 公式 + 三因素说明表（L3）
- visual_role: evidence
- density: 字数约 280 / 图 0 / 留白约 20%
- anti_pattern: 禁止只写「用了 BM25」不解释打分；禁止把公式做成装饰
- description: IDF/词频饱和/长度归一三因素；分词含词形折叠与同义归并；lookup 意图下最强轮次全文引用 1500 字符。

### 13 守卫层
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（55:45）
- visual: 三条判定规则 + 触发示例
- visual_role: evidence
- density: 字数约 300 / 图 0 / 留白约 18%
- anti_pattern: 禁止把守卫层说成 prompt 工程；禁止省略建议类豁免
- description: SCOPE NOTE 拒答措辞协议、PREMISE CHECK 专名假前提、RECOMMENDATION HINT 建议形态豁免——全部确定性代码。

### 14 S 档扩展与遗忘
- type: content / role: supporting / rhythm: valley / layout: 左大图+右侧文字
- visual: one-hop 示意（SVG 实体倒排图）
- visual_role: anchor
- density: 字数约 260 / 图 1 / 留白约 20%
- anti_pattern: 禁止把门控条件藏在脚注；禁止省略路由收支结论
- description: 实体倒排 + 稀有实体共现扩展，store>100 才启用；list_events 路由实测纯亏已移除；遗忘模块三策略软归档。

### 15 成绩与边界
- type: content / role: hero / rhythm: peak / layout: 巨型数字+洞察
- visual: 三个巨型数字（86.80 / 89.58 / 11.3ms）+ 边界清单
- visual_role: anchor
- density: 字数约 180 / 图 0 / 留白约 28%
- anti_pattern: 禁止把数字装进等宽卡片；禁止只报喜不报边界
- description: Oracle 86.80±0.72 统计打平上界、SS-P 反超；S 档 48 题 89.58%；检索 p95 11.3ms；剩余边界是 judge 噪声与 MS 计数。

### 16 结束页
- type: ending / role: hero / rhythm: peak / layout: 居中金句/巨型数字
- visual: 一行总结金句
- visual_role: anchor
- density: 字数约 30
- anti_pattern: 禁止烟花/装饰；禁止罗列联系方式
- description: LLM 负责理解，代码负责计算——可审计的记忆才是可交付的记忆。
