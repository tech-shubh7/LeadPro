from app.core.evidence_verify import Claim, quote_in_text, verify_claims

PAGE = "Acme Ltd is hiring 3  Warehouse Managers in Leeds — apply by 30 Nov. “We’re expanding fast,” said CEO Jane Roe."


def test_exact_quote_found():
    assert quote_in_text("hiring 3 Warehouse Managers in Leeds", PAGE)


def test_whitespace_quotes_and_dashes_are_normalized():
    assert quote_in_text("Leeds - apply by 30 Nov", PAGE)
    assert quote_in_text('"We\'re expanding fast," said CEO Jane Roe', PAGE)


def test_fabricated_quote_rejected():
    assert not quote_in_text("raised a $10M Series A", PAGE)


def test_too_short_quote_rejected():
    assert not quote_in_text("Acme", PAGE)


def test_claim_from_unfetched_url_rejected():
    claims = [
        Claim("Hiring warehouse managers", "https://acme.co.uk/careers", "hiring 3 Warehouse Managers"),
        Claim("Raised funding", "https://acme.co.uk/news", "raised a Series A round"),
        Claim("Expanding", "https://acme.co.uk/careers", "opening a new office in Paris"),
    ]
    verified, rejected = verify_claims(claims, {"https://acme.co.uk/careers": PAGE})
    assert [c.claim for c in verified] == ["Hiring warehouse managers"]
    assert {r.reason for r in rejected} == {
        "source_url was not fetched in this run",
        "quote not found in fetched page text",
    }
