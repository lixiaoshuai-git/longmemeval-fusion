"""文本与语言工具：截断、语言判定、字段清洗、数字提取。"""
from __future__ import annotations

import re
from datetime import datetime


def _fmt_d(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "?"


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "..."


_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")


def _looks_zh(text: str) -> bool:
    """确定性语言判定：至少 2 个汉字且占比 ≥5% 才走中文分支。"""
    s = str(text or "")
    n = len(_CJK_CHAR.findall(s))
    return n >= 2 and n / max(1, len(s)) >= 0.05


def _clean(v, allowed=None):
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.lower() in ("null", "none", "n/a"):
        return None
    if allowed and v not in allowed:
        return None
    return v


def _first_number(s: str) -> float | None:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s).replace(",", ""))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------
# 策略主体
# --------------------------------------------------------------------------
