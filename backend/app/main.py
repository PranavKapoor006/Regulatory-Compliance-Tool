import os
import shutil
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import crawler, gap, obligations
from app.services.benchmark_service import BENCHMARK_VERSION
from app.services.crawler_service import CRAWLER_VERSION, crawler_service
from app.services.gap_service import pipeline_metadata
from app.services.obligation_service import OBLIGATION_PIPELINE_VERSION

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "Regulatory Compliance Tool for offline FSCA directive discovery, "
        "PDF/OCR obligation extraction, and internal policy gap review."
    ),
)

allowed_origins = [
    settings.frontend_origin,
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(allowed_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(crawler.router)
app.include_router(obligations.router)
app.include_router(gap.router)


@app.get("/")
def root():
    return {
        "status": "ok",
        "app": settings.app_name,
        "message": "Regulatory Compliance Tool backend is running.",
        "gap_pipeline": pipeline_metadata(),
    }


@app.get("/api/health")
def health():
    crawler_metadata = crawler_service.metadata()
    return {
        "status": "ok",
        "app": settings.app_name,
        "gap_pipeline": pipeline_metadata(),
        "obligation_extraction": {
            "pipeline_version": OBLIGATION_PIPELINE_VERSION,
            "input_mode": "direct-upload-or-bundled-library",
            "accuracy_enabled": True,
        },
        "benchmark": {
            "version": BENCHMARK_VERSION,
            "method": "controlled South African Directive 159 known-answer comparison",
            "runner": "benchmark/run_benchmark.py",
        },
        "crawler": {
            "enabled": True,
            "status": "offline-ready",
            "network_access": False,
            "version": CRAWLER_VERSION,
            "source_url": "",
            "cache_status": crawler_metadata["cache_status"],
            "category_status": crawler_metadata["category_status"],
            "safety": crawler_metadata["safety"],
            "message": (
                "All 50 demo-ready official PDFs are bundled and checksummed. Topic selection "
                "is instantaneous and sends zero FSCA requests."
            ),
        },
        "utilities": [
            "Offline Directive Library",
            "Obligation Extraction",
            "Policy Gap Reviewer",
        ],
    }


@app.get("/api/config")
def app_config():
    return {
        "app_name": settings.app_name,
        "frontend_origin": settings.frontend_origin,
        "fsca_directives_url": settings.fsca_directives_url,
        "storage_root": str(settings.storage_root),
        "uploads_dir": str(settings.uploads_dir),
        "downloaded_dir": str(settings.downloaded_dir),
        "output_dir": str(settings.output_dir),
    }


@app.get("/api/diagnostics")
def diagnostics():
    storage_folders = [
        settings.uploads_dir,
        settings.downloaded_dir,
        settings.output_dir,
    ]
    storage_ready = all(folder.exists() and os.access(folder, os.W_OK) for folder in storage_folders)
    tesseract_setting = os.getenv("TESSERACT_CMD", "").strip()
    tesseract_path = (
        tesseract_setting
        if tesseract_setting and Path(tesseract_setting).exists()
        else shutil.which("tesseract")
    )
    generated_registers = [
        path for path in settings.output_dir.glob("*")
        if path.suffix.lower() in {".xlsx", ".csv"}
        and "obligation_extraction" in path.stem
        and "policy_gap_assessment" not in path.stem
    ]
    return {
        "status": "ok" if storage_ready else "warning",
        "checks": [
            {"component": "Backend API", "status": "Healthy", "detail": "FastAPI is running and responding."},
            {"component": "Gap pipeline", "status": "Healthy", "detail": f"Pipeline {pipeline_metadata()['pipeline_version']} is loaded."},
            {"component": "Obligation pipeline", "status": "Healthy", "detail": f"Pipeline {OBLIGATION_PIPELINE_VERSION} is loaded with document-grounded accuracy validation."},
            {"component": "Controlled benchmark", "status": "Ready", "detail": f"South African known-answer benchmark {BENCHMARK_VERSION} is available."},
            {
                "component": "FSCA crawler",
                "status": "Healthy",
                "detail": (
                    f"Offline library {CRAWLER_VERSION}: 50 checksummed official PDFs, "
                    "exact 40/2/8 category checks, and zero runtime FSCA requests."
                ),
            },
            {
                "component": "Obligation input",
                "status": "Ready",
                "detail": (
                    f"Direct PDF upload and {len(crawler_service.library())} validated "
                    "bundled official PDF(s) are available."
                ),
            },
            {"component": "Generated registers", "status": "Ready", "detail": f"{len(generated_registers)} Excel/CSV register file(s) available."},
            {"component": "Storage", "status": "Healthy" if storage_ready else "Warning", "detail": "Runtime folders are writable." if storage_ready else "One or more runtime folders are not writable."},
            {"component": "OCR", "status": "Available" if tesseract_path else "Optional", "detail": str(tesseract_path) if tesseract_path else "Tesseract was not detected; native-text PDFs still work."},
        ],
        "pipeline": pipeline_metadata(),
    }
