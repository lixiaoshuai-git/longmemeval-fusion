"""时间：相对表达规则解析（优先）→ 模型解析值（回退）→ 合法性校验。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta


_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_MONTH_ABBR = {m[:3]: i for m, i in _MONTHS.items()}


def _month_range(y: int, m: int) -> tuple[datetime, datetime]:
    start = datetime(y, m, 1)
    nxt = datetime(y + (m == 12), (m % 12) + 1, 1)
    return start, nxt


def _shift_month(d: datetime, k: int) -> datetime:
    total = d.year * 12 + (d.month - 1) + k
    return datetime(total // 12, total % 12 + 1, 1)


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "a": 1, "an": 1, "couple": 2, "few": 3,
             "several": 3}


def _num_of(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_NUM.get(token)


def _days_in_month(y: int, m: int) -> int:
    nxt = _shift_month(datetime(y, m, 1), 1)
    return (nxt - timedelta(days=1)).day


def _shift_month_keep_day(d: datetime, k: int) -> datetime:
    """月份平移并保留日号（月末截断）。'a month ago' 的点估计基础。"""
    total = d.year * 12 + (d.month - 1) + k
    y, m = total // 12, total % 12 + 1
    return datetime(y, m, min(d.day, _days_in_month(y, m)))


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if "十" in s:
        left, right = s.split("十", 1)
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        if left in _CN_NUM or not left:
            if right in _CN_NUM or not right:
                return tens * 10 + ones
    return None


def _cjk_when(raw: str, sdate: datetime) -> tuple[datetime, datetime] | None:
    """中文相对时间兜底；无中文表达时返回 None，英文路径不变。"""
    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", raw)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d + timedelta(days=1)
        except ValueError:
            pass
    if "前天" in raw:
        d = sdate - timedelta(days=2)
        return d, d + timedelta(days=1)
    if "昨天" in raw:
        d = sdate - timedelta(days=1)
        return d, d + timedelta(days=1)
    if "今天" in raw:
        return sdate, sdate + timedelta(days=1)
    if "上个月" in raw:
        return _shift_month(sdate.replace(day=1), -1), sdate.replace(day=1)
    if "去年" in raw:
        return datetime(sdate.year - 1, 1, 1), datetime(sdate.year, 1, 1)

    for pat, unit in ((r"([0-9一二两三四五六七八九十]+)\s*天前", "day"),
                      (r"([0-9一二两三四五六七八九十]+)\s*周前", "week"),
                      (r"([0-9一二两三四五六七八九十]+)\s*个月前", "month"),
                      (r"([0-9一二两三四五六七八九十]+)\s*年前", "year")):
        m = re.search(pat, raw)
        if not m:
            continue
        n = _cn_int(m.group(1))
        if n is None:
            continue
        if unit == "day":
            d = sdate - timedelta(days=n)
        elif unit == "week":
            d = sdate - timedelta(weeks=n)
        elif unit == "month":
            d = _shift_month_keep_day(sdate, -n)
        else:
            d = _shift_month_keep_day(sdate, -12 * n)
        return d, d + timedelta(days=1)
    return None


def resolve_when_expr(raw: str, sdate: datetime) -> tuple[datetime, datetime] | None:
    """规则解析相对时间表达 → [start, end)。不命中返回 None。

    教训（rug 题）：'N months ago' 曾解析成月边界区间 [月初, sdate)，
    区间起点被当时长计算的端点 → 差出近一个月。相对表达一律给**点估计**，
    区间语义只留给查询侧的窗口过滤。
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return None

    cjk = _cjk_when(raw, sdate)
    if cjk:
        return cjk

    # 模糊表达：不解析、不信任模型的猜测，锚定到陈述日
    if re.search(r"\b(recently|lately|a while ago|some time ago|not long ago)\b", raw):
        return None

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d + timedelta(days=1)
        except ValueError:
            pass

    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*day", raw)
    if m and "ago" in raw:
        d = sdate - timedelta(days=_num_of(m.group(1)))
        return d, d + timedelta(days=1)
    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*week", raw)
    if m and "ago" in raw:
        d = sdate - timedelta(weeks=_num_of(m.group(1)))
        return d, d + timedelta(days=1)
    m = re.search(r"(a|an|couple of|a few|several|\d+|one|two|three|four|five|"
                  r"six|seven|eight|nine|ten|eleven|twelve)\s*month", raw)
    if m and "ago" in raw:
        n = _num_of(m.group(1).replace("couple of", "couple").replace("a few", "few"))
        if n is None:
            n = 1
        d = _shift_month_keep_day(sdate, -n)
        return d, d + timedelta(days=1)
    m = re.search(r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
                  r"ten|eleven|twelve)\s*year", raw)
    if m and "ago" in raw:
        d = _shift_month_keep_day(sdate, -12 * _num_of(m.group(1)))
        return d, d + timedelta(days=1)

    if "yesterday" in raw:
        d = sdate - timedelta(days=1)
        return d, d + timedelta(days=1)
    if re.search(r"\b(today|tonight|just)\b", raw):
        return sdate, sdate + timedelta(days=1)
    if "next month" in raw:
        a, b = _shift_month(sdate, 1), _shift_month(sdate, 2)
        return a, b
    if "next week" in raw:
        return sdate + timedelta(days=7), sdate + timedelta(days=14)
    if "last week" in raw:
        return sdate - timedelta(days=7), sdate
    if "last month" in raw:
        a = _shift_month(sdate.replace(day=1), -1)
        return a, sdate.replace(day=1)
    if "last year" in raw:
        return datetime(sdate.year - 1, 1, 1), datetime(sdate.year, 1, 1)

    # "in December" / "December 2023" / "in 2023"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                  r"[a-z]*\s*,?\s*(\d{4})", raw)
    if m:
        a, b = _month_range(int(m.group(2)), _MONTH_ABBR[m.group(1)])
        return a, b
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", raw)
    if m:
        mo = _MONTH_ABBR[m.group(1)]
        y = sdate.year if mo <= sdate.month else sdate.year - 1   # 记忆默认过去
        a, b = _month_range(y, mo)
        return a, b
    m = re.search(r"\b(20\d{2})\b", raw)
    if m:
        return datetime(int(m.group(1)), 1, 1), datetime(int(m.group(1)) + 1, 1, 1)
    return None


def _valid_range(rng: tuple[datetime, datetime] | None,
                 sdate: datetime) -> tuple[datetime, datetime] | None:
    """合法性校验：陈述时间 -400d .. +60d 之外的解析结果视为错误。"""
    if rng is None:
        return None
    lo, hi = rng
    if not (sdate - timedelta(days=400) <= lo <= sdate + timedelta(days=60)):
        return None
    return rng


def resolve_when(when_raw: str, when_model: str,
                 sdate: datetime) -> tuple[datetime, datetime] | None:
    for cand in (_valid_range(resolve_when_expr(when_raw, sdate), sdate),
                 _valid_range(resolve_when_expr(when_model, sdate), sdate)):
        if cand:
            return cand
    # 模型直接给绝对日期的兜底
    wm = (when_model or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", wm)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
            rng = _valid_range((d, d + timedelta(days=1)), sdate)
            if rng:
                return rng
        except ValueError:
            pass
    return None
