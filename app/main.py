"""HTTP API."""
import os
import re
import tempfile
import zipfile
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import service
from app.config import settings
from app.database import Base, engine, get_db
from app.models import FINISHED_STATUSES, Certificate, CertificateStatus, Job
from app.schemas import CertificateList, CertificateOut, FailureOut, JobCreate, JobOut

API = "/api/v1"
MAX_FAILURES_IN_STATUS = 100


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)  # fine for this scope; use Alembic migrations in production
    yield


app = FastAPI(title="Bulk Certificate Generator", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- helpers
def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_out(db: Session, job: Job, include_failures: bool = False) -> JobOut:
    counts = service.get_counts(db, job.id)
    processed = counts["succeeded"] + counts["failed"]
    finished = job.status in FINISHED_STATUSES
    out = JobOut(
        job_id=job.id,
        status=job.status.value,
        event_name=job.event_name,
        total=job.total,
        progress_percent=round(100 * processed / job.total, 1) if job.total else 100.0,
        created_at=job.created_at,
        completed_at=job.completed_at,
        status_url=f"{API}/jobs/{job.id}",
        certificates_url=f"{API}/jobs/{job.id}/certificates",
        download_all_url=f"{API}/jobs/{job.id}/download" if finished and counts["succeeded"] else None,
        **counts,
    )
    if include_failures:
        rows = db.scalars(
            select(Certificate)
            .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED)
            .order_by(Certificate.position)
            .limit(MAX_FAILURES_IN_STATUS)
        ).all()
        out.failures = [
            FailureOut(
                position=c.position,
                recipient_name=c.recipient_name,
                recipient_email=c.recipient_email,
                error=c.error,
            )
            for c in rows
        ]
        out.failures_truncated = counts["failed"] > MAX_FAILURES_IN_STATUS
    return out


def _cert_out(c: Certificate) -> CertificateOut:
    return CertificateOut(
        certificate_id=c.id,
        position=c.position,
        recipient_name=c.recipient_name,
        recipient_email=c.recipient_email,
        status=c.status.value,
        error=c.error,
        generated_at=c.generated_at,
        download_url=f"{API}/certificates/{c.id}/download" if c.status == CertificateStatus.SUCCESS else None,
    )


def _slug(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "certificate").strip("_")[:50] or "certificate"


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post(f"{API}/jobs", response_model=JobOut, status_code=202)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Submit a bulk request. Returns 202 immediately; generation continues in the background."""
    if len(payload.recipients) > settings.max_recipients:
        raise HTTPException(
            status_code=422,
            detail=f"Too many recipients ({len(payload.recipients)}). Maximum per request is {settings.max_recipients}.",
        )
    job = service.create_job(db, payload)
    if job.status not in FINISHED_STATUSES:  # nothing to do if every recipient was invalid
        background_tasks.add_task(service.process_job, job.id)
    return _job_out(db, job, include_failures=True)


@app.get(f"{API}/jobs/{{job_id}}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Progress/result of a job, including the first failures with reasons."""
    return _job_out(db, _get_job_or_404(db, job_id), include_failures=True)


@app.get(f"{API}/jobs/{{job_id}}/certificates", response_model=CertificateList)
def list_certificates(
    job_id: str,
    status: CertificateStatus | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated per-recipient results. Filter with ?status=SUCCESS|FAILED|PENDING."""
    _get_job_or_404(db, job_id)
    base = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        base = base.where(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.scalars(base.order_by(Certificate.position).limit(limit).offset(offset)).all()
    return CertificateList(
        job_id=job_id, total=total, limit=limit, offset=offset, items=[_cert_out(c) for c in rows]
    )


@app.get(f"{API}/certificates/{{certificate_id}}", response_model=CertificateOut)
def get_certificate(certificate_id: str, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return _cert_out(cert)


@app.get(f"{API}/certificates/{{certificate_id}}/download")
def download_certificate(certificate_id: str, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if cert.status != CertificateStatus.SUCCESS or not cert.file_path:
        raise HTTPException(status_code=409, detail=f"Certificate is not available (status: {cert.status.value})")
    path = settings.storage_dir / cert.file_path
    if not path.exists():
        raise HTTPException(status_code=410, detail="Certificate file is missing from storage")
    return FileResponse(path, media_type="application/pdf", filename=f"{_slug(cert.recipient_name)}.pdf")


@app.get(f"{API}/jobs/{{job_id}}/download")
def download_job_zip(job_id: str, db: Session = Depends(get_db)):
    """ZIP of all successfully generated certificates. Only available once the job has finished."""
    job = _get_job_or_404(db, job_id)
    if job.status not in FINISHED_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job is not finished yet (status: {job.status.value})")
    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.SUCCESS)
        .order_by(Certificate.position)
    ).all()
    if not certs:
        raise HTTPException(status_code=404, detail="No certificates were generated for this job")

    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in certs:
            path = settings.storage_dir / c.file_path
            if path.exists():
                zf.write(path, arcname=f"{c.position:04d}_{_slug(c.recipient_name)}.pdf")
    return FileResponse(
        tmp_name,
        media_type="application/zip",
        filename=f"certificates_{job_id}.zip",
        background=BackgroundTask(os.remove, tmp_name),  # clean up the temp file after sending
    )
