"""最小可运行示例：不联网、不需要 API key。

    python examples/quickstart.py

演示接口的四个动作（写入 / 检索 / 作答 / 观测）。用一个假的 llm 代替真实
模型——接口只要求对象有 `.chat(prompt, max_tokens=...)`。接真实模型见
docs/api.md 第 5 节。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_sys import Memory  # noqa: E402


class DemoLLM:
    """按提示词类型返回固定结果的假模型（只用于演示接口形状）。"""

    def chat(self, prompt: str, max_tokens: int = 512) -> str:
        if "extract durable facts" in prompt:
            if "coins" in prompt:
                return ('[{"fact": "I have 30 coins.", "when_raw": "", '
                        '"when": "none", "category": "money", "kind": "numeric"}]')
            return ('[{"fact": "I like hiking on weekends.", "when_raw": "", '
                    '"when": "none", "category": "activity", "kind": "preference"}]')
        if "Convert the question" in prompt:
            return ('{"intent": "agg_sum", "topic": "coins", '
                    '"category": "money", "entities": ["coins"]}')
        return "You have 30 coins."


def main() -> None:
    mem = Memory(llm=DemoLLM())

    added = mem.add_sessions([
        ([{"role": "user", "content": "I have 30 coins."}], "2023-05-01"),
        ([{"role": "user", "content": "I like hiking on weekends."}], "2023-05-02"),
    ])
    print(f"写入 2 个会话 → 新增 {added} 条事件")

    recall = mem.recall("How many coins do I have?", "2023-06-01")
    print(f"检索：intent={recall.plan.get('intent')}，"
          f"候选 {len(recall.candidates)} 条，段落 {len(recall.sections)} 个")
    print("上下文前 260 字：")
    print("  " + recall.context[:260].replace("\n", "\n  "))

    answer = mem.answer("How many coins do I have?", "2023-06-01")
    print(f"答案：{answer.text}（{answer.latency_s:.3f}s）")
    print("统计：", {k: mem.stats()[k] for k in ("sessions", "events", "evidence")})


if __name__ == "__main__":
    main()
