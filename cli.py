#!/usr/bin/env python
"""D.I.V.E. 记忆系统交互入口（手册 B.3：多轮、跨会话 CLI）。

只依赖两个公开接口，与记忆策略实现零耦合：
  - scripts/memory.py::build_memory   （策略工厂：fusion / structured / ...）
  - scripts/lme_core.py::LLMClient    （模型客户端，.env 配置，可整体更换）

用法：
  python cli.py                                   # 纯 REPL
  python cli.py --data ref/LongMemEval/data/longmemeval_oracle.json

REPL 命令：
  /help                      命令帮助
  /load <question_id>        载入一道 LongMemEval 题的全部证据会话并 ingest
  /ingest <file> <date>      把文本文件作为一个会话写入（行前缀 U:/A: 区分角色）
  /ask <question>            基于记忆回答问题（检索过程见 /context）
  /context <question>        只显示确定性检索上下文，不调模型（可追溯性，B.5）
  /stats                     记忆库规模报告
  /forget [--ttl N] [--capacity N] [--compact N]
                             执行遗忘/生命周期策略（软归档，详见 memory_sys/lifecycle.py）
  /quit                      退出

示例：
  /load 3a704032
  /ask How many plants have I got in total?
  /forget --capacity 20
  /stats
"""
from __future__ import annotations

import argparse
import shlex
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (str(ROOT / "scripts"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lme_core import ANSWER_TEMPLATE, LLMClient, load_env, load_instances  # noqa: E402


def _parse_session_file(path: str) -> list[dict]:
    """把文本文件解析成会话轮次。行前缀 U:/A:（或 user:/assistant:）区分角色，
    无前缀的行默认 user 并与上一轮拼接（适合粘贴长段落）。"""
    turns: list[dict] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("u:", "user:")):
            turns.append({"role": "user", "content": line.split(":", 1)[1].strip()})
        elif low.startswith(("a:", "assistant:", "ai:")):
            turns.append({"role": "assistant", "content": line.split(":", 1)[1].strip()})
        elif turns:
            turns[-1]["content"] += "\n" + line
        else:
            turns.append({"role": "user", "content": line})
    if not turns:
        raise ValueError(f"{path}: 没有可解析的轮次")
    return turns


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d")


class Shell:
    def __init__(self, args: argparse.Namespace) -> None:
        env = load_env()
        self.client = LLMClient(env["LLM_BASE_URL"], env["LLM_API_KEY"],
                                env["LLM_MODEL"],
                                timeout=int(env.get("LLM_TIMEOUT", 180)),
                                max_retry=int(env.get("LLM_MAX_RETRY", 4)),
                                thinking=False)
        cache = {}
        if args.cache and Path(args.cache).exists():
            import json
            cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
        self.memory = __import__("memory").build_memory(
            "fusion", client=self.client, cache=cache,
            cache_path=args.cache)
        self.instances = None
        self.oracle_path = args.data
        self.cur_date = datetime.now().strftime("%Y-%m-%d")

    # ---------------- 命令实现 ----------------

    def cmd_load(self, arg: str) -> None:
        qid = arg.strip()
        if not qid:
            print("用法: /load <question_id>")
            return
        if self.instances is None:
            if not self.oracle_path or not Path(self.oracle_path).exists():
                print(f"找不到数据文件: {self.oracle_path}（用 --data 指定）")
                return
            print("加载数据集…")
            self.instances = {i.question_id: i
                              for i in load_instances(self.oracle_path)}
        inst = self.instances.get(qid)
        if inst is None:
            print(f"没有这道题: {qid}")
            return
        print(f"ingest {len(inst.sessions)} 个会话（逐轮抽取，走 LLM 缓存）…")
        for k, (sess, date) in enumerate(zip(inst.sessions,
                                             inst.session_dates), 1):
            self.memory.write(sess, date)
            print(f"  会话 {k}/{len(inst.sessions)} @ {date} 完成 "
                  f"| 事件 {self.memory.stats()['events']}")
        self.cur_date = inst.question_date
        print(f"当前日期设为问题日期: {self.cur_date}")

    def cmd_ingest(self, arg: str) -> None:
        try:
            path, date = arg.rsplit(None, 1)
            turns = _parse_session_file(path)
            self.memory.write(turns, _parse_date(date).strftime("%Y-%m-%d"))
            print(f"已写入 {len(turns)} 轮 @ {date} | {self.memory.stats()}")
        except ValueError as e:
            print(f"用法: /ingest <file> <YYYY-MM-DD>  ({e})")

    def _context(self, question: str) -> str:
        return self.memory.read(question, self.cur_date)

    def cmd_ask(self, arg: str) -> None:
        q = arg.strip()
        if not q:
            print("用法: /ask <question>")
            return
        ctx = self._context(q)
        prompt = ANSWER_TEMPLATE.format(ctx, self.cur_date, q)
        ans = self.client.chat(prompt, max_tokens=500)
        print(f"\n{ans}\n")
        meta = getattr(self.memory, "last_meta", None)
        if meta:
            print(f"[trace] {meta}")

    def cmd_context(self, arg: str) -> None:
        q = arg.strip()
        if not q:
            print("用法: /context <question>")
            return
        print(self._context(q))

    def cmd_stats(self, _arg: str) -> None:
        print(self.memory.stats())

    def cmd_forget(self, arg: str) -> None:
        """解析 --ttl/--capacity/--compact 并执行生命周期策略。"""
        ttl = cap = cmp_ = None
        try:
            toks = shlex.split(arg)
            i = 0
            while i < len(toks):
                if toks[i] == "--ttl":
                    ttl = int(toks[i + 1]); i += 2
                elif toks[i] == "--capacity":
                    cap = int(toks[i + 1]); i += 2
                elif toks[i] == "--compact":
                    cmp_ = int(toks[i + 1]); i += 2
                else:
                    i += 1
        except (ValueError, IndexError):
            print("用法: /forget [--ttl N] [--capacity N] [--compact N]")
            return
        if ttl is None and cap is None and cmp_ is None:
            print("至少给一个策略参数，例如: /forget --capacity 30 --compact 180")
            return
        report = self.memory.forget(ttl_days=ttl, capacity=cap,
                                    compact_days=cmp_)
        print(f"归档 {len(report['archived'])} 条: {report['counts']}")
        for a in report["archived"][:8]:
            print(f"  [{a['reason']}] {a['date']} | {a['fact']}")
        if len(report["archived"]) > 8:
            print(f"  … 共 {len(report['archived'])} 条")
        print(f"归档后: {report['after']}（archived 事件退出当前事实召回，"
              f"历史时刻查询仍可审计）")

    # ---------------- REPL ----------------

    COMMANDS = {
        "load": ("载入一道 LongMemEval 题并 ingest 其全部会话", "cmd_load"),
        "ingest": ("写入文本文件为一个会话", "cmd_ingest"),
        "ask": ("基于记忆回答", "cmd_ask"),
        "context": ("显示确定性检索上下文（不调模型）", "cmd_context"),
        "stats": ("记忆库规模", "cmd_stats"),
        "forget": ("执行遗忘/生命周期策略（软归档）", "cmd_forget"),
    }

    def repl(self) -> None:
        print("D.I.V.E. 记忆系统 CLI —— /help 查看命令，/quit 退出")
        while True:
            try:
                line = input("mem> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line in ("/quit", "/exit", "/q"):
                return
            if line == "/help":
                for name, (desc, _) in self.COMMANDS.items():
                    print(f"  /{name:<9} {desc}")
                continue
            if not line.startswith("/"):
                # 裸输入视为提问
                self.cmd_ask(line)
                continue
            cmd, _, rest = line[1:].partition(" ")
            if cmd in self.COMMANDS:
                getattr(self, self.COMMANDS[cmd][1])(rest)
            else:
                print(f"未知命令 /{cmd}（/help 查看命令）")


def main() -> None:
    ap = argparse.ArgumentParser(description="记忆系统交互入口")
    ap.add_argument("--data", default=str(ROOT / "ref/LongMemEval/data/"
                                          "longmemeval_oracle.json"),
                    help="LongMemEval oracle json 路径（/load 用）")
    ap.add_argument("--cache", default=None,
                    help="LLM 调用缓存 json（可选，复用评测缓存可加速 /load）")
    args = ap.parse_args()
    Shell(args).repl()


if __name__ == "__main__":
    main()
