from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import crawler, gap, obligations

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "EY Regulatory Compliance Tool for FSCA directive crawling, "
        "obligation extraction, and internal policy gap review."
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
        "message": "EY Regulatory Compliance Tool backend is running.",
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "fsca_directives_url": settings.fsca_directives_url,
        "utilities": [
            "Web Crawler",
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
