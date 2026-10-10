# Bulk Certificate Generator

A backend API that takes a list of recipients in **a single request**, generates a PDF certificate for each one from a predefined template, tracks the progress of the job, and lets the client retrieve the results.

- **Stack:** Python 3.10+, FastAPI, SQLAlchemy 2, SQLite (default), ReportLab (PDF), pytest
- **Tested on:** Python 3.12 and 3.13

## Features

- Bulk generation: one `POST` for any number of recipients (default cap: 5,000 per request)
- Non-blocking: the API responds immediately and generates certificates in the background
- Per-recipient validation: one bad row never rejects the rest of the batch
- Failure isolation: if one certificate fails to generate, the others still complete
- Progress tracking: counts, percentage, and the reason for every failure
- Retrieval: one PDF, a paginated list, or a ZIP of the whole job

## Project structure

```
app/
  main.py          HTTP routes (FastAPI)
  schemas.py       Request validation and response shapes (Pydantic)
  service.py       Job creation, per-recipient validation, background processing
  certificate.py   PDF template (ReportLab)
  models.py        Database tables: Job and Certificate
  database.py      SQLAlchemy engine and session
  config.py        Settings from environment variables
tests/             pytest suite (16 tests)
sample_request.json
```

## 1. Setup

Requires Python 3.10 or newer.

```bash
git clone https://github.com/JatinKancharla/Bulk-Certificate-Generator.git
cd Bulk-Certificate-Generator

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
```

No database setup is needed. SQLite is used by default and the tables are created on startup.

Optional environment variables (all have defaults):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | Any SQLAlchemy database URL |
| `CERT_STORAGE_DIR` | `./storage` | Where generated PDFs are saved |
| `MAX_RECIPIENTS` | `5000` | Maximum recipients accepted per request |

## 2. Run the application

```bash
python -m uvicorn app.main:app --reload
```

- The server runs at http://127.0.0.1:8000
- Interactive API docs (Swagger UI): http://127.0.0.1:8000/docs
- To use another port: add `--port 8001`

## 3. Run the tests

```bash
python -m pytest
```

Expected result: `16 passed`. The tests use a temporary SQLite database and storage folder, so they never touch real data.

| Required area | Where it is tested |
|---|---|
| Creating a generation job | `test_create_job_returns_202_and_processes_all` |
| Input validation | `test_request_level_validation`, `test_too_many_recipients_rejected`, `test_invalid_recipients_fail_individually_not_the_whole_job`, `test_job_with_only_invalid_recipients_is_failed_immediately` |
| Certificate generation | `test_generated_files_exist_and_are_pdfs`, `test_generates_valid_pdf_with_recipient_data` |
| Job status / progress | `test_status_shows_pending_before_processing_starts`, `test_progress_percent_counts_failures_as_processed`, `test_unknown_job_is_404` |
| Individual certificate failure | `test_one_generation_failure_does_not_stop_the_others`, `test_all_generations_failing_marks_job_failed` |
| Retrieving certificates | `test_list_filter_and_pagination`, `test_download_single_certificate_and_unknown_id`, `test_download_zip_contains_only_successful_certificates` |

## 4. Submit a certificate generation request

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
    {"name": "Ravi Kumar", "email": "ravi@example.com", "achievement": "with Distinction"},
    {"name": "Bad Email Person", "email": "not-an-email"}
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `event_name` | yes | Printed on the certificate |
| `issued_by` | no | Default: `Event Organizer` |
| `issue_date` | no | `YYYY-MM-DD`, default: today |
| `recipients[].name` | yes | 1-100 characters, Latin-1 characters only |
| `recipients[].email` | yes | Must be a valid email; duplicates in one request are rejected |
| `recipients[].achievement` | no | Extra line, e.g. "with Distinction" |

The response is `202 Accepted` with a `job_id`. Generation continues in the background.

### Check progress

```bash
curl http://127.0.0.1:8000/api/v1/jobs/<job_id>
```

```json
{
  "job_id": "d018845f-...",
  "status": "COMPLETED_WITH_ERRORS",
  "total": 3, "succeeded": 2, "failed": 1, "pending": 0,
  "progress_percent": 100.0,
  "download_all_url": "/api/v1/jobs/d018845f-.../download",
  "failures": [
    {"position": 3, "recipient_name": "Bad Email Person",
     "recipient_email": "not-an-email", "error": "email: value is not a valid email address ..."}
  ],
  "failures_truncated": false
}
```

| Job status | Meaning |
|---|---|
| `PENDING` | Accepted, not started |
| `PROCESSING` | Certificates are being generated |
| `COMPLETED` | Every certificate succeeded |
| `COMPLETED_WITH_ERRORS` | Some succeeded, some failed |
| `FAILED` | Nothing succeeded |

Poll this endpoint until the status is `COMPLETED`, `COMPLETED_WITH_ERRORS` or `FAILED`.

## 5. Retrieve generated certificates

A **job id** identifies the whole batch. Every recipient's certificate also has its **own certificate id**.

```bash
# Per-recipient results (paginated; filter with ?status=SUCCESS|FAILED|PENDING)
curl "http://127.0.0.1:8000/api/v1/jobs/<job_id>/certificates?status=SUCCESS&limit=50&offset=0"

# One certificate as a PDF (use a certificate_id from the list above)
curl -o certificate.pdf http://127.0.0.1:8000/api/v1/certificates/<certificate_id>/download

# All successful certificates of a finished job, as a ZIP (use the job_id)
curl -o certificates.zip http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Create a bulk job (returns 202) |
| GET | `/api/v1/jobs/{job_id}` | Status, counts, progress, first 100 failures |
| GET | `/api/v1/jobs/{job_id}/certificates` | Paginated per-recipient results |
| GET | `/api/v1/jobs/{job_id}/download` | ZIP of successful PDFs (409 until the job finishes) |
| GET | `/api/v1/certificates/{certificate_id}` | Metadata of one certificate |
| GET | `/api/v1/certificates/{certificate_id}/download` | One PDF (409 if it failed) |
| GET | `/health` | Liveness check |

## 6. Implementation and design decisions

**Background processing.** `POST /jobs` validates and saves everything, returns `202` immediately, and a background task generates the PDFs. Doing it synchronously would keep the HTTP connection open for the whole batch and time out on large requests. I used FastAPI's `BackgroundTasks` because it needs no extra infrastructure (no broker or worker process) while still giving clients a "submit, then poll" model. On my machine a 1,000-recipient request returned in about 0.2 s and finished generating in about 3 s. The trade-off is that the work runs inside the web process, so a restart mid-job would leave it unfinished (see Limitations).

**Two levels of validation.**
1. Request level: malformed body, empty recipient list, missing `event_name`, bad date, or more than `MAX_RECIPIENTS` returns `422` and nothing is stored.
2. Recipient level: a bad or missing email, empty or too-long name, unsupported characters, or a duplicate email in the same request marks only that recipient as `FAILED` with the reason. The rest of the batch continues.

**Failure isolation.** Each certificate is generated inside its own `try/except` and committed on its own. A failure is recorded on that row (`FAILED` plus an error message) and the loop moves on. Committing per certificate also makes progress visible while the job is running. The final job status is derived from the counts.

**Progress comes from the data.** Counts are calculated from the certificate rows (`GROUP BY status`) instead of being stored as counters on the job, so they cannot drift out of sync.

**Safe file writes.** Each PDF is written to a temporary file and then renamed, so a crash cannot leave a half-written file that looks complete. Files are stored as `storage/<job_id>/<certificate_id>.pdf`, and the database keeps the path relative to the storage folder.

**Idempotent worker.** `process_job` only touches `PENDING` rows, so running it again on a half-finished job is safe.

**Template.** One landscape A4 template drawn with ReportLab, with no external assets. Long names and event titles shrink to fit. It uses the built-in Helvetica font, which only covers Latin-1, so names in other scripts (for example Devanagari) are rejected at validation time with a clear message instead of rendering broken characters. To support them, register a Unicode TTF font in `app/certificate.py` and remove the Latin-1 check in `app/schemas.py`.

**Database.** SQLAlchemy 2 models with a one-to-many relation (`jobs` to `certificates`) and indexes on `(job_id, status)` and `(job_id, position)`. SQLite runs in WAL mode so the status endpoint can read while the worker writes. The database URL is configurable through `DATABASE_URL`; I have only tested on SQLite.

## Limitations and next steps

- **Jobs are not resumed after a crash.** A job left in `PENDING` or `PROCESSING` stays that way. Because the worker is idempotent, a startup hook or a real queue (Celery or RQ with Redis) could re-enqueue it.
- **No authentication, rate limiting or idempotency key.** Submitting the same request twice creates two jobs.
- **PDFs are stored on local disk.** In production I would use object storage (such as S3) with signed URLs.
- **Tables are created with `create_all`.** A real deployment should use Alembic migrations.
- **Not tested on Postgres or MySQL.** Only SQLite has been run.