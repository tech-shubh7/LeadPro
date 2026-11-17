"""Re-check every lead's buying signs just before delivery.

Research can take minutes and pages come from a cache, so a job posting can close or a page can change
between being read and being delivered. Each qualifying signal is fetched again, fresh:
- the page is gone (404/410): the opportunity closed, the signal no longer counts;
- the page is there but the quote is not: the signal no longer counts;
- we cannot tell (blocked, timeout): the signal is KEPT. An uncertain check never removes a lead.
A lead is held back only when every one of its qualifying signals is confirmed gone.
"""

import asyncio
from dataclasses import dataclass
from datetime import date

from app.services.export_service import LeadResult
from app.services.scoring_service import fresh_signals
from app.services.signal_intelligence_service import can_qualify
from app.utils.evidence_verifier import quote_in_text
from app.utils.logger import get_logger

logger = get_logger(__name__)
GONE = frozenset({404, 410})
# Sources whose text came from a structured record, not the page: an award notice does not "close".
_NOT_RECHECKED = frozenset({"official_notice"})
MAX_CONCURRENT_CHECKS = 6


@dataclass(frozen=True)
class SignalCheck:
    url: str
    status: str  # "live" | "gone" | "quote_missing" | "unknown"


async def check_signal(signal, fetcher) -> SignalCheck:
    if signal.source_class in _NOT_RECHECKED:
        return SignalCheck(signal.source_url, "live")
    page = await fetcher.fetch(signal.source_url, fresh=True)
    if page.status in GONE:
        return SignalCheck(signal.source_url, "gone")
    if not page.ok:
        return SignalCheck(signal.source_url, "unknown")  # blocked or failed: no conclusion
    if signal.source_class == "job_posting" and not page.text:
        return SignalCheck(signal.source_url, "unknown")  # a live posting rendered client-side: still open
    if page.text and not quote_in_text(signal.quote, page.text) and signal.source_class != "job_posting":
        return SignalCheck(signal.source_url, "quote_missing")
    return SignalCheck(signal.source_url, "live")


async def revalidate(leads: list[LeadResult], fetcher, today: date, freshness_days: int) -> dict[int, str]:
    """{index in `leads`: why it no longer qualifies} for leads whose every qualifying signal is gone.
    ICP-only leads have no signal to re-check and always pass."""
    sem = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def guarded(signal) -> SignalCheck:
        async with sem:
            try:
                return await check_signal(signal, fetcher)
            except Exception as exc:  # a re-check that breaks is an unknown, not a removal
                logger.info("re-check of %s failed: %s", signal.source_url, type(exc).__name__)
                return SignalCheck(signal.source_url, "unknown")

    dropped: dict[int, str] = {}
    for i, lead in enumerate(leads):
        signals = [s for s in fresh_signals(lead.extraction, today, freshness_days) if can_qualify(s)]
        if not signals:
            continue
        checks = await asyncio.gather(*(guarded(s) for s in signals))
        if all(c.status in ("gone", "quote_missing") for c in checks):
            how = (
                "posting or page removed"
                if any(c.status == "gone" for c in checks)
                else "quote no longer on the page"
            )
            dropped[i] = f"buying sign no longer live at delivery ({how})"
    return dropped
