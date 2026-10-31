"""Email finders behind one interface. The finder list is ordered (a waterfall); MVP uses one.

Add a second finder only if pilots show < ~60% of confirmed decision-makers get a verified
email (docs/OPEN_ITEMS.md). Any added finder must bill only for found emails.
"""
from dataclasses import dataclass
from typing import Protocol

from app.models.enums import EmailVerdict


@dataclass(frozen=True)
class EmailResult:
    email: str | None
    verdict: EmailVerdict
    provider: str
    raw_status: str


class EmailFinder(Protocol):
    name: str

    async def find(self, first_name: str, last_name: str, domain: str) -> EmailResult: ...

    async def validate(self, emails: list[str]) -> list[EmailResult]: ...


async def find_with_waterfall(finders: list[EmailFinder], first: str, last: str, domain: str) -> EmailResult | None:
    """First VALID result wins; anything else (catch-all, unknown, invalid) falls through."""
    last_result = None
    for finder in finders:
        last_result = await finder.find(first, last, domain)
        if last_result.verdict == EmailVerdict.VALID:
            return last_result
    return last_result
