# Bulk Certificate Generator

A backend API that accepts a list of recipients in **one request**, generates a PDF certificate for each
from a single predefined template, tracks progress, and lets the client retrieve the results.

**Stack:** Python 3.10+, FastAPI, SQLAlchemy 2 (SQLite by default, any SQL database via `DATABASE_URL`), ReportLab (PDF).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

No database setup needed: SQLite is used by default and tables are created on startup.

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@localhost/certs` (install the driver yourself) |
| `CERT_STORAGE_DIR` | `./storage` | Where generated PDFs are written |
| `MAX_RECIPIENTS` | `5000` | Max recipients accepted per request |

## Run

```bash
uvicorn app.main:app --reload
```

Interactive docs (Swagger UI): http://127.0.0.1:8000/docs

## Run the tests

```bash
pytest
```

Tests use a throw-away SQLite database and storage directory, so they never touch real data.

## Usage

### 1. Submit a generation request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

Request body:

```json
{
  "event_name": "Python Bootcamp 2026",
  "issued_by": "Code Academy",
  "issue_date": "2026-10-01",
  "recipients": [
    {"name": "Asha Rao", "email": "asha@example.com"},
    {"name": "Ravi Kumar", "email": "ravi@example.com", "achievement": "with Distinction"}
  ]
}
```

- `event_name` required. `issued_by` (default `Event Organizer`) and `issue_date` (default today) are optional.
- Each recipient needs `name` (1-100 chars) and a valid `email`; `achievement` is optional.

The response is `202 Accepted` with the `job_id` and links. Generation continues in the background.

### 2. Check progress

```bash
curl http://127.0.0.1:8000/api/v1/jobs/<job_id>
```

```json
{
  "job_id": "1a2c11ce-...",
  "status": "COMPLETED_WITH_ERRORS",
  "total": 4, "succeeded": 3, "failed": 1, "pending": 0,
  "progress_percent": 100.0,
  "download_all_url": "/api/v1/jobs/1a2c11ce-.../download",
  "failures": [
    {"position": 4, "recipient_name": "Bad Email Person", "recipient_email": "not-an-email",
     "error": "email: value is not a valid email address: An email address must have an @-sign."}
  ],
  "failures_truncated": false
}
```

Job statuses: `PENDING` -> `PROCESSING` -> `COMPLETED` (all ok) / `COMPLETED_WITH_ERRORS` (mixed) / `FAILED` (none succeeded).
Poll this endpoint until the status is one of the last three.

### 3. Retrieve certificates

```bash
# per-recipient results (paginated, filter with ?status=SUCCESS|FAILED|PENDING)
curl "http://127.0.0.1:8000/api/v1/jobs/<job_id>/certificates?status=SUCCESS&limit=50&offset=0"

# one certificate as PDF
curl -o asha.pdf http://127.0.0.1:8000/api/v1/certificates/<certificate_id>/download

# every successful certificate of a finished job, as a ZIP
curl -o certificates.zip http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

### API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Create a bulk job (202) |
| GET | `/api/v1/jobs/{job_id}` | Status, counts, progress, first 100 failures |
| GET | `/api/v1/jobs/{job_id}/certificates` | Paginated per-recipient results |
| GET | `/api/v1/jobs/{job_id}/download` | ZIP of successful PDFs (409 until the job finishes) |
| GET | `/api/v1/certificates/{id}` | Metadata of one certificate |
| GET | `/api/v1/certificates/{id}/download` | One PDF (409 if it failed / not ready) |
| GET | `/health` | Liveness check |

## Design decisions

**Asynchronous processing with FastAPI `BackgroundTasks`.** `POST /jobs` validates, stores everything, and returns
`202` right away; a background thread generates the PDFs. A synchronous design would hold the HTTP connection open for
the whole batch and time out on large requests. I chose `BackgroundTasks` over Celery/RQ because it needs no extra
infrastructure (no broker/worker) while still giving the client a job + polling model. Measured locally: a
1000-recipient request returns in ~0.2 s and finishes generating in ~3 s.
Trade-off: the work lives in the web process, so it is lost if the server restarts mid-job. See "Limitations".

**Two-level validation.**
1. *Request level* (malformed body, empty list, missing `event_name`, bad date, more than `MAX_RECIPIENTS`) -> `422`, nothing is stored.
2. *Recipient level* (bad/missing email, empty or too-long name, unsupported characters, duplicate email in the same request) ->
   that recipient is stored as `FAILED` with the reason, and the rest of the batch proceeds. One bad row never rejects 4,999 good ones.

**Failure isolation.** Each certificate is generated inside its own `try/except` and committed individually. A failure is
recorded on that row (`status=FAILED`, `error=...`) and the loop continues. Committing per certificate is also what makes
progress visible while the job runs. Final status is derived from the counts.

**Single source of truth for progress.** Counts are computed from the certificate rows (`GROUP BY status`) rather than kept in
counters on the job, so they cannot drift out of sync.

**Safe file writes.** A PDF is rendered to a `.tmp` file and atomically renamed, so a crash cannot leave a half-written file that
looks complete. Files are stored as `storage/<job_id>/<certificate_id>.pdf`; the database stores the path relative to the storage dir.

**Idempotent worker.** `process_job` only processes `PENDING` rows, so re-running it on a partially finished job is safe.

**Template.** One landscape A4 template drawn with ReportLab (no external assets). Long names/event titles shrink to fit.
The template uses the built-in Helvetica font, which covers Latin-1 only, so names with other scripts (e.g. Devanagari) are
rejected at validation time with a clear message instead of rendering broken glyphs. To support them, register a Unicode TTF font in
`app/certificate.py` and drop the Latin-1 check in `app/schemas.py`.

**Database.** SQLAlchemy 2 models; `jobs` 1-to-many `certificates`, with indexes on `(job_id, status)` and `(job_id, position)`.
SQLite runs in WAL mode so the status endpoint can read while the worker writes.

## Limitations / what I would do next

- **Jobs are not resumed after a server crash.** A job left in `PENDING`/`PROCESSING` stays that way. Because the worker is idempotent, a startup hook (or a real queue such as Celery/RQ with Redis) could re-enqueue them.
- **No authentication / rate limiting / idempotency key.** Resubmitting the same request creates a second job.
- **Local-disk storage.** In production, store PDFs in object storage (S3) and return signed URLs.
- **Schema is created with `create_all`.** Use Alembic migrations for real deployments.
- **Single worker thread per job.** Very large batches could be split across a worker pool.
