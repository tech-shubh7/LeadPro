from app.countries.base import CountryPack, OutreachDecision, RegistryRecord
from app.models.enums import AliveProof

# Companies House `company_type` values for corporate bodies. Under PECR, corporate
# subscribers can receive B2B marketing email without prior consent; sole traders and
# English/Welsh partnerships (incl. limited partnerships) are treated as individuals.
# Values from companieshouse/api-enumerations constants.yml.
CORPORATE_TYPES = frozenset({
    "ltd",
    "plc",
    "llp",
    "private-unlimited",
    "private-unlimited-nsc",
    "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption",
    "private-limited-shares-section-30-exemption",
    "old-public-company",
    "scottish-partnership",
    "northern-ireland",
    "european-public-limited-liability-company-se",
    "united-kingdom-societas",
})
ACTIVE_STATUS = "active"


class UKPack(CountryPack):
    code = "GB"
    name = "United Kingdom"
    phone_region = "GB"
    timezone = "Europe/London"
    registry_name = "companies_house"

    def is_alive(self, proofs: set[AliveProof]) -> bool:
        # Name matching to Companies House fails when brand != registered name, so a live site plus a
        # verified recent signal also counts; outreach is still blocked without a registry match.
        if not proofs & {AliveProof.WEBSITE_LIVE, AliveProof.WEBSITE_EXISTS}:
            return False
        return AliveProof.REGISTRY_ACTIVE in proofs or AliveProof.RECENT_DATED_SIGNAL in proofs

    def outreach_decision(self, record: RegistryRecord | None) -> OutreachDecision:
        if record is None:
            return OutreachDecision(False, "UK: no Companies House match; may be a sole trader or partnership (PECR requires consent)")
        if record.status != ACTIVE_STATUS:
            return OutreachDecision(False, f"UK: Companies House status is '{record.status}', not active")
        if record.entity_type not in CORPORATE_TYPES:
            return OutreachDecision(False, f"UK: entity type '{record.entity_type}' is not a corporate subscriber under PECR")
        return OutreachDecision(True, f"UK: active {record.entity_type} per Companies House {record.registry_id}; corporate subscriber under PECR")
