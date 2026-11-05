"""dev / test 切分的性质测试（原则 P8）。

切分必须：覆盖全部题目、互不重叠、按题型近似平衡、且可复现。
"""
from __future__ import annotations

import json
import pathlib

from lme_core import load_instances
from splits import assign

DATA = (pathlib.Path(__file__).resolve().parent.parent
        / "ref" / "LongMemEval" / "data" / "longmemeval_oracle.json")


def _insts():
    return load_instances(str(DATA))


def test_split_covers_everything_and_is_disjoint():
    insts = _insts()
    table = assign(insts)
    assert set(table) == {i.question_id for i in insts}
    assert set(table.values()) <= {"dev", "test"}


def test_split_is_stratified_and_deterministic():
    insts = _insts()
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
    insts = _insts()
    table = assign(insts)
    n_dev = sum(1 for v in table.values() if v == "dev")
    assert abs(n_dev - (len(table) - n_dev)) <= 6
