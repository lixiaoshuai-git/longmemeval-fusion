"""LLM 边界：事件抽取（入口）与问题解析（查询期）。

两处 LLM 调用的产出都是**结构化数据**：
- 抽取 → 事件数组（喂给确定性存储）
- 解析 → 结构化查询（喂给确定性执行器）
算术、过滤、排序、去重全部在 schema.py 的确定性算子里完成。
"""
from __future__ import annotations

import json
import re

from schema import CATEGORIES, PREDICATE_CARD

_PRED_LIST = ", ".join(sorted(PREDICATE_CARD))
_CAT_LIST = ", ".join(CATEGORIES)

EXTRACTION_PROMPT = """You are an information extraction engine. One chat session between a user and an assistant is given below, together with its date.

Extract facts stated in this session, as a JSON array. Cover two kinds:
1. Facts the USER states about themselves (purchases, possessions, visits, events attended or completed, plans, work, study, preferences, numbers, durations).
2. Specific facts the ASSISTANT introduces that the user may ask about later (a recommended item with a proper name, a video title, a link, a price, a named trail/route/recipe). Use subject "assistant" for these.

Output ONLY a JSON array, each item:
{{"category":"<one of: {cats}>","predicate":"<one of: {preds}>","value":"<short noun phrase>","when":"<absolute YYYY-MM-DD or start..end or unknown>","amount":<number or null>,"corrects":false}}

Rules:
- Resolve relative time using the session date ({date}): "two weeks ago" -> date-14d; "last month" -> previous calendar month. If the statement says "recently" without a date, use "unknown" rather than guessing.
- Merge repeated mentions of the SAME fact within this session into ONE item.
- Do NOT create items that merely restate or congratulate what the user already said.
- Do NOT extract generic advice lists from assistant replies as user facts. Only assistant-introduced specifics (named items, titles, links, figures) get an item with subject "assistant".
- Prefer predicates describing completed actions (complete/repair/visit/attend/acquire) over vague ones.
- amount: fill the number when the fact contains one (money, quantity, duration), else null.
- Do NOT invent anything. If nothing qualifies, output [].

Session date: {date}
Session content:
{session}
"""

PARSE_PROMPT = """Convert the question into a structured query over a memory store of events.

Output ONLY JSON:
{{"intent":"agg_count|agg_sum|fact_current|fact_at_time|list_events|unknown","subject":"user or assistant","category":"<one of: {cats}> or null","predicate":"<one of: {preds}> or null","window_days":<int or null>,"month":"<YYYY-MM or null>","before_text":"<verbatim event text the question compares against, or null>","at_text":"<date expression for fact_at_time, or null>","entities":["<key nouns>"]}}

Guides:
- "How many X did I <verb>" -> intent=agg_count, subject=user, category=category of X, predicate=<verb>.
- "How much money ..." -> intent=agg_sum, category=money (or the item's category), predicate=<spend/buy verb>.
- "What is / What was my <state>" -> fact_current, subject=user.
- "What did you recommend / say / provide" -> subject=assistant, predicate=recommend/provide/state.
- "Which did I do first / Which came first / compare X and Y" -> intent=list_events with entities=[X, Y]; NEVER use fact_at_time for ordering questions.
- "...at that time / back then" -> fact_at_time with at_text.
- "last month" -> window_days=31; "past two weeks" -> 14; "in December" -> month="YYYY-12" if the year is inferable from the question, else null and put "December" in entities.
- "before <event>" -> before_text="<event>".
- entities = the key nouns the question presumes to exist (for existence checking).

Question date: {date}
Question: {question}
"""


# ==========================================================================
# v2（fusion 策略）提示词
# 设计变化（§8.5）：事实载体 = 自由文本完整句子（数字/专名逐字保留），
# 结构化字段降级为可选索引；谓词不再封闭；对齐搬到写入期。
# ==========================================================================

TURN_EXTRACTION_PROMPT = """You extract durable facts from a chat message. Session date: {date}.

USER MESSAGE:
{user_turn}

ASSISTANT REPLY right after it (context ONLY; extract nothing from it):
{assistant_turn}

Output ONLY a JSON array. Each item is ONE atomic fact stated by the user in this message:
{{"fact":"<complete sentence>","when_raw":"<the exact time words used, or empty>","when":"<resolved YYYY-MM-DD | YYYY-MM | start..end | none>","category":"<one of: {cats}>","kind":"event|state|preference|numeric"}}

HARD RULES:
1. The fact sentence MUST be self-contained and preserve EVERY number, quantity, price, duration and proper name VERBATIM ("it lasted 3 lunches", "10 amateur comedians", "$40", "38 pre-1920 American coins"). Never drop or round numbers — a later question may ask for that exact count.
2. One statement = one item. Two different happenings = two items. Do NOT merge; do NOT pad with background.
3. ACQUISITION RULE: if the message mentions having gotten/bought/received/been given something ("I got a new X", "my new Instant Pot", "just picked up Y"), that acquisition MUST be its own fact with kind="event", even if the message is mostly about something else. Never bury an acquisition inside a usage/preference fact.
4. "when": resolve the relative time against {date} ("two weeks ago" -> {date} minus 14 days; "last month" -> previous calendar month; "in March" -> that March). Keep "when_raw" as the user's exact words (copy them verbatim — do not paraphrase). Use "none" ONLY if the sentence truly implies no time at all; for vague words like "recently" put them in when_raw unchanged.
5. PRE-ORDER RULE: for pre-orders/reservations, the user GOT the item at the stated ARRIVAL/delivery date, not at ordering time. Set "when" to the arrival date when stated, and keep both dates in the fact sentence.
6. kind: "state" = ongoing condition that can later change (where I live, my job, a collection total, a membership); "event" = one-time happening (bought, visited, watched, was born); "preference" = like/dislike/favorite; "numeric" = a count/amount attached to an event.
7. Skip small talk, greetings, questions to the assistant, and assistant replies. If nothing durable, output [].
"""

ASSISTANT_FACTS_PROMPT = """From the assistant replies below, extract ONLY concrete specifics that the ASSISTANT introduced (the user may ask about them later: "which game did you say...", "what title did you recommend...").

{assistant_turns}

Output ONLY a JSON array; each item:
{{"fact":"<complete sentence with the specific name/title/link/number verbatim>","when_raw":"","when":"none","category":"<one of: {cats}>","kind":"assistant_fact"}}

Rules:
- DO extract informational facts the assistant TAUGHT or INTRODUCED (cultural facts, definitions, how-to details, prices, dates): "the Hoop Dance is traditionally performed by skilled dancers", "the trail is 4 miles". The user may later ask "which X did you mention?" — these facts must survive.
- Do NOT extract option-list items the user was explicitly asked to CHOOSE from (menus, A-or-B picks, "which of these do you want"). Informational content is never an option list, even when it mentions several things.
- Do NOT restate what the user said. NOT congratulations.
- Extract at most 12 facts; never drop a named cultural/factual item just to stay under the cap.
- Preserve exact names/numbers verbatim.
- If nothing qualifies, output [].
"""

RELATION_PROMPT = """You maintain a temporal memory. Below: NEW candidate facts extracted from the latest session (session date {date}), and for each candidate the most similar EXISTING memories.

EXISTING ACTIVE MEMORIES:
{existing_block}

NEW CANDIDATES:
{candidates_block}

For EACH candidate, decide its relation to existing memory and output ONE JSON object per candidate, IN ORDER:
{{"id":"<candidate id>","action":"new|merge|evolve|correct|supplement","target":"<existing memory id or null>","anchor":"<YYYY-MM-DD or null>"}}

Actions:
- "new": nothing in memory covers this -> create.
- "merge": pure restatement of an existing memory (same fact, same date, nothing added) -> fold into target.
- "evolve": UPDATES a state to a new value effective from "anchor" (old value was TRUE until anchor): residence moved, job changed, a collection total restated to a NEW number ("I now have 38"), a plan/date changed. -> target required; anchor defaults to {date}.
- "correct": declares an existing memory was WRONG ("actually", "I misremembered", "to correct") -> target required; anchor = {date}.
- "supplement": ANOTHER instance added to a collection/campaign (bought ANOTHER plant, ANOTHER concert attended, ANOTHER baby born) -> create as a separate event; set target so they stay linked.

CRITICAL disambiguation:
- Different underlying happenings are NEVER "merge" — when unsure between merge and supplement, choose supplement. Over-merging destroys countable events; true duplicates are rare.
- Same happening repeated across sessions with the SAME date -> "merge".
- Stating a collection's new TOTAL is "evolve" on the total, not "supplement".
- A baby born / an item bought at a different date is "supplement" even if the item type matches an existing memory.
"""

TURN_EXTRACTION_PROMPT_ZH = """你从一条用户消息里抽取可长期记住的事实。会话日期：{date}。

用户消息：
{user_turn}

紧随其后的助手回复（只作上下文，不要从中抽取）：
{assistant_turn}

只输出 JSON 数组。每一项是用户在这条消息里陈述的**一条原子事实**：
{{"fact":"<完整中文句子，必须自足；数字、数量、价格、时长、专名逐字保留>","when_raw":"<原文时间词，没有则空字符串>","when":"<解析后的 YYYY-MM-DD | YYYY-MM | start..end | none>","category":"<one of: {cats}>","kind":"event|state|preference|numeric"}}

硬性规则：
1. 事实句必须自足，数字和专名一个都不能丢或四舍五入。
2. 一个陈述一项；两个不同事件两项；不要合并、不要补背景。
3. 提到"买了 / 收到 / 得到了 / 别人送了"时，这次获得必须单独成项，kind="event"。
4. when 要按会话日期 {date} 解析相对时间（"两周前"减 14 天，"上个月"为上一个自然月，"3月"为当年 3 月）；when_raw 必须抄原文，不要改写。模糊词如"最近"放进 when_raw，when 用 "none"。
5. kind：state=可变化的持续状态；event=一次性事件；preference=喜好；numeric=事件附带的数量。
6. 跳过寒暄、向助手提问、助手回复；没有可记事实输出 []。category 必须取自给定英文列表，其余字段值也按 schema 原样输出。
"""

ASSISTANT_FACTS_PROMPT_ZH = """从下面的助手回复里，只抽取**助手主动引入的具体信息**（用户以后可能会问"你当时推荐了哪本书 / 哪个标题 / 哪个数字"）。

{assistant_turns}

只输出 JSON 数组；每项：
{{"fact":"<完整中文句子，专名/标题/链接/数字逐字保留>","when_raw":"","when":"none","category":"<one of: {cats}>","kind":"assistant_fact"}}

规则：
- 抽取助手讲过的知识、定义、价格、日期、标题；不要复述用户说过的话，不要祝贺语。
- 用户被明确要求二选一的选项列表不抽。
- 最多 12 条；数字和专名必须逐字保留；没有则输出 []。
"""

RELATION_PROMPT_ZH = """你在维护一个时序记忆库。下面是会话日期 {date} 的新候选事实，以及每条候选最相似的已有记忆。

已有记忆：
{existing_block}

新候选：
{candidates_block}

请按顺序为每条候选输出一个 JSON 对象：
{{"id":"<候选编号>","action":"new|merge|evolve|correct|supplement","target":"<已有记忆编号或 null>","anchor":"<YYYY-MM-DD 或 null>"}}

动作含义：
- new：没有已有记忆覆盖它。
- merge：同一事实的纯复述，合并到目标。
- evolve：状态更新为新值，旧值到 anchor 为止曾为真（"我现在有 38 枚"）。
- correct：明确说以前记错了，旧值从未为真。
- supplement：同一集合新增一个实例（又买了一盆植物、又参加一场活动），新建但挂到同一目标。

关键：不同事件不要 merge；拿不准 merge 还是 supplement 时选 supplement；集合新总量是 evolve 而不是 supplement。
"""

PARSE_PROMPT_V2_ZH = """把问题转换成针对事实句记忆库的检索计划。只输出 JSON：

{{"intent":"agg_count|agg_sum|fact_current|fact_at_time|list_events|argmax|lookup|unknown","topic":"<用于模糊检索的中文关键词短语，5-15 字>","category":"<one of: {cats}> 或 null","count_kind":"events|stated_total|either|none","window_days":<整数或 null>,"month":"<YYYY-MM 或 null>","at_text":"<时刻查询的日期表达，或 null>","before_text":"<before X 比较中的原事件文本，或 null>","entities":["<问题假定存在的名词>"]}}

规则：
- "我一共 / 总共 / 有多少 X" → agg_count；"总共花了多少钱 / 合计多少" → agg_sum。
- "现在 / 目前是什么" → fact_current；"当时 / 那时候" → fact_at_time 并填 at_text。
- "哪个先 / 先后顺序 / 按时间排序" → list_events，绝不用 fact_at_time。
- "最多 / 最大 / 最长" → argmax；"你之前说 / 推荐了什么" → lookup。
- "上个月" → month 可推断时填 YYYY-MM；"过去两周" → window_days=14。
- category 只有在问题词面能直接确定类别时才填，否则一律 null；错误类别会把助手闲聊当成候选。
- topic 要包含核心名词及其常见中文词素（例如"兴趣班"补上"课 / 课程"），以便 BM25 的单字/双字召回命中。
- entities 只填问题里出现、且用户原话可能逐字出现的专名；"兴趣班 / 东西 / 活动"这类概括词放 topic，不要放 entities，避免触发假前提拒答。

问题日期：{date}
问题：{question}
"""

PARSE_PROMPT_V2 = """Convert the question into a retrieval plan over a temporal memory of fact sentences.

Output ONLY JSON:
{{"intent":"agg_count|agg_sum|fact_current|fact_at_time|list_events|argmax|lookup|unknown","topic":"<rich free-text key phrase for fuzzy retrieval, 5-15 words, e.g. 'babies born to friends and family members', 'pre-1920 American coin collection how many I have'>","category":"<one of: {cats}> or null","count_kind":"events|stated_total|either|none","window_days":<int or null>,"month":"<YYYY-MM or null>","at_text":"<date expression for fact_at_time, or null>","before_text":"<verbatim event text for 'before X' comparison, or null>","entities":["<nouns the question presumes to exist>"]}}

Guides:
- "How many X did I <verb> (during <period>)" -> intent=agg_count, count_kind=events, topic="X <verb> by me <period>".
- "How many <collection> do I have (now)" -> if the memory likely STATES a total number -> intent=fact_current, count_kind=stated_total, topic="<collection> total number I have"; if it must be tallied from separate happenings -> agg_count, count_kind=events. Use count_kind="either" when unsure.
- "How much money ..." -> agg_sum, count_kind=events.
- "What is / What was my <state>" -> fact_current (fact_at_time if "back then / at that time", with at_text).
- "Which came first / order of events" -> list_events. NEVER fact_at_time.
- "Which X did I <verb/spend> the MOST on / the most money at / the largest" -> intent=argmax, topic="X I <verb/spend> per item with amounts".
- "What did you (assistant) say/recommend" -> lookup, topic="<the thing asked about>".
- "last month" -> window_days=31; "last few months" -> 120; "past two weeks" -> 14; "in December" -> month if the year is inferable, else put "December" in topic.
- entities = nouns the question assumes exist (for existence checking).

Question date: {date}
Question: {question}
"""


def extract_json_array(text: str):
    """从 LLM 输出中稳健地取出 JSON 数组；失败返回 None。

    实测 glm-5.2 在"每候选一个对象"的任务上常输出 JSON Lines
    （逐行裸对象、无方括号包裹），这里三种形态都兜住：
    1. 标准 [...] 数组
    2. 截断数组的宽松修复
    3. 逐行/散布的 {...} 对象流（JSONL）
    """
    if not text:
        return None
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            arr = json.loads(m.group())
            return arr if isinstance(arr, list) else None
        except json.JSONDecodeError:
            # 宽松修复：截到最后一个完整对象
            seg = m.group()
            last = seg.rfind("}")
            if last > 0:
                try:
                    return json.loads(seg[: last + 1] + "]")
                except json.JSONDecodeError:
                    pass
    # JSONL / 裸对象流（扁平对象，无嵌套）
    objs = []
    for m2 in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            objs.append(json.loads(m2.group()))
        except json.JSONDecodeError:
            continue
    return objs or None


def extract_json_object(text: str):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None
