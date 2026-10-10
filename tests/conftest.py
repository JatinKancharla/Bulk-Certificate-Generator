"""Test setup: point the app at a throw-away SQLite DB and storage dir *before* importing it."""
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="certtests_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["CERT_STORAGE_DIR"] = f"{_TMP}/storage"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh tables and empty storage for every test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(settings.storage_dir, ignore_errors=True)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def client():
    # NOTE: TestClient runs FastAPI BackgroundTasks before returning the response to the test,
    # so after `client.post(...)` the job has already been processed.
    return TestClient(app)


@pytest.fixture
def payload():
    return {
        "event_name": "Python Bootcamp 2026",
        "issued_by": "Code Academy",
        "issue_date": "2026-10-01",
        "recipients": [
            {"name": "Asha Rao", "email": "asha@example.com"},
            {"name": "Ravi Kumar", "email": "ravi@example.com", "achievement": "with Distinction"},
            {"name": "Meera Iyer", "email": "meera@example.com"},
        ],
    }
