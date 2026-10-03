# LongMemEval Oracle 数据分析
数据文件: ref/LongMemEval/data/longmemeval_oracle.json
实例总数: 500
字段: question_id, question_type, question, answer, question_date, haystack_dates, haystack_session_ids, haystack_sessions, answer_session_ids

## 1. 题型构成
  题型                                 题数       占比     其中 _abs
  --------------------------------------------------------------
  temporal-reasoning                133    26.6%           6
  multi-session                     133    26.6%          12
  knowledge-update                   78    15.6%           6
  single-session-user                70    14.0%           6
  single-session-assistant           56    11.2%           0
  single-session-preference          30     6.0%           0
  --------------------------------------------------------------
  合计                                500   100.0%          30

  拒答题 (_abs 后缀) 共 30 道 -> 这是独立的一类能力（前提校验）

## 2. 每题规模（每个实例 = 一道题的完整 haystack）
  会话数          min=       1  p25=       1  median=       2  p75=       2  p95=       4  max=        6  mean=      1.9
  轮次数          min=       2  p25=      12  median=      24  p75=      24  p95=      46  max=       72  mean=     21.9
  证据会话数        min=       1  p25=       1  median=       2  p75=       2  p95=       4  max=        6  mean=      1.9
  带标记轮次        min=       0  p25=       1  median=       2  p75=       2  p95=       4  max=        6  mean=      1.8
  haystack token min=     109  p25=   3,388  median=   5,588  p75=   6,761  p95=  11,861  max=   21,827  mean=  5,558.8
  单轮 token     min=       1  p25=      47  median=     114  p75=     479  p95=     640  max=    1,008  mean=    253.6

  全部 500 题 haystack 合计 token: 2,779,394

## 3. 每题会话数分布（oracle 档关键特征）
   1 个会话:  176 题  ##########################################
   2 个会话:  250 题  ############################################################
   3 个会话:   41 题  #########
   4 个会话:   19 题  ####
   5 个会话:   11 题  ##
   6 个会话:    3 题  

  只有 1 个会话的题: 176 道 (35.2%) -> 不存在跨会话更新
  >=2 个会话的题: 324 道 -> 存在更新/聚合/时序的空间

## 4. 时间跨度与时序
  跨度(天)        min=       0  p25=       0  median=       3  p75=      13  p95=     111  max=      255  mean=     21.1
  question_date 最小值: 2021/08/20  最大值: 2024/02/20
  question_date 取值数: 497

### 4.1 haystack_dates 是否按时间升序（只统计 >=2 个会话的题）
  严格升序        : 290
  严格降序        : 27
  部分乱序        : 7
  单会话题（无关）: 176
  解析失败        : 0

  >>> 需要重排的题: 34 / 324 (10.5% of 多会话题)
  >>> 结论: oracle 档的会话不是按数组顺序排列的。
      必须按 haystack_dates 排序后再 ingest，否则时间序列错乱，
      knowledge-update（取最新值）与 temporal-reasoning（顺序推理）会直接错。

### 4.2 证据密度（has_answer 标记）
  证据轮次占总轮次比例: 均值 9.2%, 中位数 8.3%
  -> 平均每 100 轮里只有约 9 轮是证据，其余为噪声

## 5. 答案形态（决定 reader 的输出格式约束）
  答案字符数        min=       1  p25=       4  median=      11  p75=      59  p95=     304  max=      604  mean=     51.9
  答案token      min=       1  p25=       2  median=       3  p75=      16  p95=      54  max=      104  mean=     10.8
  答案 <=20 字符的题: 308 道 (61.6%) -> 短事实型，可精确匹配

## 6. 原始字段样本（第一题）
  question_id      : gpt4_2655b836
  question_type    : temporal-reasoning
  question         : What was the first issue I had with my new car after its first service?
  answer           : GPS system not functioning correctly
  question_date    : 2023/04/10 (Mon) 23:07
  haystack_dates[0]: 2023/04/10 (Mon) 17:50
  会话数           : 3
  证据会话 ids     : ['answer_4be1b6b4_2', 'answer_4be1b6b4_3', 'answer_4be1b6b4_1']
