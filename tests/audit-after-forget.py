r"""进阶验证：归档事件退出当前召回，但历史时刻仍可审计（零 LLM 成本）。

用法（在 dive 目录下）：
  C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe tests\audit-after-forget.py
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
sys.stdout.reconfigure(encoding="utf-8")

import cli  # noqa: E402

sh = cli.Shell(argparse.Namespace(
    data=str(ROOT / "ref/LongMemEval/data/longmemeval_oracle.json"),
    cache=str(ROOT / "results/cli-test-cache.json")))
sh.cmd_load("3a704032")
sh.cmd_forget("--ttl 365 --capacity 25 --compact 180")

arch = [e for e in sh.memory.store.events if e.status == "archived"]
e0 = arch[0]
slot = sh.memory.store.slot(e0.subject, e0.predicate)
t = e0.valid_from or e0.stated_at
hit = slot.value_at(t)
print(f"archived={len(arch)}, value_at({t:%Y-%m-%d}) -> "
      f"{'命中(可审计)' if hit else '未命中'}")
