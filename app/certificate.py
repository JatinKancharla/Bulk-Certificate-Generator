"""PDF generation for the single predefined certificate template."""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B8892B")
GREY = HexColor("#6B7280")


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    event_name: str
    issued_by: str
    issue_date: date
    achievement: str | None = None


def _fit_font_size(text: str, font: str, max_size: float, max_width: float, min_size: float = 14) -> float:
    """Shrink the font until the text fits the printable width (long names / event titles)."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _centered(c: canvas.Canvas, y: float, text: str, font: str, size: float, color=NAVY):
    width, _ = c._pagesize
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(width / 2, y, text)


def generate_certificate_pdf(data: CertificateData, output_path: Path) -> None:
    """Render the template to `output_path`.

    Written to a temp file then atomically renamed, so a crash mid-render can never
    leave a half-written PDF that looks like a finished certificate.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".pdf.tmp")

    width, height = landscape(A4)
    printable = width - 160
    c = canvas.Canvas(str(tmp_path), pagesize=(width, height))
    c.setTitle(f"Certificate - {data.recipient_name}")
    c.setAuthor(data.issued_by)

    # Double border
    c.setStrokeColor(GOLD)
    c.setLineWidth(4)
    c.rect(25, 25, width - 50, height - 50)
    c.setStrokeColor(NAVY)
    c.setLineWidth(1)
    c.rect(35, 35, width - 70, height - 70)

    _centered(c, height - 120, "CERTIFICATE", "Helvetica-Bold", 44, NAVY)
    _centered(c, height - 150, "OF PARTICIPATION", "Helvetica", 16, GOLD)
    _centered(c, height - 205, "This is to certify that", "Helvetica-Oblique", 16, GREY)

    name_size = _fit_font_size(data.recipient_name, "Helvetica-Bold", 38, printable)
    _centered(c, height - 260, data.recipient_name, "Helvetica-Bold", name_size, NAVY)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(width / 2 - 200, height - 272, width / 2 + 200, height - 272)

    _centered(c, height - 312, "has successfully participated in", "Helvetica-Oblique", 16, GREY)
    event_size = _fit_font_size(data.event_name, "Helvetica-Bold", 26, printable)
    _centered(c, height - 350, data.event_name, "Helvetica-Bold", event_size, NAVY)

    if data.achievement:
        ach_size = _fit_font_size(data.achievement, "Helvetica", 16, printable, min_size=10)
        _centered(c, height - 380, data.achievement, "Helvetica", ach_size, GOLD)

    # Footer: date (left), issuer (right), certificate id (centre)
    c.setStrokeColor(GREY)
    c.setLineWidth(1)
    c.setFillColor(NAVY)
    c.setFont("Helvetica", 12)
    c.drawString(80, 95, data.issue_date.strftime("%d %B %Y"))
    c.line(80, 110, 230, 110)
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    c.drawString(80, 82, "Date of issue")

    c.setFillColor(NAVY)
    c.setFont("Helvetica", 12)
    c.drawRightString(width - 80, 95, data.issued_by)
    c.line(width - 230, 110, width - 80, 110)
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    c.drawRightString(width - 80, 82, "Issued by")

    _centered(c, 55, f"Certificate ID: {data.certificate_id}", "Helvetica", 8, GREY)

    c.showPage()
    c.save()
    os.replace(tmp_path, output_path)
