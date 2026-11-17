"""A job ad from a job board, with the employer named: the shape every job-ad provider returns."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class JobAd:
    job_id: str
    employer_id: str  # the board's own id or slug for the employer ("" if it gives none)
    employer_name: str
    title: str
    location: str
    posted_on: date | None
    description: str  # plain text; may be empty until fetched (LinkedIn lists ads without it)
    url: str
    board: str = "Reed"  # shown with the evidence: "Posted 2026-11-14 on LinkedIn"
