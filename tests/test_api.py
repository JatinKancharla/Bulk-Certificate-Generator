"""API-level tests covering the required scenarios."""
import io
import zipfile

from app import service

JOBS = "/api/v1/jobs"


# ------------------------------------------------------------ creating a job
def test_create_job_returns_202_and_processes_all(client, payload):
    r = client.post(JOBS, json=payload)
    assert r.status_code == 202
    body = r.json()
    assert body["total"] == 3
    assert body["job_id"]
    assert body["status_url"] == f"{JOBS}/{body['job_id']}"

    status = client.get(body["status_url"]).json()
    assert status["status"] == "COMPLETED"
    assert (status["succeeded"], status["failed"], status["pending"]) == (3, 0, 0)


# ------------------------------------------------------------ validation
def test_request_level_validation(client, payload):
    assert client.post(JOBS, json={**payload, "recipients": []}).status_code == 422
    assert client.post(JOBS, json={**payload, "event_name": ""}).status_code == 422
    assert client.post(JOBS, json={"recipients": payload["recipients"]}).status_code == 422  # no event_name
    assert client.post(JOBS, json={**payload, "issue_date": "not-a-date"}).status_code == 422


def test_too_many_recipients_rejected(client, payload, monkeypatch):
    monkeypatch.setattr("app.main.settings.max_recipients", 2)
    r = client.post(JOBS, json=payload)  # 3 recipients > limit of 2
    assert r.status_code == 422
    assert "Too many recipients" in r.json()["detail"]


def test_invalid_recipients_fail_individually_not_the_whole_job(client, payload):
    payload["recipients"] += [
        {"name": "", "email": "noname@example.com"},                 # empty name
        {"name": "Bad Email", "email": "not-an-email"},              # bad email
        {"email": "missing-name@example.com"},                       # missing name
        {"name": "Asha Again", "email": "ASHA@example.com"},         # duplicate (case-insensitive)
        {"name": "हिंदी नाम", "email": "hindi@example.com"},          # unsupported characters
    ]
    body = client.post(JOBS, json=payload).json()
    assert body["total"] == 8

    status = client.get(f"{JOBS}/{body['job_id']}").json()
    assert status["status"] == "COMPLETED_WITH_ERRORS"
    assert status["succeeded"] == 3 and status["failed"] == 5
    errors = {f["position"]: f["error"] for f in status["failures"]}
    assert set(errors) == {4, 5, 6, 7, 8}
    assert "name" in errors[4]
    assert "email" in errors[5]
    assert "name" in errors[6]
    assert "duplicate" in errors[7]
    assert "Latin-1" in errors[8]


def test_job_with_only_invalid_recipients_is_failed_immediately(client, payload):
    payload["recipients"] = [{"name": "X", "email": "nope"}]
    body = client.post(JOBS, json=payload).json()
    assert body["status"] == "FAILED"
    assert body["failed"] == 1 and body["succeeded"] == 0
    assert body["download_all_url"] is None


# ------------------------------------------------------------ certificate generation
def test_generated_files_exist_and_are_pdfs(client, payload):
    job_id = client.post(JOBS, json=payload).json()["job_id"]
    items = client.get(f"{JOBS}/{job_id}/certificates").json()["items"]
    assert len(items) == 3
    for item in items:
        assert item["status"] == "SUCCESS"
        assert item["generated_at"] is not None
        pdf = client.get(item["download_url"])
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")


# ------------------------------------------------------------ status / progress
def test_status_shows_pending_before_processing_starts(client, payload, monkeypatch):
    monkeypatch.setattr(service, "process_job", lambda job_id: None)  # worker never runs
    job_id = client.post(JOBS, json=payload).json()["job_id"]

    status = client.get(f"{JOBS}/{job_id}").json()
    assert status["status"] == "PENDING"
    assert status["pending"] == 3 and status["succeeded"] == 0
    assert status["progress_percent"] == 0.0
    assert status["download_all_url"] is None
    assert client.get(f"{JOBS}/{job_id}/download").status_code == 409  # not finished yet


def test_progress_percent_counts_failures_as_processed(client, payload):
    payload["recipients"].append({"name": "Bad", "email": "bad"})
    job_id = client.post(JOBS, json=payload).json()["job_id"]
    status = client.get(f"{JOBS}/{job_id}").json()
    assert status["progress_percent"] == 100.0
    assert status["total"] == 4


def test_unknown_job_is_404(client):
    assert client.get(f"{JOBS}/does-not-exist").status_code == 404
    assert client.get(f"{JOBS}/does-not-exist/certificates").status_code == 404


# ------------------------------------------------------------ individual generation failure
def test_one_generation_failure_does_not_stop_the_others(client, payload, monkeypatch):
    real = service.generate_certificate_pdf

    def flaky(data, path):
        if data.recipient_name == "Ravi Kumar":
            raise RuntimeError("disk exploded")
        return real(data, path)

    monkeypatch.setattr(service, "generate_certificate_pdf", flaky)

    job_id = client.post(JOBS, json=payload).json()["job_id"]
    status = client.get(f"{JOBS}/{job_id}").json()
    assert status["status"] == "COMPLETED_WITH_ERRORS"
    assert status["succeeded"] == 2 and status["failed"] == 1
    assert status["failures"][0]["recipient_name"] == "Ravi Kumar"
    assert "disk exploded" in status["failures"][0]["error"]

    failed = client.get(f"{JOBS}/{job_id}/certificates", params={"status": "FAILED"}).json()
    assert failed["total"] == 1
    assert failed["items"][0]["download_url"] is None
    # the failed certificate cannot be downloaded
    cert_id = failed["items"][0]["certificate_id"]
    assert client.get(f"/api/v1/certificates/{cert_id}/download").status_code == 409


def test_all_generations_failing_marks_job_failed(client, payload, monkeypatch):
    def always_fail(data, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "generate_certificate_pdf", always_fail)
    job_id = client.post(JOBS, json=payload).json()["job_id"]
    assert client.get(f"{JOBS}/{job_id}").json()["status"] == "FAILED"


# ------------------------------------------------------------ retrieving certificates
def test_list_filter_and_pagination(client, payload):
    payload["recipients"].append({"name": "Bad", "email": "bad"})
    job_id = client.post(JOBS, json=payload).json()["job_id"]

    ok = client.get(f"{JOBS}/{job_id}/certificates", params={"status": "SUCCESS"}).json()
    assert ok["total"] == 3
    page = client.get(f"{JOBS}/{job_id}/certificates", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert [i["position"] for i in page["items"]] == [3, 4]
    assert client.get(f"{JOBS}/{job_id}/certificates", params={"limit": 0}).status_code == 422


def test_download_single_certificate_and_unknown_id(client, payload):
    job_id = client.post(JOBS, json=payload).json()["job_id"]
    cert = client.get(f"{JOBS}/{job_id}/certificates").json()["items"][0]
    meta = client.get(f"/api/v1/certificates/{cert['certificate_id']}").json()
    assert meta["recipient_name"] == "Asha Rao"
    assert client.get("/api/v1/certificates/nope").status_code == 404
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_download_zip_contains_only_successful_certificates(client, payload):
    payload["recipients"].append({"name": "Bad", "email": "bad"})
    job_id = client.post(JOBS, json=payload).json()["job_id"]

    r = client.get(f"{JOBS}/{job_id}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert names == ["0001_Asha_Rao.pdf", "0002_Ravi_Kumar.pdf", "0003_Meera_Iyer.pdf"]
        assert all(zf.read(n).startswith(b"%PDF") for n in names)
