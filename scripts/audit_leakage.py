"""泄漏审计：检查提示词与规则里是否混入了评测集的原句 / 实体 / 数字。

对应 docs/iteration-principles.md 的 P5（提示词只写规则，不写题库）与 P6
（词表分层）。可随时运行，零模型调用。

用法：
    python audit_leakage.py            # 报告
    python audit_leakage.py --strict   # 提示词（llm_io.py）仍有命中则退出码 1

判据：命中词来自 LongMemEval 的题面/证据，属于"写进提示词就等于把测试题
喂给模型"的泄漏；`render.py` 的领域词属于 P6 的第二类问题（词表拟合），
单列报告，不计入 strict。
"""
from __future__ import annotations

import argparse
import sys
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

# 已经出现在提示词/规则里的评测集词条（人工核对自 Oracle 题面）
DENY = [
    "coin collection", "pre-1920", "38 pre-1920", "amateur comedians",
    "3 lunches", "instant pot", "hoop dance", "trail is 4 miles",
    "babies born to friends", "siblings' as brother", "internet plan' as its speed",
]
# P6：领域词（出现在 _GENERIC_TOKS / 守卫词表里就报告）
DOMAIN = ["tank", "gallon", "apartment", "undergrad", "ukulele", "smoker",
          "air fryer", "bird watching", "collection", "course", "project"]


def scan(path: pathlib.Path, needles: list[str]) -> list[tuple[int, str, str]]:
    hits = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        low = line.lower()
        for n in needles:
            if n in low:
                hits.append((i, n, line.strip()[:110]))
                break
    return hits


def question_hits(needles: list[str]) -> dict[str, int]:
    """题面命中数（数据集缺失时返回空）。"""
    data = ROOT / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json"
    if not data.exists():
        return {}
    try:
        from lme_core import load_instances
        insts = load_instances(str(data))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for n in needles:
        out[n] = sum(1 for i in insts if n in i.question.lower())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="提示词里仍有命中时退出码 1")
    args = ap.parse_args()

    llm = ROOT / "memory_sys" / "llm_io.py"
    render = ROOT / "memory_sys" / "render.py"
    prompt_hits = scan(llm, DENY)
    read_hits = scan(ROOT / "memory_sys" / "read_path.py", DENY)
    # P6 检查的是**词表集合本身**，不是文件文本（文档里举反例是允许的）
    sys.path.insert(0, str(ROOT / "memory_sys"))
    from lexicon import LANGUAGE_GENERIC
    word_hits = [(0, w, "lexicon.LANGUAGE_GENERIC") for w in DOMAIN
                 if w in LANGUAGE_GENERIC]
    # 结构性检查：核心模块不应再出现硬编码的通用词表
    if "_GENERIC_TOKS" in render.read_text(encoding="utf-8"):
        word_hits.append((0, "_GENERIC_TOKS", "render.py 仍在硬编码词表"))

    qh = question_hits(DENY)
    print(f"提示词命中（llm_io.py）: {len(prompt_hits)}")
    for ln, n, text in prompt_hits:
        print(f"  L{ln}: [{n}] {text}")
    print(f"读侧文案命中（read_path.py）: {len(read_hits)}")
    for ln, n, text in read_hits:
        print(f"  L{ln}: [{n}] {text}")
    print(f"领域词命中（render.py / lexicon.py 词表）: {len(word_hits)}"
          f"（P6：应为 0；领域词只能由 options.domain_terms 注入）")
    if qh:
        total = sum(1 for v in qh.values() if v)
        print("\n题面命中统计（仅统计 >0 的词）:")
        for n, c in qh.items():
            if c:
                print(f"  {n:<34} {c} 题")
        print(f"  命中词条数: {total}/{len(qh)}")

    bad = prompt_hits + read_hits + word_hits
    if args.strict and bad:
        print("\nSTRICT: 提示词/文案仍含评测集原句，按 P5 需改为抽象表述。")
        raise SystemExit(1)
    print("\n结论：" + ("存在泄漏（见上）" if bad else "提示词无评测集原句"))


if __name__ == "__main__":
    main()
