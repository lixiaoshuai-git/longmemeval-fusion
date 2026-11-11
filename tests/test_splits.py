"""dev / test 切分的性质测试（原则 P8）。

主用例用**合成实例**验证切分逻辑本身（覆盖全部、互不重叠、按题型平衡、
顺序无关），不依赖 `ref/` 里 15 MB 的评测集——CI 环境没有那份数据，之前
三个用例在 GitHub Actions 上全部 FileNotFoundError。

真实数据只作附加校验：本地有数据集时跑一次，没有则跳过（不影响 CI 结论）。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from splits import assign

DATA = (pathlib.Path(__file__).resolve().parent.parent
        / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json")

TYPES = ("single-session-user", "single-session-assistant",
         "single-session-preference", "multi-session",
         "temporal-reasoning", "knowledge-update")


def _synthetic(per_type: int = 40):
    """6 种题型 × N 题；qid 带题型前缀，保证 md5 顺序与题型无关。"""
    return [SimpleNamespace(question_id=f"{t}::{k}", question_type=t)
            for t in TYPES for k in range(per_type)]


def test_split_covers_everything_and_is_disjoint():
    insts = _synthetic()
    table = assign(insts)
    assert set(table) == {i.question_id for i in insts}
    assert set(table.values()) <= {"dev", "test"}


def test_split_is_stratified_and_deterministic():
    insts = _synthetic()
    a = assign(insts)
    b = assign(list(reversed(insts)))          # 顺序无关
    assert a == b
    per_type: dict[str, list[int]] = {}
    for inst in insts:
        per_type.setdefault(inst.question_type, [0, 0])
        per_type[inst.question_type][0 if a[inst.question_id] == "dev" else 1] += 1
    for t, (dev, test) in per_type.items():
        assert abs(dev - test) <= 1, f"{t} 划分不平衡: {dev}/{test}"


def test_split_sizes_are_close():
    table = assign(_synthetic(per_type=41))    # 奇数：每型两侧差 1，总量差 ≤ 题型数
    n_dev = sum(1 for v in table.values() if v == "dev")
    assert abs(n_dev - (len(table) - n_dev)) <= len(TYPES)


def test_split_is_stable_under_repeated_ids():
    """同一题的重复出现应按 id 合并，而不是被分到两侧。"""
    insts = _synthetic(per_type=4) + _synthetic(per_type=4)
    table = assign(insts)
    assert len(table) == len(TYPES) * 4
    assert set(table.values()) <= {"dev", "test"}


@pytest.mark.skipif(not DATA.exists(),
                    reason="ref/ 评测集未入库（CI 无此数据），逻辑已由合成用例覆盖")
def test_real_dataset_split_shape():
    from lme_core import load_instances

    insts = load_instances(str(DATA))
    table = assign(insts)
    n_dev = sum(1 for v in table.values() if v == "dev")
    assert abs(n_dev - (len(table) - n_dev)) <= 6
    per_type: dict[str, list[int]] = {}
    for inst in insts:
        row = per_type.setdefault(inst.question_type, [0, 0])
        row[0 if table[inst.question_id] == "dev" else 1] += 1
    for t, (dev, test) in per_type.items():
        assert abs(dev - test) <= 1, f"{t} 划分不平衡: {dev}/{test}"
