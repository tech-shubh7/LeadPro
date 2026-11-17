"""ORM models, one table per module. Importing the package registers every table on Base.metadata."""

from app.models.base import Base
from app.models.client_profile import ClientProfileRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.delivery import Delivery
from app.models.evidence import Evidence
from app.models.job import Job
from app.models.job_lead import JobLead
from app.models.lead_feedback import LeadFeedback
from app.models.source_document import SourceDocument
from app.models.suppression import Suppression

__all__ = [
    "Base",
    "ClientProfileRecord",
    "Company",
    "Contact",
    "Delivery",
    "Evidence",
    "Job",
    "JobLead",
    "Suppression",
]
