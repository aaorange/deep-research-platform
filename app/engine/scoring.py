"""信源时效分：发布时间越近分越高，0-1。

数据来源优先级：trafilatura 抽取的 published_date > URL 中的年月 > None。
无日期信息时给中性分 0.5（不惩罚也无法加分）。
"""

import re
from datetime import UTC, datetime

FRESHNESS_HALFLIFE_MONTHS = 12.0
NO_DATE_SCORE = 0.5

_MONTH_PATTERNS = [
    (re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})[-/.](\d{1,2})"), "ym"),
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?"), "ymd"),
    (re.compile(r"(\d{4})年(\d{1,2})月"), "ym"),
]


def parse_date(text: str | None) -> datetime | None:
    """从 published_date 字符串解析日期，容忍常见脏格式。"""
    if not text:
        return None
    t = text.strip()
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).replace(tzinfo=UTC)
    except ValueError:
        pass
    for pattern, kind in _MONTH_PATTERNS:
        m = pattern.search(t)
        if m:
            y = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3)) if kind == "ymd" else 1
            if 1900 <= y <= 2100 and 1 <= month <= 12:
                try:
                    return datetime(y, month, day, tzinfo=UTC)
                except ValueError:
                    continue
    return None


def date_from_url(url: str) -> datetime | None:
    """URL 路径里的 /2025/06/、/202506/ 等日期片段。"""
    for pattern, kind in _MONTH_PATTERNS:
        m = pattern.search(url)
        if m:
            y = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3)) if kind == "ymd" else 1
            if 2000 <= y <= 2100 and 1 <= month <= 12:
                try:
                    return datetime(y, month, day, tzinfo=UTC)
                except ValueError:
                    continue
    return None


def freshness_score(
    published_date: str | None,
    url: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[float, str]:
    """返回 (0-1 分, 依据)。指数衰减，半衰期 12 个月。"""
    now = now or datetime.now(UTC)

    dt = parse_date(published_date) or date_from_url(url or "")
    if dt is None:
        return NO_DATE_SCORE, "no-date"
    if dt > now:
        return NO_DATE_SCORE, "future-date"

    months = (now - dt).days / 30.44
    score = 0.5 ** (months / FRESHNESS_HALFLIFE_MONTHS)
    return round(score, 3), dt.strftime("%Y-%m-%d")
