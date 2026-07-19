from pathlib import Path
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.models.schemas import CrawlSearchRequest, DownloadRequest
from app.services.crawler_service import crawler_service

router = APIRouter(prefix="/api/crawler", tags=["Web Crawler"])


@router.get("/metadata")
def crawler_metadata():
    try:
        return crawler_service.metadata()
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler metadata failed: {type(exc).__name__}: {exc}") from exc


@router.post("/search")
def crawler_search(payload: CrawlSearchRequest):
    try:
        return crawler_service.search(section=payload.section, year=payload.year)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler search failed: {type(exc).__name__}: {exc}") from exc


@router.post("/download")
def crawler_download(payload: DownloadRequest):
    if not payload.directive_ids:
        raise HTTPException(status_code=400, detail="Select at least one directive to download.")
    try:
        return crawler_service.download_selected(payload.directive_ids)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler download failed: {type(exc).__name__}: {exc}") from exc


@router.get("/library")
def directive_library():
    return {"documents": crawler_service.library()}


@router.get("/downloaded/{filename}")
def downloaded_file(filename: str):
    settings = get_settings()
    path = settings.downloaded_dir / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Downloaded directive not found.")
    return FileResponse(path, filename=path.name)
