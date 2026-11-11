"""文本与语言工具：截断、语言判定、字段清洗、数字提取。"""
from __future__ import annotations

import re
from datetime import datetime


def _fmt_d(dt: datetime | None) -> str:
    """手工格式化日期，不用 strftime。

    平台的 `%Y` 对 1000 年以前的年份**不保证补零**：glibc 输出 `1-01-01`，
    MSVC 输出 `0001-01-01`。查询窗口下界常用 `datetime.min`，于是同一份
    缓存在 Linux 与 Windows 上会渲染出不同的确定性结果——回归基线在 CI
    （ubuntu + Python 3.11）上因此判定漂移。手工格式化消除这个平台差异。
    """
    if not dt:
        return "?"
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


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
