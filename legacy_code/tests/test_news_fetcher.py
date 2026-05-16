"""Tests for news_fetcher.py — multi-source aggregation, date labels, temporal filtering."""

from datetime import datetime, timedelta, timezone

import pytest

from legacy_code.models import NewsArticle
from legacy_code.news_fetcher import (
    _check_source_quality,
    _deduplicate,
    _ensure_utc,
    _extract_domain,
    _parse_date,
    _parse_gdelt_date,
    _parse_rss_xml,
    format_articles_for_prompt,
)


def _make_article(
    title="Test Article",
    url="https://example.com/article1",
    hours_ago=2,
    source="example.com",
    quality_flag=None,
):
    return NewsArticle(
        title=title,
        summary="Test summary",
        source=source,
        published_date=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        url=url,
        quality_flag=quality_flag,
    )


class TestDateParsing:
    def test_relative_hours(self):
        dt = _parse_date("3 hours ago")
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 3 * 3600 + 60

    def test_relative_days(self):
        dt = _parse_date("2 days ago")
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 2 * 86400 + 60

    def test_relative_minutes(self):
        dt = _parse_date("45 minutes ago")
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 45 * 60 + 60

    def test_iso_format(self):
        dt = _parse_date("2025-01-15")
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15

    def test_iso_with_time(self):
        dt = _parse_date("2025-03-20T14:30:00Z")
        assert dt.hour == 14
        assert dt.minute == 30

    def test_human_date(self):
        dt = _parse_date("March 15, 2025")
        assert dt.month == 3
        assert dt.day == 15

    def test_empty_string_returns_now(self):
        dt = _parse_date("")
        diff = abs((datetime.now(timezone.utc) - dt).total_seconds())
        assert diff < 5

    def test_gdelt_date_format(self):
        dt = _parse_gdelt_date("20250320T143000Z")
        assert dt.year == 2025
        assert dt.month == 3
        assert dt.day == 20
        assert dt.hour == 14

    def test_gdelt_invalid_returns_now(self):
        dt = _parse_gdelt_date("invalid")
        diff = abs((datetime.now(timezone.utc) - dt).total_seconds())
        assert diff < 5

    def test_timezone_awareness(self):
        """All parsed dates should be timezone-aware."""
        cases = ["3 hours ago", "2025-01-15", "", "2025-03-20T14:30:00Z"]
        for case in cases:
            dt = _parse_date(case)
            assert dt.tzinfo is not None, f"Date '{case}' parsed without timezone"


class TestSourceQuality:
    def test_state_media_flagged(self):
        assert _check_source_quality("https://rt.com/news/article") == "state_media"
        assert _check_source_quality("https://xinhua.net/story") == "state_media"

    def test_partisan_flagged(self):
        assert _check_source_quality("https://breitbart.com/article") == "partisan"
        assert _check_source_quality("https://infowars.com/post") == "partisan"

    def test_paywall_flagged(self):
        assert _check_source_quality("https://wsj.com/article") == "paywall"
        assert _check_source_quality("https://bloomberg.com/news") == "paywall"

    def test_reliable_source_not_flagged(self):
        assert _check_source_quality("https://reuters.com/article") is None
        assert _check_source_quality("https://bbc.co.uk/news") is None

    def test_tass_flagged(self):
        """World Monitor-style expanded state media list."""
        assert _check_source_quality("https://tass.com/world") == "state_media"


class TestDeduplication:
    def test_removes_duplicate_urls(self):
        articles = [
            _make_article(url="https://example.com/a"),
            _make_article(url="https://example.com/a"),
            _make_article(url="https://example.com/b"),
        ]
        result = _deduplicate(articles)
        assert len(result) == 2

    def test_normalizes_query_params(self):
        """URLs differing only in query params are deduplicated."""
        articles = [
            _make_article(url="https://example.com/article"),
            _make_article(url="https://example.com/article?utm_source=twitter"),
        ]
        result = _deduplicate(articles)
        assert len(result) == 1

    def test_normalizes_trailing_slash(self):
        articles = [
            _make_article(url="https://example.com/article/"),
            _make_article(url="https://example.com/article"),
        ]
        result = _deduplicate(articles)
        assert len(result) == 1

    def test_preserves_order(self):
        articles = [
            _make_article(title="First", url="https://a.com/1"),
            _make_article(title="Second", url="https://b.com/2"),
        ]
        result = _deduplicate(articles)
        assert result[0].title == "First"


class TestRSSParsing:
    def test_parse_rss2(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Test Article</title>
              <link>https://example.com/test</link>
              <description>A test description</description>
              <pubDate>Mon, 20 Mar 2025 14:30:00 GMT</pubDate>
              <source>Test Source</source>
            </item>
          </channel>
        </rss>"""
        articles = _parse_rss_xml(xml, source_label="test")
        assert len(articles) == 1
        assert articles[0].title == "Test Article"
        assert articles[0].url == "https://example.com/test"
        assert articles[0].published_date.year == 2025

    def test_parse_empty_feed(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel></channel></rss>"""
        articles = _parse_rss_xml(xml)
        assert articles == []

    def test_parse_invalid_xml(self):
        articles = _parse_rss_xml("not xml at all")
        assert articles == []

    def test_parse_multiple_items(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Article 1</title>
              <link>https://example.com/1</link>
              <description>Desc 1</description>
            </item>
            <item>
              <title>Article 2</title>
              <link>https://example.com/2</link>
              <description>Desc 2</description>
            </item>
          </channel>
        </rss>"""
        articles = _parse_rss_xml(xml, source_label="test")
        assert len(articles) == 2


class TestFormatArticles:
    def test_includes_date_label(self):
        """All articles should have prominent date labels in formatted output."""
        articles = [_make_article(hours_ago=5)]
        formatted = format_articles_for_prompt(articles)
        assert "Date:" in formatted
        assert "UTC" in formatted

    def test_includes_quality_flag(self):
        articles = [_make_article(quality_flag="state_media")]
        formatted = format_articles_for_prompt(articles)
        assert "state_media" in formatted

    def test_low_news_warning(self):
        """Warning when < 3 articles found."""
        articles = [_make_article()]
        formatted = format_articles_for_prompt(articles)
        assert "NEWS QUALITY WARNING" in formatted

    def test_no_warning_enough_articles(self):
        articles = [_make_article(url=f"https://example.com/{i}") for i in range(4)]
        formatted = format_articles_for_prompt(articles)
        assert "NEWS QUALITY WARNING" not in formatted

    def test_empty_articles(self):
        formatted = format_articles_for_prompt([])
        assert "No recent news articles found" in formatted

    def test_date_format_consistency(self):
        """Date labels should use YYYY-MM-DD HH:MM UTC format."""
        articles = [_make_article()]
        formatted = format_articles_for_prompt(articles)
        # Should contain a date in the expected format
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", formatted)


class TestEnsureUTC:
    def test_naive_datetime_becomes_utc(self):
        naive = datetime(2025, 1, 15, 12, 0, 0)
        aware = _ensure_utc(naive)
        assert aware.tzinfo == timezone.utc

    def test_already_aware_unchanged(self):
        aware = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_utc(aware)
        assert result == aware

    def test_domain_extraction(self):
        assert _extract_domain("https://www.example.com/path") == "example.com"
        assert _extract_domain("https://subdomain.bbc.co.uk/news") == "subdomain.bbc.co.uk"
