"""Business logic: creating jobs, validating recipients, generating certificates."""
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import database
from app.certificate import CertificateData, generate_certificate_pdf
from app.config import settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import JobCreate, RecipientIn

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "recipient"
        parts.append(f"{field}: {err['msg']}")
    return "; ".join(parts)


def create_job(db: Session, payload: JobCreate) -> Job:
    """Validate every recipient and persist the job.

    Invalid recipients are stored as FAILED rows (with the reason) right away; valid ones are
    stored as PENDING and picked up by `process_job`. One bad row never rejects the request.
    """
    job = Job(
        event_name=payload.event_name,
        issued_by=payload.issued_by,
        issue_date=payload.issue_date,
        total=len(payload.recipients),
        status=JobStatus.PENDING,
    )
    seen_emails: set[str] = set()
    certs: list[Certificate] = []

    for position, raw in enumerate(payload.recipients, start=1):
        cert = Certificate(job=job, position=position, raw_input=raw)
        try:
            recipient = RecipientIn.model_validate(raw)
            cert.recipient_name = recipient.name
            cert.recipient_email = str(recipient.email)
            cert.achievement = recipient.achievement

            email_key = cert.recipient_email.lower()
            if email_key in seen_emails:
                raise ValueError("duplicate email in this request")
            seen_emails.add(email_key)
        except ValidationError as exc:
            cert.status = CertificateStatus.FAILED
            cert.error = _format_validation_error(exc)
            # keep whatever identifying info the client sent so the failure is traceable
            cert.recipient_name = cert.recipient_name or _safe_str(raw.get("name"))
            cert.recipient_email = cert.recipient_email or _safe_str(raw.get("email"))
        except ValueError as exc:  # duplicate
            cert.status = CertificateStatus.FAILED
            cert.error = str(exc)
        certs.append(cert)

    if all(c.status == CertificateStatus.FAILED for c in certs):
        job.status = JobStatus.FAILED
        job.completed_at = _now()

    db.add(job)
    db.add_all(certs)
    db.commit()
    return job


def _safe_str(value) -> str | None:
    return value[:200] if isinstance(value, str) else None


def process_job(job_id: str) -> None:
    """Generate all PENDING certificates of a job. Runs in a background thread.

    - Opens its own DB session (the request's session is closed once the response is sent).
    - Commits after every certificate so progress is visible to the status endpoint.
    - Catches exceptions per certificate: one failure never stops the others.
    - Only touches PENDING rows, so re-running it on a half-done job is safe.
    """
    db = database.SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.error("process_job: job %s not found", job_id)
            return
        job.status = JobStatus.PROCESSING
        db.commit()

        pending_ids = db.scalars(
            select(Certificate.id)
            .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING)
            .order_by(Certificate.position)
        ).all()

        for cert_id in pending_ids:
            cert = db.get(Certificate, cert_id)
            try:
                relative = Path(job_id) / f"{cert.id}.pdf"
                generate_certificate_pdf(
                    CertificateData(
                        certificate_id=cert.id,
                        recipient_name=cert.recipient_name,
                        event_name=job.event_name,
                        issued_by=job.issued_by,
                        issue_date=job.issue_date,
                        achievement=cert.achievement,
                    ),
                    settings.storage_dir / relative,
                )
                cert.status = CertificateStatus.SUCCESS
                cert.file_path = str(relative)
                cert.generated_at = _now()
                cert.error = None
            except Exception as exc:  # noqa: BLE001 - isolate any per-certificate failure
                logger.exception("certificate %s failed", cert_id)
                cert.status = CertificateStatus.FAILED
                cert.error = f"generation failed: {exc}"[:500]
            db.commit()

        _finalise_job(db, job)
    except Exception:  # noqa: BLE001 - something outside the per-certificate loop broke
        logger.exception("job %s crashed", job_id)
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.completed_at = _now()
            db.commit()
    finally:
        db.close()


def _finalise_job(db: Session, job: Job) -> None:
    counts = get_counts(db, job.id)
    if counts["failed"] == 0:
        job.status = JobStatus.COMPLETED
    elif counts["succeeded"] == 0:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS
    job.completed_at = _now()
    db.commit()


def get_counts(db: Session, job_id: str) -> dict[str, int]:
    rows = db.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    by_status = {status: n for status, n in rows}
    return {
        "succeeded": by_status.get(CertificateStatus.SUCCESS, 0),
        "failed": by_status.get(CertificateStatus.FAILED, 0),
        "pending": by_status.get(CertificateStatus.PENDING, 0),
    }
