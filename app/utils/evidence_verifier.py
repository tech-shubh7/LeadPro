"""Code-level check that every LLM claim is backed by a verbatim quote from a fetched page.

Fails closed: a claim whose quote cannot be found in the stored page text is dropped.
"""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

MIN_QUOTE_CHARS = 12

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        " ": " ",
        "\u200b": "",
    }
)
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    return _WS.sub(" ", text).strip().casefold()


_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|\u2026)\s*")


def _quote_in_normalized(quote: str, normalized_page: str) -> bool:
    """Quotes shortened with '...' must have every part present, in order."""
    parts = [p.strip(" .,;:\"'") for p in _ELLIPSIS.split(normalize_text(quote))]
    parts = [p for p in parts if p]
    if not parts or sum(len(p) for p in parts) < MIN_QUOTE_CHARS or any(len(p) < 4 for p in parts):
        return False
    pos = 0
    for part in parts:
        idx = normalized_page.find(part, pos)
        if idx < 0:
            return False
        pos = idx + len(part)
    return True


def quote_offset(quote: str, page_text: str) -> int | None:
    """Where the quote starts in the normalised page text (its first part, for quotes shortened with
    '...'), or None if it is not there. Stored with the evidence as its anchor in the snapshot."""
    parts = [p.strip(" .,;:\"'") for p in _ELLIPSIS.split(normalize_text(quote)) if p.strip(" .,;:\"'")]
    if not parts:
        return None
    index = normalize_text(page_text).find(parts[0])
    return index if index >= 0 else None


def quote_in_text(quote: str, page_text: str) -> bool:
    return _quote_in_normalized(quote, normalize_text(page_text))


def phrase_near_quote(phrase: str, anchor_quote: str, page_text: str, window: int = 600) -> bool:
    """`phrase` appears verbatim in the page within `window` characters of where `anchor_quote` appears
    (so a date can't be borrowed from an unrelated part of the page)."""
    page = normalize_text(page_text)
    anchor = next(
        (p.strip(" .,;:\"'") for p in _ELLIPSIS.split(normalize_text(anchor_quote)) if p.strip()), ""
    )
    needle = normalize_text(phrase).strip(" .,;:\"'")
    if len(needle) < 4 or not anchor:
        return False
    a = page.find(anchor)
    while a >= 0:
        lo, hi = max(0, a - window), a + len(anchor) + window
        if needle in page[lo:hi]:
            return True
        a = page.find(anchor, a + 1)
    return False


@dataclass(frozen=True)
class Claim:
    claim: str
    source_url: str
    quote: str


@dataclass(frozen=True)
class Rejection:
    claim: Claim
    reason: str


def canonical_url(url: str) -> str:
    """Scheme/host case, fragment, 'www.' and trailing slash don't change which page was meant."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def verify_claims(
    claims: list[Claim], page_text_by_url: dict[str, str]
) -> tuple[list[Claim], list[Rejection]]:
    verified: list[Claim] = []
    rejected: list[Rejection] = []
    normalized: dict[str, str] = {}
    by_canonical = {canonical_url(u): t for u, t in page_text_by_url.items()}
    for c in claims:
        text = page_text_by_url.get(c.source_url)
        if text is None:
            text = by_canonical.get(canonical_url(c.source_url))
        if text is None:
            rejected.append(Rejection(c, "source_url was not fetched in this run"))
            continue
        if c.source_url not in normalized:
            normalized[c.source_url] = normalize_text(text)
        if _quote_in_normalized(c.quote, normalized[c.source_url]):
            verified.append(c)
        else:
            rejected.append(Rejection(c, "quote not found in fetched page text"))
    return verified, rejected
