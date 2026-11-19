"""Template filters. Formatting only: nothing here reads the database or changes state."""

from datetime import UTC, date, datetime


def inr(value) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def num(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def pct(value, digits: int = 0) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def day(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat() if isinstance(value, date) else "—"


def when(value) -> str:
    """'3 days ago' for a timestamp, so a run's age is readable at a glance."""
    if not isinstance(value, datetime):
        return "—"
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 90:
        return "just now"
    for limit, size, unit in ((3600, 60, "min"), (86400, 3600, "h"), (30 * 86400, 86400, "d")):
        if seconds < limit:
            return f"{int(seconds // size)}{unit} ago"
    return moment.date().isoformat()


def age_days(value) -> str:
    """How old a signal's date is, which is what the freshness window is about."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return "—"
    days = (date.today() - value).days
    return "today" if days <= 0 else f"{days}d old"


def trunc(value, length: int = 140) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def host(value) -> str:
    """Just the hostname of a source URL, so evidence tables stay narrow."""
    text = str(value or "")
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    return text.removeprefix("www.").split("/")[0] or "—"


ACRONYMS = {
    "rfp",
    "rfq",
    "itt",
    "uk",
    "us",
    "ai",
    "crm",
    "erp",
    "saas",
    "mvp",
    "api",
    "it",
    "ui",
    "ux",
    "seo",
    "b2b",
    "b2c",
    "d2c",
    "ecommerce",
    "smb",
    "sme",
    "smes",
    "ceo",
    "cto",
    "cfo",
    "coo",
    "vp",
    "gdpr",
    "ats",
}


def title_case(value) -> str:
    """'rfp_website_app' -> 'RFP website app': readable labels from snake_case keys."""
    words = str(value or "").replace("_", " ").split()
    out = [w.upper() if w.lower() in ACRONYMS else w.lower() for w in words]
    if out and out[0].islower():
        out[0] = out[0].capitalize()
    return " ".join(out).replace("SMES", "SMEs").replace("ECOMMERCE", "eCommerce")


def path(value) -> str:
    """'https://acme.com/services/web?x=1' -> '/services/web'."""
    text = str(value or "")
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    rest = text.split("/", 1)[1] if "/" in text else ""
    return "/" + rest.split("?")[0].split("#")[0].rstrip("/") if rest.strip("/") else "home page"


def safe_url(value) -> str:
    """Only web links leave the console as hrefs; anything else (javascript:, data:) becomes inert."""
    text = str(value or "").strip()
    return text if text.lower().startswith(("https://", "http://")) else "#"


def initials(value) -> str:
    text = str(value or "").strip()
    if " " not in text and "." in text:  # a domain stands in for the name: "brightpathweb-example.com" -> "KR"
        return text.split(".")[0][:2].upper() or "?"
    parts = [p for p in text.split() if p[:1].isalnum()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


DATE_SOURCES = {
    "search_result": "search result date",
    "job_posting": "job posting date",
    "article_meta": "Article meta tags",
    "json_ld": "structured data (JSON-LD)",
    "url_path": "date in the URL",
    "html_markup": "page markup",
    "official_notice": "official notice",
    "document_meta": "the document's own creation date (PDF)",
}


def date_source(value) -> str:
    return DATE_SOURCES.get(str(value or ""), title_case(value))


def file_label(path) -> str:
    """'acme_job6_2026-10-31_no_contact.csv' -> 'No-contact CSV'; the workbook is 'Spreadsheet'."""
    name = getattr(path, "name", str(path))
    if name.endswith(".xlsx"):
        return "Spreadsheet"
    stem = name.rsplit(".", 1)[0]
    labels = {
        "no_contact": "No-contact CSV",
        "icp_only": "ICP-only CSV",
        "watchlist": "Watchlist CSV",
        "leads": "Leads CSV",
    }
    # Matched on the whole suffix: "icp_only" and "no_contact" contain underscores themselves.
    return next(
        (label for suffix, label in labels.items() if stem.endswith(f"_{suffix}")),
        name.rsplit(".", 1)[-1].upper(),
    )


def short_num(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{int(n):,}"


FILTERS = {
    "path": path,
    "date_source": date_source,
    "file_label": file_label,
    "safe_url": safe_url,
    "initials": initials,
    "short_num": short_num,
    "inr": inr,
    "num": num,
    "pct": pct,
    "day": day,
    "when": when,
    "age_days": age_days,
    "trunc": trunc,
    "host": host,
    "title_case": title_case,
}
