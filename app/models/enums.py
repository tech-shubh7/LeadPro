"""Every status string used in the app is defined here once."""

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RESEARCHING_CLIENT = "researching_client"
    AWAITING_ICP_REVIEW = "awaiting_icp_review"
    DISCOVERING = "discovering"
    RESEARCHING = "researching"
    SCORING = "scoring"
    FINDING_CONTACTS = "finding_contacts"
    REVALIDATING = "revalidating"
    DONE = "done"
    DONE_WITH_SHORTFALL = "done_with_shortfall"
    FAILED = "failed"


class LeadStatus(StrEnum):
    CANDIDATE = "candidate"
    QUALIFIED = "qualified"
    QUALIFIED_NO_CONTACT = "qualified_no_contact"  # passes every check except a verified decision-maker email
    REJECTED = "rejected"
    DROPPED_ON_REVALIDATION = "dropped_on_revalidation"


class LeadTier(StrEnum):
    SIGNAL = "signal"  # fits the ICP and has a verified signal dated inside the freshness window
    ICP_ONLY = "icp_only"  # fits the ICP (opt-in tier); no current public signal was found


class EvidenceKind(StrEnum):
    FIRMOGRAPHIC = "firmographic"
    SIGNAL = "signal"
    PERSON = "person"
    WARM_PATH = "warm_path"


class DateSource(StrEnum):
    SEARCH_RESULT = "search_result"
    JOB_POSTING = "job_posting"
    ARTICLE_META = "article_meta"
    JSON_LD = "json_ld"
    URL_PATH = "url_path"
    HTML_MARKUP = "html_markup"  # htmldate, markup only (time/meta tags), article pages only
    OFFICIAL_NOTICE = (
        "official_notice"  # publication date in a government open-data record (e.g. Contracts Finder)
    )
    DOCUMENT_META = "document_meta"  # creation date embedded in a published document (PDF RFP / tender brief)


class DatePrecision(StrEnum):
    EXACT = "exact"
    RELATIVE = "relative"  # derived from "N days ago"


class EmailVerdict(StrEnum):
    """Our normalized verdict; provider-specific statuses map onto this."""

    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"
    NOT_CHECKED = "not_checked"


class EmailSource(StrEnum):
    COMPANY_PAGE = "company_page"
    FINDER = "finder"


class BuyerRole(StrEnum):
    ECONOMIC_BUYER = "economic_buyer"
    FUNCTIONAL_OWNER = "functional_owner"


class PhoneType(StrEnum):
    MOBILE = "mobile"
    LANDLINE = "landline"
    OTHER = "other"


class PhoneSource(StrEnum):
    COMPANY_SITE = "company_site"
    MAPS_LISTING = "maps_listing"
    WHATSAPP_LINK = "whatsapp_link"
    PAID_LOOKUP = "paid_lookup"


class AliveProof(StrEnum):
    WEBSITE_LIVE = "website_live"
    WEBSITE_EXISTS = "website_exists"  # responds but refuses automated access
    MAPS_OPERATIONAL = "maps_operational"
    REGISTRY_ACTIVE = "registry_active"
    RECENT_DATED_SIGNAL = "recent_dated_signal"
