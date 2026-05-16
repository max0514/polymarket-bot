"""Multi-source news aggregation inspired by World Monitor.

Sources (all free, no API key required unless noted):
  1. Google News RSS — topic-based search feeds
  2. GDELT API — global event monitoring (free, no key)
  3. RSS feeds — curated feeds from major outlets by category
  4. Brave Search API (requires NEWS_API_KEY)
  5. SerpAPI / Google News (requires NEWS_API_KEY)

Every article is date-labeled so backtesting can filter temporally.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx

from .models import NewsArticle

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERP_API_URL = "https://serpapi.com/search"
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
MAX_ARTICLES_PER_MARKET = 10  # raised from 5 — we deduplicate across sources
MAX_ARTICLES_FINAL = 5  # final count after dedup + ranking

# ── Source quality flags ─────────────────────────────────────────────────────
STATE_MEDIA = {
    "rt.com", "sputniknews.com", "xinhua.net", "globaltimes.cn",
    "presstv.ir", "tass.com", "kcna.kp", "cgtn.com",
}
PARTISAN_OUTLETS = {
    "breitbart.com", "infowars.com", "occupydemocrats.com",
    "dailykos.com", "thegatewaypundit.com", "rawstory.com",
}
PAYWALL_SOURCES = {
    "wsj.com", "ft.com", "nytimes.com", "bloomberg.com",
    "economist.com", "theathletic.com", "barrons.com",
}

# ── Curated RSS feeds by category (World Monitor style) ──────────────────────
CATEGORY_RSS_FEEDS: dict[str, list[str]] = {
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://techcrunch.com/feed/",
        "https://feeds.feedburner.com/venturebeat/SZYF",
        "https://www.wired.com/feed/rss",
    ],
    "ai": [
        "https://news.google.com/rss/search?q=artificial+intelligence+when:7d&hl=en-US&gl=US&ceid=US:en",
        "https://feeds.feedburner.com/venturebeat/SZYF",
        "https://techcrunch.com/tag/artificial-intelligence/feed/",
    ],
    "business": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://news.google.com/rss/search?q=business+when:7d&hl=en-US&gl=US&ceid=US:en",
    ],
    "crypto": [
        "https://cointelegraph.com/rss",
        "https://news.google.com/rss/search?q=cryptocurrency+when:7d&hl=en-US&gl=US&ceid=US:en",
    ],
    "finance": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://news.google.com/rss/search?q=stock+market+finance+when:7d&hl=en-US&gl=US&ceid=US:en",
    ],
    "geopolitics": [
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://news.google.com/rss/search?q=geopolitics+when:7d&hl=en-US&gl=US&ceid=US:en",
    ],
}

# ── Public API ───────────────────────────────────────────────────────────────


async def fetch_news(
    question: str,
    api_key: str = "",
    api_type: str = "brave",
    days_back: int = 7,
    category: str = "",
    cutoff_date: Optional[datetime] = None,
) -> list[NewsArticle]:
    """Aggregate news from multiple sources, deduplicate, and rank.

    Args:
        question: The market question to search for.
        api_key: API key for paid search (Brave / SerpAPI). Empty = skip.
        api_type: "brave" or "serpapi" (only used if api_key provided).
        days_back: How many days of news to look back.
        category: Market category for RSS feed selection.
        cutoff_date: If set, discard articles published AFTER this date
                     (used in backtesting to prevent look-ahead bias).
    """
    all_articles: list[NewsArticle] = []

    # 1. Google News RSS (free, no API key)
    gn_articles = await _fetch_google_news_rss(question, days_back)
    all_articles.extend(gn_articles)

    # 2. GDELT (free, no API key)
    gdelt_articles = await _fetch_gdelt(question, days_back)
    all_articles.extend(gdelt_articles)

    # 3. Category RSS feeds (free, no API key)
    if category:
        rss_articles = await _fetch_category_rss(category, days_back)
        all_articles.extend(rss_articles)

    # 4. Paid search API (Brave or SerpAPI)
    if api_key:
        if api_type == "brave":
            paid = await _fetch_brave(question, api_key, days_back)
        elif api_type == "serpapi":
            paid = await _fetch_serpapi(question, api_key, days_back)
        else:
            paid = []
        all_articles.extend(paid)

    # Apply temporal cutoff for backtesting
    if cutoff_date:
        cutoff_aware = _ensure_utc(cutoff_date)
        all_articles = [
            a for a in all_articles
            if _ensure_utc(a.published_date) <= cutoff_aware
        ]

    # Filter to only articles within days_back window
    earliest = _now_utc() - timedelta(days=days_back)
    if cutoff_date:
        earliest = _ensure_utc(cutoff_date) - timedelta(days=days_back)
    all_articles = [
        a for a in all_articles
        if _ensure_utc(a.published_date) >= earliest
    ]

    # Deduplicate by URL
    all_articles = _deduplicate(all_articles)

    # Rank: prefer recent + quality sources
    all_articles.sort(key=lambda a: a.published_date, reverse=True)

    final = all_articles[:MAX_ARTICLES_FINAL]
    logger.info(
        "Aggregated %d articles (from %d raw) for: %s",
        len(final), len(all_articles), question[:60],
    )
    return final


# ── Google News RSS (free) ───────────────────────────────────────────────────


async def _fetch_google_news_rss(
    query: str, days_back: int
) -> list[NewsArticle]:
    """Fetch news via Google News RSS feed (no API key needed)."""
    encoded = quote_plus(f"{query} when:{days_back}d")
    url = f"{GOOGLE_NEWS_RSS}?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Google News RSS failed: %s", e)
            return []

    return _parse_rss_xml(resp.text, source_label="Google News")


# ── GDELT (free) ────────────────────────────────────────────────────────────


async def _fetch_gdelt(query: str, days_back: int) -> list[NewsArticle]:
    """Fetch articles from GDELT Doc API (free, no key required).

    GDELT indexes news from 100+ languages and thousands of outlets worldwide.
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": "10",
        "format": "json",
        "timespan": f"{days_back}days",
        "sort": "DateDesc",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(GDELT_DOC_API, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("GDELT API failed: %s", e)
            return []

    try:
        data = resp.json()
    except Exception:
        logger.warning("GDELT returned non-JSON response")
        return []

    articles_data = data.get("articles", [])
    articles = []
    for item in articles_data[:MAX_ARTICLES_PER_MARKET]:
        pub_date = _parse_gdelt_date(item.get("seendate", ""))
        url = item.get("url", "")
        articles.append(NewsArticle(
            title=item.get("title", ""),
            summary=item.get("title", ""),  # GDELT doesn't provide summaries
            source=item.get("domain", _extract_domain(url)),
            published_date=pub_date,
            url=url,
            quality_flag=_check_source_quality(url),
        ))

    return articles


# ── Category RSS feeds ──────────────────────────────────────────────────────


async def _fetch_category_rss(
    category: str, days_back: int
) -> list[NewsArticle]:
    """Fetch articles from curated RSS feeds for a given category."""
    cat_lower = category.lower()
    feeds = CATEGORY_RSS_FEEDS.get(cat_lower, [])
    if not feeds:
        # Try partial match
        for key, feed_list in CATEGORY_RSS_FEEDS.items():
            if key in cat_lower or cat_lower in key:
                feeds = feed_list
                break

    if not feeds:
        return []

    all_articles: list[NewsArticle] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for feed_url in feeds[:3]:  # limit to 3 feeds per category
            try:
                resp = await client.get(feed_url, follow_redirects=True)
                resp.raise_for_status()
                parsed = _parse_rss_xml(resp.text, source_label=_extract_domain(feed_url))
                all_articles.extend(parsed[:5])
            except httpx.HTTPError as e:
                logger.debug("RSS feed %s failed: %s", feed_url, e)
                continue

    return all_articles


# ── Brave Search (paid) ─────────────────────────────────────────────────────


async def _fetch_brave(
    question: str, api_key: str, days_back: int
) -> list[NewsArticle]:
    """Fetch news from Brave Search API."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": question,
        "count": 10,
        "freshness": f"pd{days_back}",
        "text_decorations": False,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(BRAVE_SEARCH_URL, headers=headers, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Brave Search failed: %s", e)
            return []

    data = resp.json()
    results = data.get("web", {}).get("results", [])

    articles = []
    for r in results:
        url = r.get("url", "")
        article = NewsArticle(
            title=r.get("title", ""),
            summary=r.get("description", ""),
            source=r.get("profile", {}).get("name", _extract_domain(url)),
            published_date=_parse_date(r.get("age", "")),
            url=url,
            quality_flag=_check_source_quality(url),
        )
        articles.append(article)
        if len(articles) >= MAX_ARTICLES_PER_MARKET:
            break

    return articles


# ── SerpAPI (paid) ──────────────────────────────────────────────────────────


async def _fetch_serpapi(
    question: str, api_key: str, days_back: int
) -> list[NewsArticle]:
    """Fetch news from SerpAPI (Google News)."""
    params = {
        "engine": "google_news",
        "q": question,
        "api_key": api_key,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(SERP_API_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("SerpAPI failed: %s", e)
            return []

    data = resp.json()
    results = data.get("news_results", [])

    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    for r in results:
        url = r.get("link", "")
        pub_date = _parse_date(r.get("date", ""))
        if _ensure_utc(pub_date) < cutoff:
            continue

        article = NewsArticle(
            title=r.get("title", ""),
            summary=r.get("snippet", ""),
            source=r.get("source", {}).get("name", _extract_domain(url)),
            published_date=pub_date,
            url=url,
            quality_flag=_check_source_quality(url),
        )
        articles.append(article)
        if len(articles) >= MAX_ARTICLES_PER_MARKET:
            break

    return articles


# ── RSS XML parser ──────────────────────────────────────────────────────────


def _parse_rss_xml(xml_text: str, source_label: str = "") -> list[NewsArticle]:
    """Parse RSS/Atom XML into NewsArticle list."""
    articles = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.debug("Failed to parse RSS XML from %s", source_label)
        return []

    # Handle RSS 2.0
    for item in root.iter("item"):
        title = _xml_text(item, "title")
        link = _xml_text(item, "link")
        desc = _xml_text(item, "description")
        pub_date_str = _xml_text(item, "pubDate")
        source = _xml_text(item, "source") or source_label

        pub_date = _parse_rfc2822(pub_date_str) if pub_date_str else _now_utc()

        if title and link:
            articles.append(NewsArticle(
                title=title,
                summary=desc[:500] if desc else "",
                source=source,
                published_date=pub_date,
                url=link,
                quality_flag=_check_source_quality(link),
            ))

    # Handle Atom feeds
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = _xml_text(entry, "atom:title", ns) or _xml_text(entry, "title")
        link_el = entry.find("atom:link", ns) or entry.find("link")
        link = (link_el.get("href", "") if link_el is not None else "")
        summary = _xml_text(entry, "atom:summary", ns) or _xml_text(entry, "atom:content", ns) or ""
        updated = _xml_text(entry, "atom:updated", ns) or _xml_text(entry, "atom:published", ns) or ""

        pub_date = _parse_date(updated) if updated else _now_utc()

        if title and link:
            articles.append(NewsArticle(
                title=title,
                summary=summary[:500],
                source=source_label,
                published_date=pub_date,
                url=link,
                quality_flag=_check_source_quality(link),
            ))

    return articles


def _xml_text(
    element: ET.Element, tag: str, ns: Optional[dict] = None
) -> str:
    """Safely extract text from an XML element."""
    child = element.find(tag, ns) if ns else element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _deduplicate(articles: list[NewsArticle]) -> list[NewsArticle]:
    """Remove duplicate articles by URL, keeping the first occurrence."""
    seen: set[str] = set()
    unique = []
    for a in articles:
        normalized = a.url.split("?")[0].rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            unique.append(a)
    return unique


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return "unknown"


def _check_source_quality(url: str) -> str | None:
    """Flag potentially unreliable sources."""
    domain = _extract_domain(url)
    if domain in STATE_MEDIA:
        return "state_media"
    if domain in PARTISAN_OUTLETS:
        return "partisan"
    if domain in PAYWALL_SOURCES:
        return "paywall"
    return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_rfc2822(date_str: str) -> datetime:
    """Parse RFC 2822 date (common in RSS feeds)."""
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return _parse_date(date_str)


def _parse_gdelt_date(date_str: str) -> datetime:
    """Parse GDELT seendate format: YYYYMMDDTHHmmSSZ."""
    try:
        return datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _now_utc()


def _parse_date(date_str: str) -> datetime:
    """Best-effort date parsing with timezone awareness."""
    if not date_str:
        return _now_utc()

    # Handle relative dates like "2 hours ago", "3 days ago"
    date_str_lower = date_str.lower()
    if "hour" in date_str_lower:
        try:
            hours = int("".join(c for c in date_str_lower.split("hour")[0] if c.isdigit()))
            return _now_utc() - timedelta(hours=hours)
        except ValueError:
            pass
    if "minute" in date_str_lower:
        try:
            minutes = int("".join(c for c in date_str_lower.split("minute")[0] if c.isdigit()))
            return _now_utc() - timedelta(minutes=minutes)
        except ValueError:
            pass
    if "day" in date_str_lower:
        try:
            days = int("".join(c for c in date_str_lower.split("day")[0] if c.isdigit()))
            return _now_utc() - timedelta(days=days)
        except ValueError:
            pass

    # Try ISO format variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    # Try RFC 2822
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass

    return _now_utc()


def format_articles_for_prompt(articles: list[NewsArticle]) -> str:
    """Format articles with date labels for Claude's prompt.

    Each article includes a prominent date label so Claude can reason
    about recency and temporal ordering of evidence.
    """
    if not articles:
        return "No recent news articles found."

    lines = []
    for i, a in enumerate(articles, 1):
        flag = f" [⚠️ {a.quality_flag}]" if a.quality_flag else ""
        date_label = _ensure_utc(a.published_date).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"Article {i}{flag}:\n"
            f"  📅 Date: {date_label}\n"
            f"  Title: {a.title}\n"
            f"  Source: {a.source}\n"
            f"  Summary: {a.summary}\n"
            f"  URL: {a.url}"
        )

    news_quality = len(articles)
    quality_note = ""
    if news_quality < 3:
        quality_note = (
            "\n\n⚠️ NEWS QUALITY WARNING: Only {n} article(s) found. "
            "Low evidence — estimate with lower confidence."
        ).format(n=news_quality)

    return "\n\n".join(lines) + quality_note
