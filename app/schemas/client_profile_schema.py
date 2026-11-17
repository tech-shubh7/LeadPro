"""What we know about the client (the company the leads are for), as evidence-backed facts.

The planning model proposes these from the client's own website, each with the verbatim words it relied
on; code then keeps only the facts whose words are really on the site. Eligibility rules run on the
result, so a fact the model invented can never unlock a channel the client cannot sell through.
"""

from pydantic import BaseModel, Field


class ClientFactsOut(BaseModel):
    """Model output: facts about the client, each with the exact words from its site that show it."""

    based_in_country: str = Field(
        default="",
        description="ISO 3166-1 alpha-2 code of the country the client operates from (e.g. 'IN', 'GB'). "
        "Empty if its site does not say.",
    )
    based_in_quote: str = Field(
        default="",
        description="Verbatim words from the client's site that show where it is based: an address, "
        "'based in Pune', 'our London office'. Empty if none.",
    )
    public_sector_quote: str = Field(
        default="",
        description="Verbatim words from the client's site showing it has done public-sector or "
        "government work (named councils, departments, frameworks it is on). Empty if none: do not infer.",
    )


class ClientProfile(BaseModel):
    """Verified facts about the client. Unknown is a valid, safe answer."""

    country: str = ""  # ISO2, "" when no verified evidence
    country_evidence: str = ""  # the verified quote or the deterministic hint (TLD, phone prefix)
    public_sector_evidence: str = ""  # verified quote; "" means no proven public-sector track record

    @property
    def has_public_sector_record(self) -> bool:
        return bool(self.public_sector_evidence)
