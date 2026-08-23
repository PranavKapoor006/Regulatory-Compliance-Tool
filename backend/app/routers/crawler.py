from __future__ import annotations

import traceback
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.crawler_service import MAX_DOWNLOAD_BATCH, crawler_service

router = APIRouter(prefix="/api/crawler", tags=["FSCA Web Crawler"])


class CrawlerSearchRequest(BaseModel):
    section: str = "All"
    year: str = "All"
    refresh: bool = False
    cached_only: bool = False


class CrawlerDownloadRequest(BaseModel):
    directive_ids: List[str]


class CrawlerCacheAllRequest(BaseModel):
    refresh: bool = False


def _raise_crawler_error(operation: str, exc: Exception) -> None:
    message = str(exc)
    if "already running" in message.lower():
        raise HTTPException(status_code=409, detail=message) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=message) from exc
    traceback.print_exc()
    raise HTTPException(
        status_code=502,
        detail=f"{operation} failed safely: {type(exc).__name__}: {message}",
    ) from exc


@router.get("/metadata")
def crawler_metadata():
    """Return local crawler metadata without contacting the FSCA website."""
    try:
        return crawler_service.metadata()
    except Exception as exc:
        _raise_crawler_error("Crawler metadata", exc)


@router.post("/search")
def crawler_search(payload: CrawlerSearchRequest):
    """Return one complete topic from the checksummed local bundle."""
    try:
        return crawler_service.search(
            section=payload.section,
            year=payload.year,
            force_refresh=payload.refresh,
            cached_only=payload.cached_only,
        )
    except Exception as exc:
        _raise_crawler_error("Crawler search", exc)


@router.post("/download")
def crawler_download(payload: CrawlerDownloadRequest):
    if not payload.directive_ids:
        raise HTTPException(status_code=400, detail="Select at least one directive to download.")
    if len(set(payload.directive_ids)) > MAX_DOWNLOAD_BATCH:
        raise HTTPException(
            status_code=422,
            detail=f"Select at most {MAX_DOWNLOAD_BATCH} bundled directives per action.",
        )
    try:
        return crawler_service.download_selected(payload.directive_ids)
    except Exception as exc:
        _raise_crawler_error("Crawler download", exc)


@router.post("/export")
def crawler_export(payload: CrawlerDownloadRequest):
    """Export selected bundled local files as one ZIP."""
    if not payload.directive_ids:
        raise HTTPException(status_code=400, detail="Select at least one directive to export.")
    if len(set(payload.directive_ids)) > MAX_DOWNLOAD_BATCH:
        raise HTTPException(
            status_code=422,
            detail=f"Export is limited to {MAX_DOWNLOAD_BATCH} selected directives per action.",
        )
    try:
        archive, result = crawler_service.export_selected(payload.directive_ids)
        return FileResponse(
            path=archive,
            media_type="application/zip",
            filename=archive.name,
            headers={
                "X-Downloaded-Count": str(len(result.get("downloaded") or [])),
                "X-Failed-Count": str(result.get("failed") or 0),
            },
        )
    except Exception as exc:
        _raise_crawler_error("Crawler export", exc)


@router.post("/cache-all")
def crawler_cache_all(_: CrawlerCacheAllRequest):
    raise HTTPException(
        status_code=409,
        detail="This action is disabled because all 50 demo-ready official PDFs are already bundled locally.",
    )


@router.post("/export-all")
def crawler_export_all(_: CrawlerCacheAllRequest):
    raise HTTPException(
        status_code=409,
        detail="Export-all is disabled; topic selection already exposes the complete local file set.",
    )


@router.get("/library")
def crawler_library():
    try:
        return {"documents": crawler_service.library()}
    except Exception as exc:
        _raise_crawler_error("Crawler library", exc)
