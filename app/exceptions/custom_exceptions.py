"""Application exceptions, one hierarchy rooted at `AppError`.

Each carries an HTTP status so the web layer can render any of them without a mapping table; the
pipeline raises and catches them as ordinary domain errors.
"""

from typing import Any


class AppError(Exception):
    """Base for every error this application raises on purpose (as opposed to bugs)."""

    status_code = 500

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class CostCapExceededError(AppError):
    """A paid call would take the job past its spend cap. Stops the job; never retried."""

    status_code = 402


class LLMRefusalError(AppError):
    """The model returned nothing usable (refusal, truncation, schema mismatch) or every model in the
    fallback chain was unavailable."""

    status_code = 502


class CandidateRejectedError(AppError):
    """A candidate company failed a qualification check. Expected, recorded with its reason."""

    status_code = 422

    def __init__(self, reason: str, score: Any = None, evidence: list | None = None):
        super().__init__(reason)
        self.score = score  # the judge's scoring, kept for tuning the plan when present
        # The verified signs seen before rejecting ([{url, quote, ...}]), kept so a near-miss can be reviewed
        # and the source that found it judged, instead of only a one-line reason.
        self.evidence = evidence


class SuppressedError(AppError):
    """The company or address was suppressed (opt-out or erasure) while a run was in flight."""

    status_code = 409


class BadRequestError(AppError):
    """The request cannot be acted on as sent (a stale form, an unrunnable plan)."""

    status_code = 400


class NotFoundError(AppError):
    status_code = 404

    def __init__(self, message: str = "That page does not exist."):
        super().__init__(message)


class TooBusyError(AppError):
    """A concurrency limit is reached (runs or drafts in flight)."""

    status_code = 429
