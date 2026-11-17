"""Step 6: XLSX (Leads / Good leads - no verified contact / Methodology) + matching CSVs."""

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from app.models.enums import LeadTier
from app.schemas.job_spec_schema import JobSpec
from app.services.contact_service import DecisionMaker
from app.services.draft_service import Draft
from app.services.extraction_service import Extraction
from app.services.research_service import ResearchBundle
from app.services.scoring_service import Score, fresh_signals
from app.services.signal_intelligence_service import TIER_ORDER, SignalStack

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
    *(
        f"signal_{i}_{col}"
        for i in (1, 2, 3)
        for col in ("type", "detail", "date", "date_basis", "source_url")
    ),
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
ICP_SHEET = "ICP fit - no strong signal"
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

    @property
    def verified_people(self) -> list[DecisionMaker]:
        return [p for p in self.people if p.email]


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
        "country": spec.countries[0],
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


def write_outputs(
    leads: list[LeadResult], spec: JobSpec, today: date, out_dir: Path, job_id: int
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    lead_rows, icp_rows, nc_rows = build_rows(leads, spec, today)
    lead_cols = COMPANY_COLUMNS + PERSON_COLUMNS + (DRAFT_COLUMNS if spec.personalized_email else [])
    icp_cols = COMPANY_COLUMNS + PERSON_COLUMNS  # no drafts: there is no genuine dated hook to write from
    stem = f"{spec.client.key}_job{job_id}_{today.isoformat()}"
    paths = {
        "leads_csv": out_dir / f"{stem}_leads.csv",
        "no_contact_csv": out_dir / f"{stem}_no_contact.csv",
        "xlsx": out_dir / f"{stem}.xlsx",
    }
    sheets = [("Leads", lead_cols, lead_rows, paths["leads_csv"])]
    if spec.include_icp_only:
        paths["icp_only_csv"] = out_dir / f"{stem}_icp_only.csv"
        sheets.append((ICP_SHEET, icp_cols, icp_rows, paths["icp_only_csv"]))
    sheets.append(("Good leads - no contact", NO_CONTACT_COLUMNS, nc_rows, paths["no_contact_csv"]))
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
    for line in (
        f"Client: {spec.client.name}",
        f"Ask: {spec.ask}",
        f"Countries: {', '.join(spec.countries)}",
        f"Generated: {today.isoformat()}",
        f"Freshness window: signals dated within {spec.freshness_days} days",
        "A signal's date is when the event happened: if the source states an earlier event date than its publication "
        "date (an old event in a new article), the earlier date is used. Republished reports of one event count once.",
        "Every fact and signal was quoted from a page fetched live in this run and checked by code against that page.",
        "Signal dates come only from article metadata, URL dates, search-result dates or job-posting dates; homepages are never dated.",
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
    ):
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


def job_files(out_dir: Path, client_key: str, job_id: int) -> list[Path]:
    """Exports a job wrote, found by the stem `write_outputs` uses (client key + job id)."""
    if not out_dir.is_dir():
        return []
    return sorted(
        p for p in out_dir.glob(f"{client_key}_job{job_id}_*") if p.is_file() and p.suffix != ".tmp"
    )
