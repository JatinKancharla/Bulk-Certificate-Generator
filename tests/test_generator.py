"""Unit tests for the PDF generator on its own (no HTTP, no DB)."""
from datetime import date

from pypdf import PdfReader

from app.certificate import CertificateData, generate_certificate_pdf


def _data(**overrides):
    base = dict(
        certificate_id="abc-123",
        recipient_name="Asha Rao",
        event_name="Python Bootcamp 2026",
        issued_by="Code Academy",
        issue_date=date(2026, 10, 1),
    )
    base.update(overrides)
    return CertificateData(**base)


def test_generates_valid_pdf_with_recipient_data(tmp_path):
    out = tmp_path / "cert.pdf"
    generate_certificate_pdf(_data(achievement="with Distinction"), out)

    assert out.read_bytes().startswith(b"%PDF")
    assert not (tmp_path / "cert.pdf.tmp").exists()  # temp file was renamed away
    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    for expected in ("Asha Rao", "Python Bootcamp 2026", "Code Academy", "with Distinction", "abc-123", "01 October 2026"):
        assert expected in text


def test_very_long_name_still_renders(tmp_path):
    out = tmp_path / "long.pdf"
    generate_certificate_pdf(_data(recipient_name="Wolfeschlegelsteinhausenbergerdorff " * 2), out)
    assert out.read_bytes().startswith(b"%PDF")
