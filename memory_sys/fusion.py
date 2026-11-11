"""Fusion 记忆策略（v2）——吸取 Mem0 / Graphiti / Letta / Hindsight 各家之长。

对应 docs/layer2-data-structure.md §8.5 的修订方向：

1. **事实载体 = 自由文本完整句子**（Mem0/Graphiti）：数字、专名、时间原话逐字保留，
   结构化字段（valid 区间、slot 链）降级为加速索引而非唯一载体。
2. **按 user 轮次抽取**（Mem0 粒度）：一次一个聚焦 prompt，替代 v1 的单会话 mega-prompt；
   assistant 专名事实单独一路（SS-A 专项）。
3. **写入期对齐**（Mem0 update phase + 手册要求的确定性执行）：
   LLM 只做"关系分类"（new/merge/evolve/correct/supplement + 时间锚点），
   双时间轴几何由确定性代码执行（演化只封 τ_v，更正连 τ_t 一起封）。
   —— 槽位因此由**语义聚类**形成，不再依赖 value 词面相等（修复 coins 变体断裂）。
4. **模糊检索回退**（Graphiti/Hindsight）：BM25 覆盖事实句/槽位代表句/证据轮次，
   查询期零硬性键匹配；结构化查询与 BM25 并行，RRF 思想合并。
5. **证据层兜底**（Letta archival 思想）：全部 user 轮次作为地板（约 10.6% token），
   assistant 轮次按相关度取 top-k（SS-A 覆盖且不引入复述计数污染）。
   —— 有索引、按需召回，与 F.1 禁止的"全量历史拼入"有本质区别。
6. **相对时间确定性解析优先**（第一层时间归一化 + Graphiti 双时间轴）：
   先用规则解析 when_raw，规则不命中才用模型解析值，最后做 ±400 天合法性校验。

模块结构（`infra-2026-11`，本文件 1500 → 151 行；行为零变化，由
`scripts/regress.py` 的 500 题上下文指纹机械验证）：

    config.py       可调常量（阈值、上限）
    temporal.py     相对/绝对时间解析与校验
    textutil.py     截断、语言判定、字段清洗、数字提取
    write_path.py   WriteMixin：抽取 → 定时 → 关系 → 落库
    read_path.py    ReadMixin：解析 → 算子 → 检索 → 渲染输入
    render.py       RenderMixin：事实清单 / 台账 / 偏好 / 摘录 / 地板 / 守卫
    fusion.py       本文件：组合、缓存、生命周期；对外 API 保持不变

历史符号仍可从 `memory_sys.fusion` 导入（下方 re-export），tests/ 与
scripts/ 无需改动。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (  # noqa: E402,F401  (re-export)
    _EXCERPT_K, _FACT_CAP, _INTENTS, _STRONG_MIN, _STRONG_RATIO, _TURN_CHARS,
    _VERBATIM_CHARS,
)
from options import RuntimeOptions  # noqa: E402,F401  (re-export)
from read_path import ReadMixin  # noqa: E402
from render import RenderMixin  # noqa: E402
from schema import MemoryStore  # noqa: E402
from temporal import (  # noqa: E402,F401  (re-export)
    _CN_NUM, _MONTH_ABBR, _MONTHS, _WORD_NUM, _cjk_when, _cn_int, _days_in_month,
    _month_range, _num_of, _shift_month, _shift_month_keep_day, _valid_range,
    resolve_when, resolve_when_expr,
)
from textutil import (  # noqa: E402,F401  (re-export)
    _CJK_CHAR, _clean, _clip, _first_number, _fmt_d, _looks_zh,
)
from write_path import WriteMixin  # noqa: E402

PROMPT_VERSION = "fusion-v2.4"
# 四个 LLM 边界的缓存版本独立演进。初值刻意与旧 PROMPT_VERSION 相同，
# 因此旧缓存哈希逐位兼容；以后只改 PARSE_VERSION 不会让写入缓存失效。
PROMPT_VERSIONS = {
    "extract": PROMPT_VERSION,
    "assistant": PROMPT_VERSION,
    "relation": PROMPT_VERSION,
    # 2026-11-05 P5：解析提示词里的题库示例换成抽象描述，英文解析缓存因此
    # 需要重建（写入侧 extract/assistant/relation 三个 scope 不受影响）。
    "parse": "fusion-v2.5-parse",
    "answer": PROMPT_VERSION,
    # 中文分支独立版本：新增语言支持不触碰上面五条英文缓存哈希。
    "extract_zh": "fusion-zh-v1",
    "assistant_zh": "fusion-zh-v1",
    "relation_zh": "fusion-zh-v1",
    "parse_zh": "fusion-zh-v2",
}


class FusionMemory(WriteMixin, ReadMixin, RenderMixin):
    """实现 memory.py 的 MemorySystem 协议（duck typing）。

    写入 / 读取 / 渲染分别在 write_path.py、read_path.py、render.py 的 mixin 里；
    本类只负责状态、缓存与生命周期委托。
    """

    def __init__(self, client, cache: dict | None = None,
                 cache_path: str | None = None,
                 options: RuntimeOptions | None = None, **_):
        self.client = client
        self.cache = cache if cache is not None else {}
        self.cache_path = cache_path
        self.store = MemoryStore()
        self._sess_n = 0
        self._lock = threading.Lock()
        self._dirty = False
        # 全部开关收在一个不可变对象里：显式传入 > 环境变量 > 默认值。
        # 两个实例可以携带不同选项并存，不再有「改模块全局改错一份」的问题。
        self.options = options if options is not None else RuntimeOptions.from_env()
        self.last_meta: dict = {}
        self.last_trace: dict = {}

    # 兼容旧的 `mem.enable_arith` 读法（read_path/render 仍在用）。
    # 只读：要改行为请传入新的 RuntimeOptions，而不是就地改属性。
    @property
    def enable_arith(self) -> bool:
        return self.options.enable_arith

    @property
    def enable_excerpts(self) -> bool:
        return self.options.enable_excerpts

    @property
    def enable_floor(self) -> bool:
        return self.options.enable_floor

    # -- 缓存 --

    def _cached(self, key: str, fn, *, scope: str):
        version = PROMPT_VERSIONS[scope]
        full = hashlib.sha1((version + "|" + key).encode("utf-8")).hexdigest()[:16]
        with self._lock:
            hit = self.cache.get(full)
        if hit is not None:
            return hit
        val = fn()
        with self._lock:
            self.cache[full] = val
            self._dirty = True
        return val

    def save_cache(self) -> None:
        if self.cache_path and self._dirty:
            with self._lock:
                p = pathlib.Path(self.cache_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(self.cache, ensure_ascii=False),
                             encoding="utf-8")
                self._dirty = False

    def reset(self) -> None:
        self.store = MemoryStore()
        self._sess_n = 0

    def stats(self) -> dict:
        return {"events": len(self.store.events),
                "evidence": len(self.store.evidence),
                **self.store.stats}

    def forget(self, *, now=None, ttl_days=None, capacity=None,
               compact_days=None) -> dict:
        """遗忘/生命周期（手册 B.1）：软归档，不改检索逻辑本身。

        实现委托给独立的 memory_sys/lifecycle.py（只操作 store 公开形状，
        与本类零逻辑耦合）。默认不启用——评测流水线不调用；
        归档事件退出"当前事实"召回，但 value_at 时刻查询仍可审计。
        """
        from memory_sys.lifecycle import apply_lifecycle
        return apply_lifecycle(self.store, now=now, ttl_days=ttl_days,
                               capacity=capacity, compact_days=compact_days)
