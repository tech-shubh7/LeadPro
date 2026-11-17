import asyncio

import pytest

from app.models.enums import BuyerRole, EmailVerdict
from app.providers.email.email_finder import EmailResult
from app.providers.registry.companies_house_client import Officer
from app.services import contact_service as contacts
from app.services.contact_service import DecisionMaker, attach_emails, rank_decision_makers
from app.services.draft_service import DraftOut, draft_is_grounded
from app.services.extraction_service import Extraction, QuotedPerson

LISTING = {
    "E1": '[E1] (hiring_ops, dated 2026-10-20) Hiring 3 warehouse operatives | quote: "hiring 3 warehouse operatives"'
}


def test_draft_grounding_rejects_invented_numbers_and_ids():
    ok = DraftOut(
        subject="Your 3 new warehouse roles",
        body="Saw you're hiring 3 warehouse operatives.",
        cited_evidence_ids=["E1"],
    )
    invented = DraftOut(subject="Hi", body="Congrats on raising £5m.", cited_evidence_ids=["E1"])
    bad_id = DraftOut(subject="Hi", body="Saw your news.", cited_evidence_ids=["E9"])
    assert draft_is_grounded(ok, LISTING)
    assert not draft_is_grounded(invented, LISTING)
    assert not draft_is_grounded(bad_id, LISTING)


def test_ranking_prefers_buyer_title_then_registry_director():
    ext = Extraction(
        "Acme",
        "desc",
        False,
        people=[
            QuotedPerson(
                first_name="Sam", last_name="Lee", job_title="Marketing Executive", source_url="u", quote="q"
            ),
            QuotedPerson(
                first_name="Jane", last_name="Roe", job_title="Head of Operations", source_url="u", quote="q"
            ),
        ],
    )
    officers = [Officer("Bob", "Smith", "director", None)]
    ranked = rank_decision_makers(ext, officers, "https://ch/officers", ["Head of Operations"], limit=2)
    assert [(p.first_name, p.buyer_role) for p in ranked] == [
        ("Jane", BuyerRole.FUNCTIONAL_OWNER),
        ("Bob", BuyerRole.ECONOMIC_BUYER),
    ]


class FakeFinder:
    name = "fake"

    def __init__(self, find_result, validate_result=None):
        self.find_result, self.validate_result, self.find_calls = find_result, validate_result or [], 0

    async def find(self, first, last, domain):
        self.find_calls += 1
        return self.find_result

    async def validate(self, emails):
        return self.validate_result


@pytest.fixture
def mx_ok(monkeypatch):
    async def yes(domain):
        return True

    monkeypatch.setattr(contacts, "has_mx", yes)


def _dm():
    return DecisionMaker("Jane", "Roe", "Head of Ops", BuyerRole.FUNCTIONAL_OWNER, "u", "q")


def test_printed_email_validated_first_and_finder_skipped(mx_ok):
    finder = FakeFinder(
        EmailResult(None, EmailVerdict.UNKNOWN, "fake", "unknown"),
        [EmailResult("jane.roe@acme.co.uk", EmailVerdict.VALID, "fake", "valid")],
    )
    dm = _dm()
    asyncio.run(attach_emails([dm], ["jane.roe@acme.co.uk", "info@acme.co.uk"], "acme.co.uk", [finder]))
    assert dm.email == "jane.roe@acme.co.uk"
    assert finder.find_calls == 0


def test_catch_all_is_never_delivered(mx_ok):
    finder = FakeFinder(EmailResult("jane@acme.co.uk", EmailVerdict.CATCH_ALL, "fake", "valid"))
    dm = _dm()
    asyncio.run(attach_emails([dm], [], "acme.co.uk", [finder]))
    assert dm.email is None
    assert dm.email_verdict == EmailVerdict.CATCH_ALL


def test_no_mx_means_no_paid_lookup(monkeypatch):
    async def no(domain):
        return False

    monkeypatch.setattr(contacts, "has_mx", no)
    finder = FakeFinder(EmailResult("jane@acme.co.uk", EmailVerdict.VALID, "fake", "valid"))
    asyncio.run(attach_emails([_dm()], [], "acme.co.uk", [finder]))
    assert finder.find_calls == 0


def test_suppressed_email_never_attached_from_finder(mx_ok):
    """An opt-out/erasure request must not be undone by a finder returning the same address again."""
    finder = FakeFinder(EmailResult("jane.roe@acme.co.uk", EmailVerdict.VALID, "fake", "valid"))
    dm = _dm()
    asyncio.run(
        attach_emails([dm], [], "acme.co.uk", [finder], suppressed_emails=frozenset({"jane.roe@acme.co.uk"}))
    )
    assert dm.email is None
    assert dm.finder_status == "suppressed"


def test_suppressed_email_never_attached_from_printed_page(mx_ok):
    # The fake finder would happily confirm this address via EITHER path (validate or find), so this
    # only passes if suppression is actually enforced at both points, not by the finder finding nothing.
    suppressed = "jane.roe@acme.co.uk"
    finder = FakeFinder(
        EmailResult(suppressed, EmailVerdict.VALID, "fake", "valid"),
        [EmailResult(suppressed, EmailVerdict.VALID, "fake", "valid")],
    )
    dm = _dm()
    asyncio.run(
        attach_emails([dm], [suppressed], "acme.co.uk", [finder], suppressed_emails=frozenset({suppressed}))
    )
    assert dm.email is None


def test_suppressed_email_never_attached_from_reuse_cache(mx_ok):
    finder = FakeFinder(EmailResult(None, EmailVerdict.UNKNOWN, "fake", "unknown"))
    dm = _dm()
    asyncio.run(
        attach_emails(
            [dm],
            [],
            "acme.co.uk",
            [finder],
            known={("jane", "roe"): "jane.roe@acme.co.uk"},
            suppressed_emails=frozenset({"jane.roe@acme.co.uk"}),
        )
    )
    assert dm.email is None


def test_same_mailbox_for_two_names_is_one_person(mx_ok):
    """'Chris' from an article and 'Christopher' from the registry resolve to one address: keep the
    higher-ranked one only. (The fake finder returns one address for everybody.)"""
    finder = FakeFinder(EmailResult("c.burgess@acme.co.uk", EmailVerdict.VALID, "fake", "valid"))
    chris = DecisionMaker("Chris", "Burgess", "Group CEO", BuyerRole.ECONOMIC_BUYER, "u", "q")
    christopher = DecisionMaker("Christopher", "Burgess", "Director", BuyerRole.ECONOMIC_BUYER, "u", "q")
    people = [chris, christopher]
    asyncio.run(attach_emails(people, [], "acme.co.uk", [finder]))
    assert [p.first_name for p in people] == ["Chris"]
    assert people[0].email == "c.burgess@acme.co.uk"


def test_distinct_mailboxes_are_both_kept(mx_ok):
    class TwoAddresses(FakeFinder):
        async def find(self, first, last, domain):
            return EmailResult(f"{first.lower()}@acme.co.uk", EmailVerdict.VALID, "fake", "valid")

    a = DecisionMaker("Alan", "Foot", "Director", BuyerRole.ECONOMIC_BUYER, "u", "q")
    b = DecisionMaker("Paul", "Slaney", "Director", BuyerRole.ECONOMIC_BUYER, "u", "q")
    people = [a, b]
    asyncio.run(attach_emails(people, [], "acme.co.uk", [TwoAddresses(None)]))
    assert len(people) == 2
    assert {p.email for p in people} == {"alan@acme.co.uk", "paul@acme.co.uk"}
