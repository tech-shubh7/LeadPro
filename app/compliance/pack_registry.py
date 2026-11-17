from app.compliance.country_pack import CountryPack, OutreachDecision, RegistryRecord
from app.compliance.uk_pack import UKPack
from app.compliance.us_pack import USPack
from app.models.enums import AliveProof

_PACKS: dict[str, CountryPack] = {p.code: p for p in (UKPack(), USPack())}


class ResearchOnlyPack(CountryPack):
    """Any country without a pack: research is allowed, outreach is always blocked."""

    phone_region = "ZZ"
    timezone = "UTC"

    def __init__(self, code: str):
        self.code = code
        self.name = code

    def is_alive(self, proofs: set[AliveProof]) -> bool:
        return AliveProof.WEBSITE_LIVE in proofs and AliveProof.RECENT_DATED_SIGNAL in proofs

    def outreach_decision(self, record: RegistryRecord | None) -> OutreachDecision:
        return OutreachDecision(
            False, f"{self.code}: no Country Pack; cold-email rules not verified for this country"
        )


def get_pack(country_code: str) -> CountryPack:
    code = country_code.upper()
    return _PACKS.get(code) or ResearchOnlyPack(code)


__all__ = ["CountryPack", "OutreachDecision", "RegistryRecord", "ResearchOnlyPack", "get_pack"]
