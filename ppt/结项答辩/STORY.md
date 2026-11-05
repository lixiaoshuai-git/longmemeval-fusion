# STORY.md — 结项答辩

## ① 用户意图对齐

- **目标受众**：D.I.V.E. 计划评审老师与项目组；结项答辩场合（学术评审）。
- **核心目标**：讲完后评审相信三件事——① 这是一个真实的记忆系统（不是全上下文包装）；② 每个机制都有消融与多轮数据支撑；③ 评测方法本身严谨（方差控制、失败回滚、标定先行）。
- **PPT 长度**：17 页（Hero 4 页：封面 / Oracle 成绩 / S 档泛化 / 结束）。
- **视觉调性**：学术严谨、证据驱动、蓝白克制。
- **内容边界**：必讲问题定义、架构、三大机制、评测纪律、Oracle 与 S 档成绩、消融、交付物、边界；不讲代码细节与全部迭代史；分数一律多运行口径并披露方差；不夸大「超过上界」。

## ② 页面布局骨架

页面总数 17，分 3 章：

| 章 | 目录条目 | 页码区间 | section 扉页 |
|---|---|---|---|
| 01 | 问题与架构 | 04–05 | 第 3 页 |
| 02 | 核心机制 | 07–09 | 第 6 页 |
| 03 | 评测、交付与展望 | 11–16 | 第 10 页 |

目录 3 条 ↔ 3 个 section 扉页，编号连续（01/02/03），标题逐字一致。

Hero 页：01 封面、12 Oracle 成绩、13 S 档泛化、17 结束（4/17 ≈ 24%，互不相邻）。
rhythm：1 peak → 2 valley → 3 transition → 4 valley → 5 valley → 6 transition → 7 valley → 8 valley → 9 valley → 10 transition → 11 valley → 12 peak → 13 peak → 14 valley → 15 valley → 16 valley → 17 peak。
连续 valley 均被 transition/peak 打断（5→6，9→10，11→12，13→14 后 17 peak）。

非对称版式：05（非对称双栏 65:35）、07（左标题+右内容）、08（上大图+下方卡片）、11（左标题+右内容）、13（非对称双栏 60:40）、14（非对称双栏 55:45）、15（左大图+右侧文字）≥ 40%。

## ③ 页面大纲

### 01 封面
- type: cover / role: hero / rhythm: peak / layout: 全屏视觉+大标题（学术封面）
- visual: 深蓝顶条 + 细网格线稿（L3）
- visual_role: anchor
- density: 字数约 45 / 图 1 / 留白约 30%
- anti_pattern: 禁止红金装饰；禁止人物照片；禁止营销式大图
- description: D.I.V.E. 结项答辩：面向长对话的确定性记忆系统。苑泽宇 / 四川大学软件学院 / 2026.12

### 02 目录
- type: catalog / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 三条章节编号（L3）
- visual_role: evidence
- density: 字数约 100 / 图 0 / 留白约 25%
- anti_pattern: 禁止四卡片预览；禁止英文比中文抢眼
- description: 三章：问题与架构 / 核心机制 / 评测、交付与展望。

### 03 章节扉页 01
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 01
- visual_role: anchor
- density: 字数约 45
- anti_pattern: 禁止四卡片预览
- description: 问题与架构 —— 全上下文不可行，记忆系统要解决什么。

### 04 问题定义
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（40:60）
- visual: 三个缺口 + 一条禁令（L3）
- visual_role: evidence
- density: 字数约 280 / 图 0 / 留白约 18%
- anti_pattern: 禁止只讲「大模型没有记忆」不给出处；禁止省略 F.1 禁令
- description: LLM 无状态；全上下文成本/噪声/合规三个问题；目标是压缩历史并在 Oracle 追平上界、S 档保持可用。

### 05 架构总览
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（65:35）
- visual: SVG 架构图（双层记忆 + 四边界）
- visual_role: anchor
- density: 字数约 240 / 图 1 / 留白约 18%
- anti_pattern: 禁止堆名词；禁止把「确定性」写成口号而不标位置
- description: 事实层 + 证据层；LLM 只在抽取/关系/解析/作答四处；其余时间几何、聚合、守卫全部确定性。

### 06 章节扉页 02
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 02
- visual_role: anchor
- density: 字数约 45
- anti_pattern: 禁止四卡片预览
- description: 核心机制 —— 双时间轴、确定性执行、守卫层。

### 07 双时间轴版本链
- type: content / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 演化/更正两条时间线（SVG）
- visual_role: anchor
- density: 字数约 300 / 图 1 / 留白约 18%
- anti_pattern: 禁止把两种更新画成同一条线；禁止只讲概念不写算子名
- description: 演化只封 τ_v、更正连封 τ_t；current()/value_at(t) 两个算子；对比 ADD-only 系统的 KU 短板。

### 08 确定性执行与算术注记
- type: content / role: supporting / rhythm: valley / layout: 上大图+下方卡片（流程图 + 两张要点卡）
- visual: 解析→执行路由图（SVG）
- visual_role: anchor
- density: 字数约 300 / 图 1 / 留白约 15%
- anti_pattern: 禁止把「算术注记」讲成模型能力；禁止省略 37+1=38 的实例
- description: 8 种意图路由；聚合/版本链/argmax/序数；算术注记能答出原文从未出现的数字（37+1=38）。

### 09 守卫层
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（55:45）
- visual: 三类守卫判定表（L3）
- visual_role: evidence
- density: 字数约 300 / 图 0 / 留白约 18%
- anti_pattern: 禁止把守卫层说成提示词技巧；禁止省略建议类豁免的失败教训
- description: 拒答措辞协议、专名假前提校验、建议形态豁免；_abs 57%→80%，四道触发题全翻对。

### 10 章节扉页 03
- type: section / role: transition / rhythm: transition / layout: 巨型数字+洞察
- visual: 大数字 03
- visual_role: anchor
- density: 字数约 45
- anti_pattern: 禁止四卡片预览
- description: 评测、交付与展望 —— 多轮口径、Oracle 与 S 档、消融、交付物。

### 11 评测纪律
- type: content / role: supporting / rhythm: valley / layout: 左标题+右内容
- visual: 方差来源表 + 判定阈值（L3）
- visual_role: evidence
- density: 字数约 300 / 图 0 / 留白约 18%
- anti_pattern: 禁止只报单次最高分；禁止把回滚实验藏起来
- description: temperature=0 仍有 ±1.4pp；单次差 <3pp 不下结论；14 个闭环含 5 次回滚与 2 次标定否决——失败实验是方法严谨性的证据。

### 12 Oracle 成绩
- type: content / role: hero / rhythm: peak / layout: 巨型数字+洞察
- visual: 86.80（±0.72）巨型数字 + 分题型条形
- visual_role: anchor
- density: 字数约 200 / 图 1 / 留白约 22%
- anti_pattern: 禁止只给总分不给运行数；禁止把「打平上界」写成「超过」
- description: Oracle 500 题 3 run 86.80±0.72 vs 上界 87.90±0.84 统计打平；SS-P 90.00 反超；TR/MS 结构性领先；_abs 80%。

### 13 S 档泛化
- type: content / role: hero / rhythm: peak / layout: 非对称双栏（60:40）
- visual: 演进阶梯 72.92→79.17→89.58（SVG 阶梯图）
- visual_role: anchor
- density: 字数约 220 / 图 1 / 留白约 22%
- anti_pattern: 禁止省略 n=48 的样本说明；禁止把 89.58 与自报 94+ 直接比
- description: 122k token/题、48 题样本 89.58%，8 修 0 坏；四题型满分；定位在第三方协议带上方、逼近 Hindsight 91.4。

### 14 公开对比与消融
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（55:45）
- visual: 对比表 + 消融矩阵（L3）
- visual_role: evidence
- density: 字数约 300 / 图 0 / 留白约 18%
- anti_pattern: 禁止把不同协议分数并列不写口径警告；禁止只给消融结论不给数字
- description: Hindsight 复现 82.80 vs 86.80（同基准同 judge）；消融：关摘录 SS-A -7.14、关算术注记 MS -2.0、关 PREMISE CHECK _abs 57%。

### 15 交付物
- type: content / role: supporting / rhythm: valley / layout: 左大图+右侧文字
- visual: 交付物清单五列（L3）
- visual_role: evidence
- density: 字数约 280 / 图 0 / 留白约 20%
- anti_pattern: 禁止罗列文件路径；禁止把「待办」混进交付物
- description: 可运行代码 + CLI（39 单测/17 手工用例）、架构文档与迭代日志、Oracle/S 档 JSONL、评估报告、答辩材料、GitHub 仓库。

### 16 边界与展望
- type: content / role: supporting / rhythm: valley / layout: 非对称双栏（40:60）
- visual: 三条边界 + 三条下一步（L3）
- visual_role: evidence
- density: 字数约 260 / 图 0 / 留白约 20%
- anti_pattern: 禁止把边界写成免责声明；禁止承诺未验证的指标
- description: 边界：judge 噪声、MS 大池计数、压缩固有损失；下一步：官方 judge 复跑、embedding 混合检索、S 档扩样本。

### 17 结束页
- type: ending / role: hero / rhythm: peak / layout: 居中金句/巨型数字
- visual: 结论金句 + 致谢
- visual_role: anchor
- density: 字数约 40
- anti_pattern: 禁止烟花/印章装饰；禁止空泛口号
- description: 记忆系统的价值不在记住更多，而在能证明「此刻该信哪一条」。谢谢。
