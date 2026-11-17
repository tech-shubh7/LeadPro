"""RFPs and tender briefs are mostly PDFs: they must be readable, quotable and dated, or the strongest
buying signal there is ("we invite proposals") can never qualify a lead."""

import asyncio
from datetime import date

import httpx

from app.models.enums import DateSource
from app.providers.web.page_fetcher import Fetcher, PageCache, pdf_text_and_date
from app.services.discovery_service import _domain_names
from app.services.extraction_service import _signal_date
from app.services.research_service import ResearchBundle
from app.utils.evidence_verifier import quote_in_text

BRIEF = (
    "Brightwave Robotics invites proposals for a new customer portal. Submission deadline 12 December 2026."
)


def make_pdf(text: str, created: str | None = "D:20261102090000Z") -> bytes:
    """A minimal valid one-page PDF with real extractable text and an optional CreationDate."""
    stream = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET".encode()
    info = f"<< /Title (Brief) /CreationDate ({created}) >>" if created else "<< /Title (Brief) >>"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        info.encode(),
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def _fetch(tmp_path, body: bytes, ctype="application/pdf", url="https://brightwave.co.uk/docs/brief.pdf"):
    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": ctype})

    fetcher = Fetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), cache=PageCache(tmp_path, None)
    )
    fetcher._robots["https://brightwave.co.uk"] = None

    async def go():
        try:
            return await fetcher.fetch(url)
        finally:
            await fetcher.aclose()

    return asyncio.run(go())


def test_a_pdf_brief_is_read_quoted_and_dated(tmp_path):
    page = _fetch(tmp_path, make_pdf(BRIEF))
    assert page.ok
    assert page.is_document
    assert page.doc_date == "2026-11-02"
    assert quote_in_text(
        "invites proposals for a new customer portal", page.text
    )  # verifiable like a web page

    bundle = ResearchBundle("brightwave.co.uk", None, pages={page.final_url: page})
    dated = _signal_date(page.final_url, bundle, date(2026, 11, 15))
    assert dated.value == date(2026, 11, 2)
    assert dated.source == DateSource.DOCUMENT_META


def test_a_pdf_served_as_octet_stream_is_still_read(tmp_path):
    page = _fetch(tmp_path, make_pdf(BRIEF), ctype="application/octet-stream")
    assert page.is_document
    assert "customer portal" in page.text


def test_an_undated_or_future_dated_pdf_gives_no_date(tmp_path):
    """Fails closed: no date means the signal cannot count as fresh."""
    assert pdf_text_and_date(make_pdf(BRIEF, created=None))[1] is None
    assert pdf_text_and_date(make_pdf(BRIEF, created="D:20991231000000Z"))[1] is None


def test_a_broken_pdf_is_unreadable_not_an_error(tmp_path):
    page = _fetch(tmp_path, b"%PDF-1.4 this is not really a pdf")
    assert not page.ok
    assert page.text == ""
    assert page.blocked_reason == "document has no readable text"


def test_an_oversized_pdf_is_not_downloaded(tmp_path, monkeypatch):
    from app.providers.web import page_fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "MAX_PDF_BYTES", 100)
    page = _fetch(tmp_path, make_pdf(BRIEF))
    assert page.blocked_reason == "page too large"


def test_a_company_named_only_by_its_own_domain_is_kept():
    """An RFP PDF on the buyer's site often never repeats the name in the search snippet."""
    assert _domain_names("https://brightwave.co.uk/docs/brief.pdf", "Brightwave Robotics")
    assert not _domain_names("https://www.glassdoor.co.uk/brightwave", "Brightwave Robotics")  # third party
    assert not _domain_names("https://example.org/brief.pdf", "Brightwave Robotics")
