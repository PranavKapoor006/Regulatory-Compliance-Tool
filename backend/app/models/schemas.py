from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class DirectiveRecord(BaseModel):
    id: str
    title: str
    section: str = "Directives"
    category: str = "Directives"
    year: str = "Unknown"
    source_link: str
    filename: Optional[str] = None
    cached: bool = False
    downloaded: bool = False


class CrawlSearchRequest(BaseModel):
    section: Optional[str] = None
    year: Optional[str] = None


class DownloadRequest(BaseModel):
    directive_ids: List[str] = Field(default_factory=list)


class ProcessLogEntry(BaseModel):
    stage: str
    status: str
    message: str
    row_count: int = 0


class Kpi(BaseModel):
    label: str
    value: int | str


class ResultsPayload(BaseModel):
    kpis: List[Kpi]
    tabs: Dict[str, Any]
    logs: List[ProcessLogEntry]
    output_files: Dict[str, str] = Field(default_factory=dict)
