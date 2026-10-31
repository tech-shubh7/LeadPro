"""A Country Pack holds everything that differs by market; the pipeline core stays country-agnostic."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from app.models.enums import AliveProof


REGISTRY_MATCH_NUMBER = "company_number_on_site"  # number printed on the company's own pages: identity-grade
REGISTRY_MATCH_LEGAL_NAME = "legal_name_on_site"  # "X Limited" printed on its own pages, exactly one active match
REGISTRY_MATCH_OFFICER = "officer_and_name"  # a quote-verified decision-maker is a current officer of it, name matches
REGISTRY_MATCH_NAME = "exact_name"  # exact name to exactly one active company: good, but never used to merge records


@dataclass(frozen=True)
class RegistryRecord:
    registry: str
    registry_id: str
    legal_name: str
    entity_type: str
    status: str
    incorporated_on: date | None
    checked_at: datetime
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class OutreachDecision:
    allowed: bool
    reason: str


class CountryPack(ABC):
    code: str
    name: str
    phone_region: str
    timezone: str
    registry_name: str | None = None

    @abstractmethod
    def is_alive(self, proofs: set[AliveProof]) -> bool:
        """Enough independent proof that the business is operating now."""

    @abstractmethod
    def outreach_decision(self, record: RegistryRecord | None) -> OutreachDecision:
        """May we deliver/write cold outreach for this company? Always returns a logged reason."""
