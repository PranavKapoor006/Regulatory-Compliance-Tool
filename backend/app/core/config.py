from functools import lru_cache
from pathlib import Path
from pydantic import BaseModel
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    app_name: str = os.getenv("APP_NAME", "Regulatory Compliance Tool")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
    fsca_directives_url: str = os.getenv(
        "FSCA_DIRECTIVES_URL",
        "https://www.fsca.co.za/Supervisory-Information/?collapse=collapseEight",
    )
    storage_root: Path = Path(os.getenv("STORAGE_ROOT", str(BASE_DIR / "storage")))
    reference_directives_root: Path = Path(
        os.getenv("REFERENCE_DIRECTIVES_ROOT", str(BASE_DIR / "reference_directives"))
    )
    taxonomy_root: Path = Path(os.getenv("TAXONOMY_ROOT", str(BASE_DIR / "taxonomy")))
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "75"))

    @property
    def uploads_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def downloaded_dir(self) -> Path:
        return self.storage_root / "downloaded_directives"

    @property
    def output_dir(self) -> Path:
        return self.storage_root / "generated_outputs"

    @property
    def breakdown_output_dir(self) -> Path:
        return self.storage_root / "regulatory_text_breakdowns"

    @property
    def obligation_output_dir(self) -> Path:
        return self.storage_root / "obligation_registers"

    @property
    def gap_output_dir(self) -> Path:
        return self.storage_root / "gap_assessments"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for folder in (
        settings.uploads_dir,
        settings.downloaded_dir,
        settings.output_dir,
        settings.breakdown_output_dir,
        settings.obligation_output_dir,
        settings.gap_output_dir,
        settings.reference_directives_root,
        settings.taxonomy_root,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    return settings
