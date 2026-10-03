"""LongMemEval 评测核心：配置、模型客户端、数据加载、提示词构造。

三条硬约束（均来自官方仓库的实证，不是设计偏好）：

1. **会话必须按 haystack_dates 排序。**
   官方 ``src/generation/run_generation.py`` 第 224-225 行即
   ``retrieved_chunks.sort(key=lambda x: x[0])``。而 oracle 档中
   324 道多会话题里有 34 道（10.5%）的会话在数组里并非按时间排列，
   不排序会让 knowledge-update 与 temporal-reasoning 直接出错。

2. **has_answer 字段必须在喂给模型前剔除。**
   官方同文件第 182-183 行显式 pop 掉该字段，否则等于向模型泄露答案位置。

3. **推理模型的思考 token 计入 completion_tokens。**
   本项目的 glm-5.2 默认开启思考，``max_tokens`` 会被思考耗尽导致
   ``message.content`` 返回空串。评测判定需要短输出，必须可关闭。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load_env(path: pathlib.Path | None = None) -> dict[str, str]:
    """极简 .env 读取，避免引入 python-dotenv 依赖。"""
    p = path or ENV_FILE
    env: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # 环境变量优先，便于 CI 覆盖
    for k in list(env) + ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
                          "JUDGE_BASE_URL", "JUDGE_API_KEY", "JUDGE_MODEL"]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------

_DOW = re.compile(r"\([A-Za-z]{3}\)\s*")
_DT_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d")


def parse_dt(s: Any) -> datetime | None:
    """解析数据集里的时间戳，形如 ``2023/04/10 (Mon) 17:50``。

    **必须保留时分。** 只取日期会把同一天内的多个会话压平，
    使排序与顺序检查同时失效——这是一个真实踩过的坑。
    """
    s = _DOW.sub("", str(s)).strip()
    for f in _DT_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 模型客户端
# --------------------------------------------------------------------------

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0
    _lock: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        import threading
        if self._lock is None:
            self._lock = threading.Lock()

    def add(self, u: Any) -> None:
        with self._lock:
            self.prompt_tokens += getattr(u, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(u, "completion_tokens", 0) or 0
            details = getattr(u, "completion_tokens_details", None)
            if details is not None:
                self.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0
            self.calls += 1

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            }


class LLMClient:
    """OpenAI 兼容客户端，带重试与思考开关。

    glm-5.3 的端点**强制开启思考**（thinking must be enabled，无法关闭），
    且思考 token 计入 max_tokens——因此命中该模型时自动切到强制思考模式：
    发送 ``thinking: enabled`` 并给 max_tokens 追加思考余量，
    保证调用方传入的 max_tokens 语义（答案预算）不变。
    """

    # 思考余量：覆盖抽取/关系分类等复杂任务的推理链长度
    REASONING_HEADROOM = 1500

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 180, max_retry: int = 4,
                 thinking: bool = False):
        from openai import OpenAI  # 延迟导入，便于 --help 不出错

        self.model = model
        self.thinking = thinking
        self.max_retry = max_retry
        # glm-5.3 无法关闭思考（端点 400 校验），自动启用强制思考模式
        self.force_reasoning = "glm-5.3" in model.lower()
        self.usage = Usage()
        self.cli = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat(self, prompt: str, max_tokens: int = 500,
             temperature: float = 0.0) -> str:
        extra: dict[str, Any] = {}
        if self.force_reasoning:
            # glm-5.3：思考必须开启；max_tokens = 调用方答案预算 + 思考余量
            extra["thinking"] = {"type": "enabled"}
            max_tokens = max_tokens + self.REASONING_HEADROOM
        else:
            # GLM-5.2 默认开启思考；关闭后思考 token 为 0，content 直接是答案
            extra["thinking"] = {"type": "enabled" if self.thinking else "disabled"}

        last_err: Exception | None = None
        for attempt in range(self.max_retry):
            try:
                r = self.cli.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    n=1,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body=extra,
                )
                if getattr(r, "usage", None):
                    self.usage.add(r.usage)
                return (r.choices[0].message.content or "").strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = min(2 ** attempt, 30)
                print(f"  [retry {attempt + 1}/{self.max_retry}] "
                      f"{type(e).__name__}: {str(e)[:120]} — {wait}s 后重试",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
        raise RuntimeError(f"调用失败 {self.max_retry} 次: {last_err}")


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------

@dataclass
class Instance:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    sessions: list[dict]          # 已按时间升序
    session_dates: list[str]
    session_ids: list[str]
    evidence_ids: list[str]
    reordered: bool               # 是否因原始顺序非时序而重排过
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith("_abs")


def load_instances(path: str | pathlib.Path) -> list[Instance]:
    """加载数据集，并**按 haystack_dates 升序重排每个实例的会话**。"""
    data = json.load(open(path, encoding="utf-8"))
    out: list[Instance] = []
    for e in data:
        ids = e["haystack_session_ids"]
        dates = e["haystack_dates"]
        sess = e["haystack_sessions"]

        pairs = list(zip(ids, dates, sess))
        keyed = [(parse_dt(p[1]), i, p) for i, p in enumerate(pairs)]
        # 解析失败的时间戳排在最后，保持稳定
        keyed.sort(key=lambda t: (t[0] is None, t[0] or datetime.min, t[1]))
        ordered = [t[2] for t in keyed]
        reordered = [p[0] for p in ordered] != list(ids)

        out.append(Instance(
            question_id=e["question_id"],
            question_type=e["question_type"],
            question=e["question"],
            answer=e["answer"],
            question_date=e["question_date"],
            sessions=[p[2] for p in ordered],
            session_dates=[p[1] for p in ordered],
            session_ids=[p[0] for p in ordered],
            evidence_ids=list(e.get("answer_session_ids", [])),
            reordered=reordered,
            raw=e,
        ))
    return out


def strip_answer_labels(session: list[dict]) -> list[dict]:
    """剔除 has_answer 标记，避免向模型泄露答案位置（官方同做法）。"""
    return [{"role": t["role"], "content": t["content"]} for t in session]


# --------------------------------------------------------------------------
# 提示词（与官方 run_generation.py 保持一致，便于横向对比）
# --------------------------------------------------------------------------

ANSWER_TEMPLATE = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{}\n\n"
    "Current Date: {}\n"
    "Question: {}\n"
    "Answer:"
)

ANSWER_TEMPLATE_COT = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history. "
    "Answer the question step by step: first extract all the relevant "
    "information, and then reason over the information to get the answer.\n\n\n"
    "History Chats:\n\n{}\n\n"
    "Current Date: {}\n"
    "Question: {}\n"
    "Answer (step by step):"
)

SESSION_TEMPLATE = "\n### Session {}:\nSession Date: {}\nSession Content:\n{}\n"


def render_session(session: list[dict], fmt: str = "nl",
                   useronly: bool = False) -> str:
    turns = [t for t in session if t["role"] == "user"] if useronly else session
    if fmt == "json":
        return "\n" + json.dumps(turns, ensure_ascii=False)
    parts = []
    for t in turns:
        parts.append("\n\n{}: {}".format(t["role"], t["content"].strip()))
    return "".join(parts)


def build_prompt(inst: Instance, context: str, cot: bool = False) -> str:
    tpl = ANSWER_TEMPLATE_COT if cot else ANSWER_TEMPLATE
    return tpl.format(context, inst.question_date, inst.question)
