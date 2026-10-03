"""遗忘与生命周期策略（手册 B.1 必做能力之五）。

设计原则（与 fusion 核心保持低耦合）：

- 本模块**只**操作 ``MemoryStore`` 的公开形状（``events`` 列表及其字段），
  不 import fusion 的任何逻辑；fusion 通过一个 3 行的 ``forget()`` 委托调用。
- **遗忘 = 软归档**：事件状态置为 ``archived`` 而非物理删除。
  所有"当前有效事实"检索路径都只取 ``status == "active"``，归档即退役；
  而 ``Slot.value_at(t)`` 的时刻查询不过滤状态——**历史时刻仍可审计**，
  符合 B.1"历史版本保留方式"要求。
- 三条策略全部确定性，可解释、可复现，报告返回给调用方落日志。
- **默认不启用**：LongMemEval Oracle 档不含记忆损失实验（手册 C.1），
  评测流水线不调用本模块；交互入口（cli.py）按需启用。

策略（对应手册 B.1"基于时效、优先级或容量上限的策略"）：

1. ``ttl_days``  **时效**：事实已失效（``valid_to`` 闭合）且失效已久
   的 active 事件 → 归档。开放区间（仍为真）的事件不受 TTL 影响。
2. ``capacity``  **容量**：active 事件超过上限时，按重要性分从低到高
   归档。重要性 = 佐证轮次数 + 数值完备 + 新近度（纯确定性评分）。
3. ``compact_days`` **历史压缩**：superseded/invalidated 的老版本，
   陈述时间早于阈值且同槽位已有更新值时归档。归档后版本链渲染仍可见
   （带 archived 标签），只是不再作为候选参与召回评分。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def importance(e) -> float:
    """确定性重要性评分（越高越该留）。

    - 佐证数：被多个轮次独立提及的事实更可能是核心记忆（×2 权重）；
    - 数值完备：带精确数量/金额的事实是聚合题的骨架；
    - 新近度：最近 90 天内的事件线性加分。
    """
    n_ev = len(e.evidence or [])
    score = float(n_ev) * 2.0
    if getattr(e, "amount", None) is not None:
        score += 1.0
    anchor = e.valid_from or e.stated_at
    if anchor is not None:
        days = max((datetime.now() - anchor).days, 0)
        score += max(0.0, 3.0 - days / 30.0)     # 90 天线性衰减
    return score


def apply_lifecycle(store, *,
                    now: datetime | None = None,
                    ttl_days: int | None = None,
                    capacity: int | None = None,
                    compact_days: int | None = None) -> dict:
    """对 ``MemoryStore`` 执行生命周期策略，返回可审计的报告。

    三个参数都可独立开关（None = 跳过该策略）。只改 ``status`` 字段，
    不删除任何数据；幂等（已归档的事件不会重复处理）。
    """
    now = now or datetime.now()
    report: dict = {"policy": {"ttl_days": ttl_days, "capacity": capacity,
                               "compact_days": compact_days, "now": now.isoformat()},
                    "archived": [], "counts": {}}
    archived: list = []

    def _arch(e, reason: str) -> None:
        if e.status == "archived":                    # 幂等：不重复处理
            return
        if e.status in ("superseded", "invalidated") and reason != "compact":
            return
        e.status = "archived"
        archived.append({"reason": reason,
                         "date": (e.valid_from or e.stated_at),
                         "fact": (e.fact or e.value)[:120]})

    # 1) 时效：失效已久的状态类事实退役（开放区间 = 仍为真，不动）
    if ttl_days is not None:
        cutoff = now - timedelta(days=ttl_days)
        n0 = len(archived)
        for e in store.events:
            if e.status != "active" or e.valid_to is None:
                continue
            if e.valid_to < cutoff:
                _arch(e, "ttl")
        report["counts"]["ttl"] = len(archived) - n0

    # 2) 容量：超出上限时按重要性从低到高归档 active 事件
    if capacity is not None:
        n0 = len(archived)
        act = [e for e in store.events if e.status == "active"]
        overflow = len(act) - capacity
        if overflow > 0:
            ranked = sorted(act, key=importance)      # 低分在前
            for e in ranked[:overflow]:
                _arch(e, "capacity")
        report["counts"]["capacity"] = len(archived) - n0

    # 3) 历史压缩：老版本若同槽位已有更新值（active 或更晚的 closed 版本）
    #    且陈述久远 → 归档。value_at 时刻查询仍命中（status 不过滤）。
    if compact_days is not None:
        cutoff = now - timedelta(days=compact_days)
        n0 = len(archived)
        chains: dict[tuple, list] = {}
        for e in store.events:
            if e.status not in ("superseded", "invalidated"):
                continue
            anchor = e.stated_at or e.valid_from
            if anchor is None or anchor >= cutoff:
                continue
            chains.setdefault((e.subject, e.predicate), []).append(e)
        for _key, olds in chains.items():
            olds.sort(key=lambda e: (e.valid_from or datetime.min, e.tx_seq))
            # 链上最后一个老版本保留（最近的"曾经为真"），其余归档
            for e in olds[:-1]:
                _arch(e, "compact")
        report["counts"]["compact"] = len(archived) - n0

    report["archived"] = archived
    act_n = sum(1 for e in store.events if e.status == "active")
    report["after"] = {"events": len(store.events), "active": act_n,
                       "archived_total": len(store.events) - act_n
                       - sum(1 for e in store.events
                             if e.status in ("superseded", "invalidated"))}
    return report
