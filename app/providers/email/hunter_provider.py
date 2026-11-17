"""Hunter email finder and verifier (api.hunter.io/v2).

GET /v2/email-finder  {domain, first_name, last_name} -> data.email, data.score, data.verification.status
GET /v2/email-verifier {email} -> data.status: valid | invalid | accept_all | webmail | disposable | unknown
Auth: X-API-KEY header (never the query string, so the key cannot leak into a logged URL).
A finder call that finds nothing is not charged. Rate limit is 403, an exhausted plan is 429.
Only status "valid" counts as VALID; "accept_all" is catch-all and is never delivered.
"""

import asyncio

import httpx

from app.config.settings import get_settings
from app.models.enums import EmailVerdict
from app.providers.email.email_finder import EmailResult
from app.utils.cost_tracker import CostTracker
from app.utils.http_retry import request_with_retry
from app.utils.logger import get_logger

logger = get_logger(__name__)
BASE_URL = "https://api.hunter.io/v2"
RATE_LIMITED, QUOTA_EXHAUSTED = 403, 429  # Hunter's codes, the reverse of most APIs
VERIFIER_PENDING = frozenset({202, 222})
TRANSIENT = frozenset(
    {500, 502, 503, 504}
)  # worth retrying; 403/429 are handled explicitly below  # verification still running / SMTP unreachable: retry later
_VERDICTS = {
    "valid": EmailVerdict.VALID,
    "accept_all": EmailVerdict.CATCH_ALL,
    "invalid": EmailVerdict.INVALID,
    "disposable": EmailVerdict.INVALID,
}


def to_verdict(status: str | None) -> EmailVerdict:
    """'webmail' (a personal Gmail etc.) and 'unknown' are not a verified work address."""
    return _VERDICTS.get((status or "").lower(), EmailVerdict.UNKNOWN)


class HunterProvider:
    name = "hunter"

    def __init__(self, cost: CostTracker, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL, timeout=60, headers={"X-API-KEY": self.settings.hunter_api_key}
        )
        self.exhausted = False  # plan credits ran out: stop asking for the rest of this run

    def _inr(self, usd: float) -> float:
        return usd * self.settings.usd_to_inr

    async def _get(self, path: str, params: dict) -> httpx.Response | None:
        """GET with Hunter's rate limit (403) honoured once. None when the plan is exhausted."""
        if self.exhausted:
            return None
        for attempt in range(2):
            resp = await request_with_retry(self.client, "GET", path, retry_status=TRANSIENT, params=params)
            if resp.status_code == QUOTA_EXHAUSTED:
                self.exhausted = True
                logger.warning("hunter: plan credits exhausted; skipping Hunter for the rest of this run")
                return None
            if resp.status_code == RATE_LIMITED and attempt == 0:
                await asyncio.sleep(2.0)
                continue
            return resp
        return resp

    async def find(self, first_name: str, last_name: str, domain: str) -> EmailResult:
        async with self.cost.hold(self._inr(self.settings.hunter_usd_per_found_email)):
            resp = await self._get(
                "/email-finder", {"domain": domain, "first_name": first_name, "last_name": last_name}
            )
            if resp is None or resp.status_code == 404:
                return EmailResult(
                    None, EmailVerdict.UNKNOWN, self.name, "not_found" if resp else "exhausted"
                )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            email = data.get("email")
            if email:  # billed only when an address is returned
                self.cost.record("hunter", "find", 1, self._inr(self.settings.hunter_usd_per_found_email))
        status = (data.get("verification") or {}).get("status")
        verdict = to_verdict(status) if email else EmailVerdict.UNKNOWN
        return EmailResult(email.lower() if email else None, verdict, self.name, str(status))

    async def validate(self, emails: list[str]) -> list[EmailResult]:
        results = []
        for email in emails:  # the verifier takes one address per call
            async with self.cost.hold(self._inr(self.settings.hunter_usd_per_verification)):
                resp = await self._get("/email-verifier", {"email": email})
                if resp is None:
                    results.append(EmailResult(email.lower(), EmailVerdict.UNKNOWN, self.name, "exhausted"))
                    continue
                if resp.status_code in VERIFIER_PENDING:
                    results.append(EmailResult(email.lower(), EmailVerdict.UNKNOWN, self.name, "pending"))
                    continue
                resp.raise_for_status()
                self.cost.record("hunter", "verify", 1, self._inr(self.settings.hunter_usd_per_verification))
                status = (resp.json().get("data") or {}).get("status")
            results.append(EmailResult(email.lower(), to_verdict(status), self.name, str(status)))
        return results
