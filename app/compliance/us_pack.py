from app.compliance.country_pack import CountryPack, OutreachDecision, RegistryRecord
from app.models.enums import AliveProof


class USPack(CountryPack):
    """No national company registry: aliveness rests on web evidence only (stated in the deliverable)."""

    code = "US"
    name = "United States"
    phone_region = "US"
    timezone = "America/New_York"
    registry_name = None

    def is_alive(self, proofs: set[AliveProof]) -> bool:
        return bool(proofs & {AliveProof.WEBSITE_LIVE, AliveProof.WEBSITE_EXISTS}) and bool(
            proofs & {AliveProof.MAPS_OPERATIONAL, AliveProof.RECENT_DATED_SIGNAL}
        )

    def outreach_decision(self, record: RegistryRecord | None) -> OutreachDecision:
        return OutreachDecision(
            True,
            "US: CAN-SPAM permits B2B email; every email needs accurate headers, an opt-out and a postal address",
        )
