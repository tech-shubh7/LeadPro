"""Trustworthy dates for signals.

Only these sources count: search-result dates, job-posting dates, article meta tags,
JSON-LD datePublished, and a date in the URL path. Homepages, about, product and
contact pages are never dated: footers like "(c) 2026" make stale pages look fresh
(htmldate's default extensive search returns 2026-01-01 for such a page).
"""
import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import htmldate
from lxml import html as lxml_html

from app.models.enums import DatePrecision, DateSource


@dataclass(frozen=True)
class TrustedDate:
    value: date
    source: DateSource
    precision: DatePrecision = DatePrecision.EXACT


_UNDATEABLE_PATH = re.compile(
    r"^/?(?:index\.\w+)?$|/(?:about|about-us|company|who-we-are|team|our-team|leadership|people|"
    r"contact|contact-us|products?|services?|solutions?|pricing|careers|jobs|privacy|terms|"
    r"refund|cookie|faq)s?(?:[/.?#]|$)",
    re.I,
)
_URL_DATE = re.compile(r"/(20\d{2})/(0[1-9]|1[0-2])(?:/(0[1-9]|[12]\d|3[01]))?(?:/|$)")
_META_PROPS = ("article:published_time", "og:article:published_time")
_RELATIVE = re.compile(
    r"(?P<n>\d+|an?|one)\s+(?P<unit>minute|hour|day|week|month|year)s?\s+ago", re.I
)
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def is_dateable_url(url: str) -> bool:
    return not _UNDATEABLE_PATH.search(urlparse(url).path or "/")


def _parse_iso(value: str) -> date | None:
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
        return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def _json_ld_dates(tree) -> list[date]:
    found: list[date] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("datePublished"), str) and (d := _parse_iso(node["datePublished"])):
                found.append(d)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            walk(json.loads(script))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def extract_page_date(page_html: str, url: str) -> TrustedDate | None:
    """Publication date of an article-like page, or None if it can't be trusted."""
    if not is_dateable_url(url):
        return None
    try:
        tree = lxml_html.fromstring(page_html)
    except (ValueError, lxml_html.etree.ParserError):
        tree = None
    if tree is not None:
        if dates := _json_ld_dates(tree):
            return TrustedDate(min(dates), DateSource.JSON_LD)
        for prop in _META_PROPS:
            for content in tree.xpath(f'//meta[@property="{prop}"]/@content'):
                if d := _parse_iso(content):
                    return TrustedDate(d, DateSource.ARTICLE_META)
        for content in tree.xpath('//*[@itemprop="datePublished"]/@content | //time[@itemprop="datePublished"]/@datetime'):
            if d := _parse_iso(content):
                return TrustedDate(d, DateSource.ARTICLE_META)
    if m := _URL_DATE.search(urlparse(url).path):
        return TrustedDate(date(int(m[1]), int(m[2]), int(m[3] or 1)), DateSource.URL_PATH)
    # Last resort for article pages: htmldate restricted to markup (no free-text/footer heuristics).
    try:
        found = htmldate.find_date(page_html, extensive_search=False, original_date=True, outputformat="%Y-%m-%d")
    except (ValueError, TypeError):
        found = None
    return TrustedDate(date.fromisoformat(found), DateSource.HTML_MARKUP) if found else None


def parse_relative_date(text: str, today: date, source: DateSource = DateSource.JOB_POSTING) -> TrustedDate | None:
    """'Posted 3 days ago' -> today - 3 days (approximate)."""
    if re.search(r"\b(today|just now)\b", text, re.I):
        return TrustedDate(today, source, DatePrecision.RELATIVE)
    if re.search(r"\byesterday\b", text, re.I):
        return TrustedDate(today - timedelta(days=1), source, DatePrecision.RELATIVE)
    m = _RELATIVE.search(text)
    if not m:
        return None
    n = 1 if m["n"].lower() in ("a", "an", "one") else int(m["n"])
    return TrustedDate(today - timedelta(days=n * _UNIT_DAYS[m["unit"].lower()]), source, DatePrecision.RELATIVE)


def within_window(d: date, today: date, days: int) -> bool:
    return today - timedelta(days=days) <= d <= today


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}
_MONTH = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_EVENT_PATTERNS = (  # (regex, kind); day-month-year, month-day-year, ISO, month-year, "in 2025"
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+(20\d{{2}})\b", re.I), "dmy"),
    (re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b", re.I), "mdy"),
    (re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b"), "iso"),
    (re.compile(rf"\b{_MONTH}\s+(20\d{{2}})\b", re.I), "my"),
    (re.compile(r"\b(?:in|during|since|throughout|back in|early|late|mid-?)\s+(20\d{2})\b", re.I), "y"),
)


def parse_event_date(text: str) -> date | None:
    """When an event happened, from a phrase like 'in March 2025' or '3 June 2026'.

    Returns the LAST day of the stated period (end of month for 'March 2025', 31 Dec for 'in 2024'),
    so an event is never made to look older than the text supports. With several dates, the latest wins.
    """
    found: list[date] = []
    taken: list[tuple[int, int]] = []  # patterns run most-precise first; a coarser overlapping match is ignored
    for pattern, kind in _EVENT_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue
            taken.append(m.span())
            try:
                if kind == "dmy":
                    found.append(date(int(m[3]), _MONTHS[m[2].lower()], int(m[1])))
                elif kind == "mdy":
                    found.append(date(int(m[3]), _MONTHS[m[1].lower()], int(m[2])))
                elif kind == "iso":
                    found.append(date(int(m[1]), int(m[2]), int(m[3])))
                elif kind == "my":
                    y, mo = int(m[2]), _MONTHS[m[1].lower()]
                    found.append(date(y, mo, calendar.monthrange(y, mo)[1]))
                else:
                    found.append(date(int(m[1]), 12, 31))
            except ValueError:  # e.g. 31 February
                continue
    return max(found) if found else None
