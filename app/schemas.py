"""Pydantic schemas: request validation and response shapes."""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def _latin1_only(value: str | None) -> str | None:
    # The PDF template uses a built-in font (Helvetica) which only supports Latin-1.
    # Reject early with a clear message instead of rendering garbage boxes.
    if value is None:
        return value
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError("contains characters unsupported by the certificate font (Latin-1 only)")
    return value


class RecipientIn(BaseModel):
    """One recipient. Validated individually so one bad row never rejects the whole request."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    achievement: str | None = Field(default=None, max_length=200)  # e.g. "with Distinction"

    _check_name = field_validator("name", "achievement")(_latin1_only)


class JobCreate(BaseModel):
    """Request body. Recipients stay raw dicts here and are validated one by one in the service."""

    event_name: str = Field(min_length=1, max_length=200)
    issued_by: str = Field(default="Event Organizer", min_length=1, max_length=200)
    issue_date: date = Field(default_factory=date.today)
    recipients: list[dict[str, Any]] = Field(min_length=1)

    _check_text = field_validator("event_name", "issued_by")(_latin1_only)


class FailureOut(BaseModel):
    position: int
    recipient_name: str | None
    recipient_email: str | None
    error: str | None


class JobOut(BaseModel):
    job_id: str
    status: str
    event_name: str
    total: int
    succeeded: int
    failed: int
    pending: int
    progress_percent: float
    created_at: datetime
    completed_at: datetime | None
    status_url: str
    certificates_url: str
    download_all_url: str | None  # only set once the job has finished
    failures: list[FailureOut] | None = None  # first N failures (detail endpoint only)
    failures_truncated: bool | None = None


class CertificateOut(BaseModel):
    certificate_id: str
    position: int
    recipient_name: str | None
    recipient_email: str | None
    status: str
    error: str | None
    generated_at: datetime | None
    download_url: str | None


class CertificateList(BaseModel):
    job_id: str
    total: int  # number matching the filter
    limit: int
    offset: int
    items: list[CertificateOut]
