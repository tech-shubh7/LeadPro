"""Generect email API, per its OpenAPI spec (docs.generect.com/api-reference/openapi.yaml).

POST /api/v1/email/find/      {first_name, last_name, domain} -> data.valid_email|null, data.result
                               (valid|invalid|unknown), data.catch_all (bool)
POST /api/v1/email/validate/  {emails: [...]} -> data[].result (valid|invalid|catch_all|unknown), catch_all
Auth header: "Authorization: Token <key>". Finder is billed only when an email is found.
Only result == "valid" with catch_all false counts as VALID: catch-all is never delivered.
"""

import httpx

from app.config.settings import get_settings
from app.models.enums import EmailVerdict
from app.providers.email.email_finder import EmailResult
from app.utils.cost_tracker import CostTracker
from app.utils.http_retry import request_with_retry

BASE_URL = "https://api.generect.com"


def _is_true(value) -> bool:
    # Live API returns catch_all as a string ("unknown", ...) although the spec says boolean.
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"))


def to_verdict(result: str | None, catch_all) -> EmailVerdict:
    if _is_true(catch_all) or result == "catch_all":
        return EmailVerdict.CATCH_ALL
    return {"valid": EmailVerdict.VALID, "invalid": EmailVerdict.INVALID}.get(
        result or "", EmailVerdict.UNKNOWN
    )


class GenerectProvider:
    name = "generect"

    def __init__(self, cost: CostTracker, client: httpx.AsyncClient | None = None):
        self.settings = get_settings()
        self.cost = cost
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=150,
            headers={"Authorization": f"Token {self.settings.generect_api_key}"},
        )

    def _inr(self, usd: float) -> float:
        return usd * self.settings.usd_to_inr

    async def find(self, first_name: str, last_name: str, domain: str) -> EmailResult:
        async with self.cost.hold(self._inr(self.settings.generect_usd_per_found_email)):
            resp = await request_with_retry(
                self.client,
                "POST",
                "/api/v1/email/find/",
                json={"first_name": first_name, "last_name": last_name, "domain": domain},
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or {}
            email = data.get("valid_email")
            charged = (body.get("meta") or {}).get("amount_charged")
            if charged is None:  # fall back to list price: billed only when an email is found
                charged = self.settings.generect_usd_per_found_email if email else 0.0
            if charged:
                self.cost.record("generect", "find", 1, self._inr(float(charged)))
        verdict = to_verdict(data.get("result"), data.get("catch_all")) if email else EmailVerdict.UNKNOWN
        return EmailResult(email.lower() if email else None, verdict, self.name, str(data.get("result")))

    async def validate(self, emails: list[str]) -> list[EmailResult]:
        if not emails:
            return []
        price = self._inr(self.settings.generect_usd_per_validation * len(emails))
        async with self.cost.hold(price):
            resp = await request_with_retry(
                self.client, "POST", "/api/v1/email/validate/", json={"emails": emails}
            )
            resp.raise_for_status()
            body = resp.json()
            charged = (body.get("meta") or {}).get("amount_charged")
            self.cost.record(
                "generect",
                "validate",
                len(emails),
                self._inr(float(charged)) if charged is not None else price,
            )
        return [
            EmailResult(
                item.get("email", "").lower(),
                to_verdict(item.get("result"), item.get("catch_all")),
                self.name,
                str(item.get("result")),
            )
            for item in body.get("data", [])
        ]
