from __future__ import annotations

import traceback
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.crawler_service import crawler_service

router = APIRouter(prefix="/api/crawler", tags=["crawler"])


class CrawlerSearchRequest(BaseModel):
    section: str = "All"
    year: str = "All"
    refresh: bool = False


class CrawlerDownloadRequest(BaseModel):
    directive_ids: List[str]


@router.get("/metadata")
def crawler_metadata():
    try:
        return crawler_service.metadata()
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler metadata failed: {type(exc).__name__}: {exc}")


@router.post("/search")
def crawler_search(payload: CrawlerSearchRequest):
    try:
        return crawler_service.search(
            section=payload.section,
            year=payload.year,
            force_refresh=payload.refresh,
        )
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler search failed: {type(exc).__name__}: {exc}")


@router.post("/download")
def crawler_download(payload: CrawlerDownloadRequest):
    if not payload.directive_ids:
        raise HTTPException(status_code=400, detail="Select at least one directive to download.")
    try:
        return crawler_service.download_selected(payload.directive_ids)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler download failed: {type(exc).__name__}: {exc}")


@router.get("/library")
def crawler_library():
    try:
        return {"documents": crawler_service.library()}
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Crawler library failed: {type(exc).__name__}: {exc}")
