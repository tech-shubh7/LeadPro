"""Step 6: XLSX (Leads / Good leads - no verified contact / Methodology) + matching CSVs."""

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from app.config.constants import WORLDWIDE
from app.config.settings import get_settings
from app.models.enums import LeadTier
from app.schemas.job_spec_schema import JobSpec
from app.services.contact_service import DecisionMaker
from app.services.draft_service import Draft
from app.services.extraction_service import Extraction
from app.services.research_service import ResearchBundle
from app.services.scoring_service import Score, fresh_signals
from app.services.signal_intelligence_service import TIER_ORDER, SignalStack

SIGNAL_COLUMNS = [
    f"signal_{i}_{col}" for i in (1, 2, 3) for col in ("type", "detail", "date", "date_basis", "source_url")
]
COMPANY_COLUMNS = [
    "lead_id",
    "lead_tier",
    "confidence_tier",
    "signal_summary",
    "rank",
    "total_score",
    "fit_score",
    "intent_score",
    "timing_score",
    "company_name",
    "legal_name",
    "company_domain",
    "website_url",
    "country",
    "description",
    "company_phone",
    "company_phone_type",
    "company_phone_source",
    "company_whatsapp",
    "company_email",
    "registry",
    "registry_id",
    "registry_match",
    "registry_entity_type",
    "registry_status",
    "incorporated_on",
    "alive_proofs",
    "outreach_allowed",
    "outreach_reason",
    "website_access",
    *SIGNAL_COLUMNS,
    "why_this_lead",
    "suggested_outreach_angle",
    "risks_or_disqualifiers",
    "data_freshness_date",
]
PERSON_COLUMNS = [
    "first_name",
    "last_name",
    "full_name",
    "job_title",
    "buyer_role",
    "work_email",
    "email_status",
    "email_source",
    "person_source_url",
]
DRAFT_COLUMNS = ["email_subject", "email_body"]
# Signals-only runs: one row per company, the prospect and the dated reason to call now. Nothing about people,
# so the sheet carries no personal data. Identity columns (domain, legal name, registry id) are what a CRM or
# a contact tool (Apollo, Hunter) matches on.
SIGNALS_ONLY_COLUMNS = [
    "rank",
    "company_name",
    "company_domain",
    "website_url",
    "country",
    "confidence_tier",
    "signal_summary",
    *SIGNAL_COLUMNS,
    "why_this_lead",
    "description",
    "legal_name",
    "registry_id",
    "total_score",
    "lead_id",
    "data_freshness_date",
]
ICP_SHEET = "ICP fit - no strong signal"
WATCHLIST_SHEET = "Watchlist - near misses"
# Company + signal, plus why it fell short: the score parts and the judge's reasoning and risks.
WATCHLIST_COLUMNS = [*SIGNALS_ONLY_COLUMNS, "fit_score", "intent_score", "timing_score", "why_not_qualified"]
NO_CONTACT_COLUMNS = [*COMPANY_COLUMNS, "decision_makers_found_without_verified_email", "why_no_contact"]


# Text a spreadsheet would run as a formula: '=HYPERLINK(...)', '+cmd|...', '@SUM(...)', or one led by a
# tab/CR. Every value here comes from web pages we do not control (company names, quotes, drafted
# emails), so any such cell is prefixed with an apostrophe, which Excel/Sheets/LibreOffice show as text.
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


_PHONE = re.compile(r"^\+\d{6,15}$")  # an E.164 number cannot execute; keep it readable


def safe_cell(value):
    if isinstance(value, str) and value.startswith(FORMULA_LEAD) and not _PHONE.match(value):
        return "'" + value
    return value


def _safe_row(row: dict) -> dict:
    return {k: safe_cell(v) for k, v in row.items()}


@dataclass
class LeadResult:
    lead_id: str
    bundle: ResearchBundle
    extraction: Extraction
    score: Score
    registry: dict  # registry_name, registry_id, entity_type, status, incorporated_on, legal_name
    alive_proofs: list[str]
    outreach_allowed: bool
    outreach_reason: str
    people: list[DecisionMaker]
    tier: LeadTier = LeadTier.SIGNAL
    stack: SignalStack = field(default_factory=lambda: SignalStack("", (), 0, 0, ""))  # combined buying signs
    drafts: dict[tuple[str, str], Draft] = field(default_factory=dict)
    company_id: int | None = None
    contact_ids: dict[tuple[str, str], int] = field(default_factory=dict)  # set once persisted
    contacts_wanted: bool = True  # False in a signals-only run: the company + signal is the whole lead
    watchlist: bool = False  # a near miss: fresh signal, score just under the bar (LeadStatus.WATCHLIST)

    @property
    def verified_people(self) -> list[DecisionMaker]:
        return [p for p in self.people if p.email]

    @property
    def deliverable(self) -> bool:
        """Complete for its run's output mode. The one rule the run, revalidation, persistence and export
        all share, so a lead counted as ready is always the lead on the Leads sheet. A watchlist lead never
        is: it fell short of the bar, and goes on its own sheet."""
        return not self.watchlist and (bool(self.verified_people) or not self.contacts_wanted)


def rank_key(lead: LeadResult) -> tuple[int, int]:
    """Sort key: confidence tier first (strong, good, weak), then highest score. Shared by the run (delivery
    order) and the export (sheet order), so a lead's rank in the database is its rank in the spreadsheet."""
    return TIER_ORDER.get(lead.stack.tier, 3), -lead.score.total


def company_row(lead: LeadResult, rank: int, spec: JobSpec, today: date) -> dict:
    b, e, s = lead.bundle, lead.extraction, lead.score
    company_phone = next((p for p in b.phones if p.source.value != "whatsapp_link"), None)
    whatsapp = next((p for p in b.phones if p.source.value == "whatsapp_link"), None)
    row = {
        "lead_id": lead.lead_id,
        "lead_tier": lead.tier.value,
        "confidence_tier": lead.stack.tier,
        "signal_summary": lead.stack.summary,
        "rank": rank,
        "total_score": s.total,
        "fit_score": s.fit,
        "intent_score": s.intent,
        "timing_score": s.timing,
        "company_name": e.company_name,
        "legal_name": lead.registry.get("legal_name", ""),
        "company_domain": b.domain,
        "website_url": b.homepage.final_url if b.homepage else "",
        # A worldwide run does not verify each company's country, so none is claimed.
        "country": "" if spec.countries[0] == WORLDWIDE else spec.countries[0],
        "description": e.description,
        "company_phone": company_phone.e164 if company_phone else "",
        "company_phone_type": company_phone.phone_type.value if company_phone else "",
        "company_phone_source": "company website" if company_phone else "",
        "company_whatsapp": whatsapp.e164 if whatsapp else "",
        "company_email": next(
            (m for m in b.emails if m.split("@")[0] in ("info", "sales", "hello", "contact", "enquiries")), ""
        ),
        "registry": lead.registry.get("registry_name", ""),
        "registry_id": lead.registry.get("registry_id", ""),
        "registry_match": lead.registry.get("match", ""),
        "registry_entity_type": lead.registry.get("entity_type", ""),
        "registry_status": lead.registry.get("status", ""),
        "incorporated_on": lead.registry.get("incorporated_on", ""),
        "alive_proofs": ", ".join(lead.alive_proofs),
        "outreach_allowed": "yes" if lead.outreach_allowed else "no",
        "outreach_reason": lead.outreach_reason,
        "website_access": b.access_note,
        "why_this_lead": s.why,
        "suggested_outreach_angle": s.angle,
        "risks_or_disqualifiers": s.risks,
        "data_freshness_date": today.isoformat(),
    }
    for i, sig in enumerate(
        sorted(fresh_signals(e, today, spec.freshness_days), key=lambda x: x.effective_date, reverse=True)[
            :3
        ],
        1,
    ):
        row |= {
            f"signal_{i}_type": sig.signal_type,
            f"signal_{i}_detail": sig.claim,
            f"signal_{i}_date": sig.effective_date.isoformat(),
            f"signal_{i}_date_basis": sig.date_basis(),
            f"signal_{i}_source_url": sig.source_url,
        }
    return row


def _person_rows(leads: list[LeadResult], spec: JobSpec, today: date) -> list[dict]:
    rows = []
    for rank, lead in enumerate(leads, 1):
        base = company_row(lead, rank, spec, today)
        for p in lead.verified_people:
            draft = lead.drafts.get((p.first_name, p.last_name))
            rows.append(
                base
                | {
                    "first_name": p.first_name,
                    "last_name": p.last_name,
                    "full_name": f"{p.first_name} {p.last_name}",
                    "job_title": p.job_title,
                    "buyer_role": p.buyer_role.value,
                    "work_email": p.email,
                    "email_status": p.email_verdict.value,
                    "email_source": p.email_source.value if p.email_source else "",
                    "person_source_url": p.source_url,
                    "email_subject": draft.subject if draft else "",
                    "email_body": draft.body if draft else "",
                }
            )
    return rows


def build_rows(
    leads: list[LeadResult], spec: JobSpec, today: date
) -> tuple[list[dict], list[dict], list[dict]]:
    """(Leads, ICP fit - no strong signal, Good leads - no contact) rows."""
    leads = [lead for lead in leads if not lead.watchlist]  # on their own sheet (watchlist_rows)
    lead_rows = _person_rows(
        sorted(
            (lead for lead in leads if lead.verified_people and lead.tier == LeadTier.SIGNAL), key=rank_key
        ),
        spec,
        today,
    )
    icp_rows = _person_rows(
        sorted(
            (lead for lead in leads if lead.verified_people and lead.tier == LeadTier.ICP_ONLY), key=rank_key
        ),
        spec,
        today,
    )
    no_contact = sorted((lead for lead in leads if not lead.verified_people), key=rank_key)
    nc_rows = []
    for rank, lead in enumerate(no_contact, 1):
        found = "; ".join(
            f"{p.first_name} {p.last_name} ({p.job_title}) — {p.source_url}" for p in lead.people
        )
        reason = (
            "no decision-maker named on any fetched source"
            if not lead.people
            else "no verified email: "
            + ", ".join(f"{p.first_name} {p.last_name}: {p.email_verdict.value}" for p in lead.people)
        )
        nc_rows.append(
            company_row(lead, rank, spec, today)
            | {"decision_makers_found_without_verified_email": found, "why_no_contact": reason}
        )
    return lead_rows, icp_rows, nc_rows


def signals_only_rows(leads: list[LeadResult], spec: JobSpec, today: date) -> list[dict]:
    """One row per signal lead, ranked as the run ranked them. ICP-only leads never reach a signals-only
    run (the spec forbids the tier), and are filtered here as well."""
    ranked = sorted(
        (lead for lead in leads if lead.tier == LeadTier.SIGNAL and not lead.watchlist), key=rank_key
    )
    return [company_row(lead, rank, spec, today) for rank, lead in enumerate(ranked, 1)]


def watchlist_rows(leads: list[LeadResult], spec: JobSpec, today: date) -> list[dict]:
    """Near misses: a fresh verified sign, but a score just under the bar. Ranked like leads, from 1."""
    ranked = sorted((lead for lead in leads if lead.watchlist), key=rank_key)
    rows = []
    for rank, lead in enumerate(ranked, 1):
        sc = lead.score
        why = f"score {sc.total} below {get_settings().qualify_score}: {sc.why}" + (
            f" Risks: {sc.risks}" if sc.risks else ""
        )
        rows.append(
            company_row(lead, rank, spec, today)
            | {
                "fit_score": sc.fit,
                "intent_score": sc.intent,
                "timing_score": sc.timing,
                "why_not_qualified": why,
            }
        )
    return rows


def write_outputs(
    leads: list[LeadResult], spec: JobSpec, today: date, out_dir: Path, job_id: int
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{spec.client.key}_job{job_id}_{today.isoformat()}"
    paths = {"leads_csv": out_dir / f"{stem}_leads.csv", "xlsx": out_dir / f"{stem}.xlsx"}
    if not spec.wants_contacts:
        sheets = [("Leads", SIGNALS_ONLY_COLUMNS, signals_only_rows(leads, spec, today), paths["leads_csv"])]
    else:
        lead_rows, icp_rows, nc_rows = build_rows(leads, spec, today)
        lead_cols = COMPANY_COLUMNS + PERSON_COLUMNS + (DRAFT_COLUMNS if spec.personalized_email else [])
        icp_cols = COMPANY_COLUMNS + PERSON_COLUMNS  # no drafts: there is no genuine dated hook to write from
        paths["no_contact_csv"] = out_dir / f"{stem}_no_contact.csv"
        sheets = [("Leads", lead_cols, lead_rows, paths["leads_csv"])]
        if spec.include_icp_only:
            paths["icp_only_csv"] = out_dir / f"{stem}_icp_only.csv"
            sheets.append((ICP_SHEET, icp_cols, icp_rows, paths["icp_only_csv"]))
        sheets.append(("Good leads - no contact", NO_CONTACT_COLUMNS, nc_rows, paths["no_contact_csv"]))
    if spec.include_watchlist:
        paths["watchlist_csv"] = out_dir / f"{stem}_watchlist.csv"
        sheets.append(
            (WATCHLIST_SHEET, WATCHLIST_COLUMNS, watchlist_rows(leads, spec, today), paths["watchlist_csv"])
        )
    temporary_paths: dict[str, Path] = {}
    for _, cols, rows, path in sheets:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary_paths[str(path)] = temporary
        with temporary.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(_safe_row(r) for r in rows)
    wb = Workbook()
    for title, cols, rows, _ in sheets:
        ws = wb.active if title == "Leads" else wb.create_sheet(title)
        ws.title = title
        ws.append(cols)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([safe_cell(r.get(c, "")) for c in cols])
        ws.freeze_panes = "A2"
    ws = wb.create_sheet("Methodology")
    for line in _methodology(spec, today):
        ws.append([safe_cell(line)])
    xlsx_temporary = paths["xlsx"].with_suffix(".xlsx.tmp")
    wb.save(xlsx_temporary)
    temporary_paths[str(paths["xlsx"])] = xlsx_temporary
    # Publish only fully-written files.  os.replace is atomic on the output
    # filesystem and keeps an existing deliverable intact until the replacement
    # is ready.
    for final in paths.values():
        os.replace(temporary_paths[str(final)], final)
    return paths


def _methodology(spec: JobSpec, today: date) -> list[str]:
    common = [
        f"Client: {spec.client.name}",
        f"Ask: {spec.ask}",
        f"Countries: {', '.join(spec.countries)}",
        f"Generated: {today.isoformat()}",
        f"Freshness window: signals dated within {spec.freshness_days} days",
        "A signal's date is when the event happened: if the source states an earlier event date than its publication "
        "date (an old event in a new article), the earlier date is used. Republished reports of one event count once.",
        "Every fact and signal was quoted from a page fetched live in this run and checked by code against that page.",
        "Signal dates come only from article metadata, URL dates, search-result dates or job-posting dates; homepages are never dated.",
    ]
    if spec.countries[0] == WORLDWIDE:
        common.append(
            "Worldwide run: no company registry or country rules were checked, and the country column is left "
            "blank because each company's country was not verified."
        )
    if spec.include_watchlist:
        settings = get_settings()
        common.append(
            f"'{WATCHLIST_SHEET}' sheet: companies with a fresh, verified buying sign that scored "
            f"{settings.watchlist_min_score}-{settings.qualify_score - 1} (qualified leads score "
            f"{settings.qualify_score}+) and fit at least {settings.watchlist_min_fit} of 40. They did not qualify: "
            "no contacts were looked up and they are not marked as delivered, so a later run can still deliver "
            "them as leads. why_not_qualified says what fell short."
        )
    if not spec.wants_contacts:
        return [
            *common,
            "Signals only: each row is a company and its dated, sourced buying signal. No people were looked up "
            "and no emails were searched for or stored; find contacts in your own tools (CRM, Apollo, Hunter).",
            "confidence_tier: strong = asked for a supplier, or two different kinds of demand sign; good = one "
            "demand sign; weak = budget only (funding, growth). signal_N_source_url is the page the sign was "
            "quoted from.",
        ]
    return [
        *common,
        "Emails: only addresses confirmed deliverable (valid, not catch-all) are delivered. No guessed emails.",
        "Leads sheet: qualified companies with at least one verified decision-maker email (one row per person).",
        "'Good leads - no contact' sheet: companies that passed every check except a verified decision-maker email.",
        *(
            [
                f"'{ICP_SHEET}' sheet (lead_tier=icp_only): active registered companies that fit the ICP strongly but "
                "showed no current signal strong enough to qualify. Not finding a public signal does not mean there is "
                "no need; it means none was found. No outreach drafts are written for them."
            ]
            if spec.include_icp_only
            else []
        ),
        "registry_match: company_number_on_site = the number printed on the company's own pages; legal_name_on_site = the "
        "registered name printed on its own pages matched exactly one active company; officer_and_name = a quote-verified "
        "decision-maker is a current officer of exactly one active company sharing the brand's name; exact_name = the only active "
        "company with exactly the brand name.",
        "outreach_allowed: UK = active company/LLP on Companies House (PECR corporate subscriber); other rules per country.",
    ]


def job_files(out_dir: Path, client_key: str, job_id: int) -> list[Path]:
    """Exports a job wrote, found by the stem `write_outputs` uses (client key + job id)."""
    if not out_dir.is_dir():
        return []
    return sorted(
        p for p in out_dir.glob(f"{client_key}_job{job_id}_*") if p.is_file() and p.suffix != ".tmp"
    )
