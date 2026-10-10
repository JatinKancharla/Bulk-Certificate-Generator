"""Central configuration, read from environment variables with sane defaults."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./certificates.db")
    storage_dir: Path = Path(os.getenv("CERT_STORAGE_DIR", "./storage")).resolve()
    # Hard cap on recipients per request. Protects the server from a single huge payload.
    max_recipients: int = int(os.getenv("MAX_RECIPIENTS", "5000"))


settings = Settings()
