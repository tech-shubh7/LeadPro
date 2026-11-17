from datetime import date

from app.models.enums import DatePrecision, DateSource
from app.utils.dates import extract_page_date, is_dateable_url, parse_relative_date, within_window

TODAY = date(2026, 10, 30)
FOOTER_ONLY = "<html><body><p>We make packaging.</p><footer>© 2026 Acme</footer></body></html>"


def test_footer_year_never_dates_a_page():
    assert extract_page_date(FOOTER_ONLY, "https://acme.co.uk/news/some-update") is None


def test_homepage_and_about_are_never_dated():
    meta = '<html><head><meta property="article:published_time" content="2026-10-01"></head></html>'
    assert extract_page_date(meta, "https://acme.co.uk/") is None
    assert extract_page_date(meta, "https://acme.co.uk/about-us") is None
    assert not is_dateable_url("https://acme.co.uk/products/widgets")
    assert is_dateable_url("https://acme.co.uk/news/acme-opens-leeds-site")


def test_article_meta_date():
    meta = '<html><head><meta property="article:published_time" content="2026-09-14T10:00:00+01:00"></head></html>'
    d = extract_page_date(meta, "https://news.example.com/acme-raises")
    assert d.value == date(2026, 9, 14)
    assert d.source == DateSource.ARTICLE_META


def test_json_ld_date_preferred():
    html = '<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-08-02"}</script></head></html>'
    assert extract_page_date(html, "https://news.example.com/x").source == DateSource.JSON_LD


def test_url_path_date():
    d = extract_page_date("<html></html>", "https://blog.example.com/2026/09/acme-expands/")
    assert d.value == date(2026, 9, 1)
    assert d.source == DateSource.URL_PATH


def test_relative_job_dates():
    assert parse_relative_date("Posted 3 days ago", TODAY).value == date(2026, 10, 27)
    assert parse_relative_date("2 weeks ago", TODAY).value == date(2026, 10, 16)
    assert parse_relative_date("a month ago", TODAY).precision == DatePrecision.RELATIVE
    assert parse_relative_date("no date here", TODAY) is None


def test_freshness_window_rejects_old_signal():
    assert within_window(date(2026, 8, 15), TODAY, 90)
    assert not within_window(date(2024, 5, 1), TODAY, 90)
    assert not within_window(date(2026, 12, 1), TODAY, 90)


def test_article_time_tag_used_as_last_resort_but_never_footer():
    article = '<html><body><article><time datetime="2026-10-21">21 Oct</time><p>Brand launches at Ulta</p></article><footer>© 2026</footer></body></html>'
    d = extract_page_date(article, "https://theindustry.beauty/brand-lands-at-ulta/")
    assert d is not None
    assert d.value == date(2026, 10, 21)
    assert extract_page_date(FOOTER_ONLY, "https://theindustry.beauty/brand-lands-at-ulta/") is None
