"""The `Company` table."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.models.base import Base, TimestampMixin, UpdatedMixin, str_enum
from app.models.enums import (
    PhoneType,
)


class Company(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("domain = lower(domain)", name="domain_lowercase"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code_iso2"),
        CheckConstraint("(registry_name IS NULL) = (registry_id IS NULL)", name="registry_name_with_id"),
        # Identity: one company record per registered company, even if it is reached through several domains.
        Index(
            "uq_companies_registry",
            "registry_name",
            "registry_id",
            unique=True,
            postgresql_where="registry_id IS NOT NULL",
        ),
    )

    @validates("domain")
    def _lower_domain(self, _key, value: str) -> str:
        return value.strip().lower()

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    country_code: Mapped[str] = mapped_column(String(2))
    name: Mapped[str | None] = mapped_column(String(300))
    legal_name: Mapped[str | None] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(200))
    sub_industry: Mapped[str | None] = mapped_column(String(200))
    business_model: Mapped[str | None] = mapped_column(String(100))
    employee_band: Mapped[str | None] = mapped_column(String(50))
    revenue_band: Mapped[str | None] = mapped_column(String(50))
    hq_city: Mapped[str | None] = mapped_column(String(120))
    hq_region: Mapped[str | None] = mapped_column(String(120))
    company_phone: Mapped[str | None] = mapped_column(String(20))
    company_phone_type: Mapped[PhoneType | None] = mapped_column(str_enum(PhoneType))
    company_phone_source_url: Mapped[str | None] = mapped_column(String(1000))
    company_whatsapp: Mapped[str | None] = mapped_column(String(20))
    company_email: Mapped[str | None] = mapped_column(String(320))  # role inbox only
    maps_url: Mapped[str | None] = mapped_column(String(1000))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    # Registry data (Country Pack); null where the country has no registry, e.g. US
    registry_name: Mapped[str | None] = mapped_column(String(40))
    registry_id: Mapped[str | None] = mapped_column(String(40))
    registry_entity_type: Mapped[str | None] = mapped_column(String(80))
    registry_status: Mapped[str | None] = mapped_column(String(40))
    incorporated_on: Mapped[date | None] = mapped_column(Date)
    registry_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registry_raw: Mapped[dict] = mapped_column(JSON, default=dict)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
