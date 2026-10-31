"""Database tables. Country-agnostic: registry data lives in registry_records, keyed by jurisdiction."""
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, MetaData, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.models.enums import (
    BuyerRole, DatePrecision, DateSource, EmailSource, EmailVerdict, EvidenceKind, JobStatus,
    LeadStatus, LeadTier, PhoneSource, PhoneType,
)


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    # create_constraint: the database itself rejects any status string not defined in enums.py
    return Enum(enum_cls, native_enum=False, values_callable=lambda e: [m.value for m in e], length=40,
                create_constraint=True, name=f"ck_{enum_cls.__name__.lower()}")


NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UpdatedMixin:
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class Job(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("requested_count > 0", name="requested_count_positive"),
        CheckConstraint("freshness_days BETWEEN 1 AND 365", name="freshness_days_range"),
        CheckConstraint("cost_inr >= 0 AND cost_cap_inr > 0", name="cost_non_negative"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String(100), index=True)  # stable slug, used for never-repeat
    client_name: Mapped[str] = mapped_column(String(200))
    client_website: Mapped[str | None] = mapped_column(String(500))
    ask: Mapped[str] = mapped_column(Text)
    target_countries: Mapped[list] = mapped_column(JSON)  # e.g. ["GB"]
    requested_count: Mapped[int] = mapped_column(Integer)
    freshness_days: Mapped[int] = mapped_column(Integer, default=90)
    personalized_email: Mapped[bool] = mapped_column(Boolean, default=False)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)  # ICP + signal playbook (from the job YAML)
    suppressed_domains: Mapped[list] = mapped_column(JSON, default=list)  # client's customers/competitors
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_inr: Mapped[float] = mapped_column(Float, default=0.0)
    cost_cap_inr: Mapped[float] = mapped_column(Float)
    cost_lines: Mapped[list] = mapped_column(JSON, default=list)  # per-provider usage
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class Company(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("domain = lower(domain)", name="domain_lowercase"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code_iso2"),
        CheckConstraint("(registry_name IS NULL) = (registry_id IS NULL)", name="registry_name_with_id"),
        # Identity: one company record per registered company, even if it is reached through several domains.
        Index("uq_companies_registry", "registry_name", "registry_id", unique=True,
              postgresql_where="registry_id IS NOT NULL"),
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



class Evidence(TimestampMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("quote_verified", name="only_verified_quotes"),  # unverified claims are never stored
        CheckConstraint("event_date IS NULL OR event_date_quote IS NOT NULL", name="event_date_has_quote"),
        Index("ix_evidence_company_kind", "company_id", "kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[EvidenceKind] = mapped_column(str_enum(EvidenceKind))
    signal_type: Mapped[str | None] = mapped_column(String(80))
    claim: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(2000))
    quote: Mapped[str] = mapped_column(Text)
    evidence_date: Mapped[date | None] = mapped_column(Date)  # when the source was published (trusted sources only)
    event_date: Mapped[date | None] = mapped_column(Date)  # when the event happened, if the source states it
    event_date_quote: Mapped[str | None] = mapped_column(String(200))  # the verbatim date phrase it was parsed from
    event_key: Mapped[str | None] = mapped_column(String(120))  # same key = same real-world event
    corroborating_urls: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))  # other sources reporting the same event
    date_source: Mapped[DateSource | None] = mapped_column(str_enum(DateSource))
    date_precision: Mapped[DatePrecision | None] = mapped_column(str_enum(DatePrecision))
    quote_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    revalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobLead(TimestampMixin, Base):
    __tablename__ = "job_leads"
    __table_args__ = (
        UniqueConstraint("job_id", "company_id"),
        CheckConstraint("fit_score BETWEEN 0 AND 40", name="fit_range"),
        CheckConstraint("intent_score BETWEEN 0 AND 40", name="intent_range"),
        CheckConstraint("timing_score BETWEEN 0 AND 20", name="timing_range"),
        CheckConstraint("total_score IS NULL OR total_score = fit_score + intent_score + timing_score", name="total_is_sum"),
        CheckConstraint("status = 'rejected' OR total_score IS NOT NULL", name="non_rejected_has_score"),
        CheckConstraint("status <> 'rejected' OR reject_reason IS NOT NULL", name="rejected_has_reason"),
        CheckConstraint("status = 'rejected' OR lead_tier IS NOT NULL", name="non_rejected_has_tier"),
        Index("ix_job_leads_job_status", "job_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    fit_score: Mapped[int | None] = mapped_column(Integer)
    intent_score: Mapped[int | None] = mapped_column(Integer)
    timing_score: Mapped[int | None] = mapped_column(Integer)
    total_score: Mapped[int | None] = mapped_column(Integer)
    why_summary: Mapped[str | None] = mapped_column(Text)
    outreach_angle: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[str | None] = mapped_column(Text)
    alive_proofs: Mapped[list] = mapped_column(JSON, default=list)
    outreach_allowed: Mapped[bool | None] = mapped_column(Boolean)
    outreach_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LeadStatus] = mapped_column(str_enum(LeadStatus), default=LeadStatus.CANDIDATE)
    lead_tier: Mapped[LeadTier | None] = mapped_column(str_enum(LeadTier))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(Integer)
    # [{contact_id, subject, body, evidence_ids}] when personalized_email is on
    email_drafts: Mapped[list] = mapped_column(JSON, default=list)


class Contact(TimestampMixin, UpdatedMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("company_id", "first_name", "last_name"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
        CheckConstraint("email IS NULL OR email_verdict = 'valid'", name="email_only_if_valid"),  # never store unverified emails
        CheckConstraint("email IS NULL OR email_verified_at IS NOT NULL", name="email_has_verified_at"),
        Index("ix_contacts_email", "email"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    job_title: Mapped[str] = mapped_column(String(200))
    seniority: Mapped[str | None] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(100))
    buyer_role: Mapped[BuyerRole | None] = mapped_column(str_enum(BuyerRole))
    person_source_url: Mapped[str] = mapped_column(String(2000))
    person_source_quote: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    email_source: Mapped[EmailSource | None] = mapped_column(str_enum(EmailSource))
    email_finder_status: Mapped[str | None] = mapped_column(String(40))  # raw provider status
    email_verdict: Mapped[EmailVerdict] = mapped_column(str_enum(EmailVerdict), default=EmailVerdict.NOT_CHECKED)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    direct_phone: Mapped[str | None] = mapped_column(String(20))
    direct_phone_source: Mapped[PhoneSource | None] = mapped_column(str_enum(PhoneSource))
    direct_phone_source_url: Mapped[str | None] = mapped_column(String(2000))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))

    @validates("email")
    def _lower_email(self, _key, value: str | None) -> str | None:
        return value.strip().lower() if value else value


class Delivery(Base):
    __tablename__ = "deliveries"
    # NULLS NOT DISTINCT: a company-only delivery (no contact) can't be recorded twice either
    __table_args__ = (UniqueConstraint("client_key", "company_id", "contact_id", postgresql_nulls_not_distinct=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String(100), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Suppression(TimestampMixin, Base):
    """Opt-outs and erasure requests (UK GDPR / DPDP). Suppressed domains are never researched again and
    suppressed emails are never looked up or delivered again."""
    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("kind", "value"),
        CheckConstraint("kind IN ('email', 'domain')", name="kind_valid"),
        CheckConstraint("value = lower(value)", name="value_lowercase"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(300))
