from datetime import UTC, datetime

from app.engine.scoring import date_from_url, freshness_score, parse_date


def test_parse_date_iso():
    assert parse_date("2025-06-15").month == 6
    assert parse_date("2025-06-15T08:00:00Z") == datetime(2025, 6, 15, 8, tzinfo=UTC)
    assert parse_date("2025-06-15 08:00:00+08:00") is not None


def test_parse_date_loose_formats():
    assert parse_date("发布于 2025/3/2 上午").day == 2
    assert parse_date("2025年3月").month == 3
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("no date here") is None
    assert parse_date("9999-99-99") is None


def test_date_from_url():
    assert date_from_url("https://news.com/2025/06/15/story.html").day == 15
    assert date_from_url("https://news.com/2025/06/story.html").month == 6
    assert date_from_url("https://news.com/story.html") is None


def test_freshness_recent():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    score, basis = freshness_score("2026-08-15", now=now)
    assert score > 0.9
    assert basis == "2026-08-15"


def test_freshness_half_life():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    score, _ = freshness_score("2025-09-15", now=now)  # 恰好 12 个月
    assert abs(score - 0.5) < 0.05


def test_freshness_old_decay():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    score, _ = freshness_score("2022-09-15", now=now)  # 48 个月 = 4 个半衰期
    assert score < 0.1


def test_freshness_no_date():
    score, basis = freshness_score(None, "https://a.com/x")
    assert score == 0.5
    assert basis == "no-date"


def test_freshness_fallback_to_url_date():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    score, basis = freshness_score(None, "https://a.com/2026/01/x", now=now)
    assert 0.55 < score < 0.7  # 8.5 个月 ≈ 0.61
    assert basis == "2026-01-01"


def test_freshness_future_date_neutral():
    now = datetime(2026, 9, 15, tzinfo=UTC)
    score, basis = freshness_score("2030-01-01", now=now)
    assert score == 0.5
    assert basis == "future-date"
