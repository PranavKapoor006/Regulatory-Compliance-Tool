from __future__ import annotations

"""
FSCA Directives Web Crawler
===========================

This module is intentionally self-contained and defensive because the FSCA Directives
page is a classic SharePoint page. In a normal browser the page uses JavaScript to
expand grouped rows, but a backend crawler gets only the grouped HTML and SharePoint
placeholders. Because of that, a reliable crawler needs more than a simple requests.get.

Design goals
------------
1. Use SharePoint REST first. No Selenium/browser automation.
2. Probe multiple likely SharePoint sites and list names because FSCA's public page
   exposes grouped rows, while the actual document list may be under a related subsite.
3. Normalize only the three required directive categories:
      - Insurer / Micro Insurer
      - Joint FSCA / PA Directives
      - Retirement Fund
4. Treat year as the directive launch/issue/publication year, not a random number.
5. Use one in-memory crawl cache so dropdown year/category filters do not trigger a
   different second crawl and accidentally show empty results.
6. Download selected directives reliably:
      - local/reference PDF copy
      - already cached file reuse
      - direct PDF URL download
      - HTML page PDF-link discovery
      - best-match reference fallback by directive number/title
7. Log everything into Crawl Log so failures are visible instead of silent.
8. Never create fake PDFs. If a file cannot be downloaded and no valid reference exists,
   the download is logged as failed.

Compatibility
-------------
This file preserves the service API expected by the existing FastAPI router:
    crawler_service.metadata()
    crawler_service.search(section, year)
    crawler_service.download_selected(directive_ids)
    crawler_service.library()

The frontend can continue calling:
    GET  /api/crawler/metadata
    POST /api/crawler/search
    POST /api/crawler/download
    GET  /api/crawler/library

Operational note
----------------
The public FSCA page currently exposes grouped category counts in static HTML, but not
individual file rows. The crawler therefore has a clear live-first, reference-safe design.
When live SharePoint rows are accessible, they are used. When the network/site blocks
file-level access, the bundled reference directives remain available so the end-to-end
workflow still works for demo and development.
"""

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import get_settings
from app.services.storage import safe_filename, unique_path


# -----------------------------------------------------------------------------
# Required categories
# -----------------------------------------------------------------------------

FSCA_DIRECTIVE_CATEGORIES: List[str] = [
    "Insurer / Micro Insurer",
    "Joint FSCA / PA Directives",
    "Retirement Fund",
]

CATEGORY_ALIASES: Dict[str, str] = {
    "insurer": "Insurer / Micro Insurer",
    "insurers": "Insurer / Micro Insurer",
    "insurance": "Insurer / Micro Insurer",
    "micro insurer": "Insurer / Micro Insurer",
    "micro-insurer": "Insurer / Micro Insurer",
    "long-term": "Insurer / Micro Insurer",
    "short-term": "Insurer / Micro Insurer",
    "ltst": "Insurer / Micro Insurer",
    "lt&st": "Insurer / Micro Insurer",
    "long term": "Insurer / Micro Insurer",
    "short term": "Insurer / Micro Insurer",
    "joint": "Joint FSCA / PA Directives",
    "prudential authority": "Joint FSCA / PA Directives",
    "pa directive": "Joint FSCA / PA Directives",
    "fsca / pa": "Joint FSCA / PA Directives",
    "fsca/pa": "Joint FSCA / PA Directives",
    "retirement": "Retirement Fund",
    "retirement fund": "Retirement Fund",
    "pension": "Retirement Fund",
}

# FSCA public Directives page. The backend config may override this.
DEFAULT_DIRECTIVES_PAGE = "https://www2.fsca.co.za/Regulatory%20Frameworks/Pages/Directives.aspx"
PUBLIC_HOST = "https://www2.fsca.co.za"
REGULATORY_FRAMEWORKS_SITE = "https://www2.fsca.co.za/Regulatory%20Frameworks"

# Candidate sites. The mentor screenshot showed an Enforcement-Matters subsite while
# the public URL is under Regulatory Frameworks. Include both and a few conservative
# variants so the crawler survives site restructuring.
CANDIDATE_SHAREPOINT_SITES: List[str] = [
    "https://www2.fsca.co.za/Enforcement-Matters",
    "https://www2.fsca.co.za/Regulatory%20Frameworks/Enforcement-Matters",
    "https://www2.fsca.co.za/Regulatory%20Frameworks",
    "https://www2.fsca.co.za",
]

# Embedded by the FSCA Directives.aspx web part. This public document library
# currently contains the exact 55 rows shown by the three grouped categories.
DIRECTIVES_LIST_GUID = "1196F9B8-9C72-4A43-9397-C02988E27043"
DIRECTIVES_LIST_SITE = "https://www2.fsca.co.za/Enforcement-Matters"

# Candidate list titles and server-relative list folders. SharePoint deployments often
# use a display title, while the internal root folder may differ.
CANDIDATE_LIST_TITLES: List[str] = [
    "Directives",
    "Regulatory Framework Documents",
    "Documents",
    "Regulatory Frameworks",
]

CANDIDATE_LIST_URLS: List[str] = [
    "/Regulatory Frameworks/Enforcement-Matters/Directives",
    "/Regulatory Frameworks/Directives",
    "/Regulatory Frameworks/Documents",
    "/Regulatory Frameworks/Regulatory Framework Documents",
]

# Internal field candidates. We read dynamically rather than assuming one exact schema.
FIELD_CANDIDATES = {
    "id": ["Id", "ID"],
    "title": ["Title", "FileLeafRef", "Name", "LinkFilename", "File/Name"],
    "description": ["Description", "Description0", "DocumentDescription", "Document_x0020_Description"],
    "document_no": [
        "Document_x0020_No",
        "Document_x0020_no",
        "Document No",
        "DocumentNo",
        "DocNo",
        "Doc_x0020_No",
    ],
    "category": ["Category1", "Category", "Category0", "Section", "Sector", "Type"],
    "subcategory": ["Subcategory", "Subcategory0", "Sub_x0020_Category", "SubCategory"],
    "year": ["Year0", "Year", "LaunchYear", "IssueYear", "PublicationYear"],
    "issue_date": [
        "Issue_x0020_Date",
        "IssueDate",
        "Publication_x0020_Date",
        "PublicationDate",
        "Date",
        "Created",
        "Modified",
    ],
    "file_ref": [
        "FileRef",
        "ServerRelativeUrl",
        "File/ServerRelativeUrl",
        "File_x0020_Url",
        "EncodedAbsUrl",
        "LinkingUrl",
    ],
    "file_leaf": ["FileLeafRef", "File/Name", "Name", "LinkFilename"],
    "file_size": ["File/Length", "FileSizeDisplay", "FileSize", "SMTotalFileStreamSize"],
    "created": ["Created"],
    "modified": ["Modified", "Last_x0020_Modified"],
}

PDF_MIME_HINTS = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
}

# Directives may be old PDFs with slightly inconsistent naming. Use broad but safe
# patterns to pull directive numbers and years from titles/file names.
DIRECTIVE_NO_RE = re.compile(
    r"(?:Directive\s*)?(?P<num>\d{1,4}\s*\.\s*[A-Za-z]\s*\.\s*[A-Za-z](?:\s*\([^)]+\))?)",
    re.I,
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
DATE_RE = re.compile(
    r"\b(?:(\d{1,2})[\-/ ](\d{1,2}|[A-Za-z]{3,9})[\-/ ](19\d{2}|20\d{2})|"
    r"(19\d{2}|20\d{2})[\-/](\d{1,2})[\-/](\d{1,2}))\b"
)

# Reject clearly wrong future/noise years. The project is in 2026, but the FSCA page may
# include strategy docs like 2025-2028; those should not drive directive launch years.
MIN_DIRECTIVE_YEAR = 1990
MAX_DIRECTIVE_YEAR = datetime.now().year + 1

# Cache lifetime keeps filtering consistent but lets user refresh during a demo.
DEFAULT_CACHE_SECONDS = int(os.getenv("FSCA_CRAWLER_CACHE_SECONDS", "300"))


# -----------------------------------------------------------------------------
# Data containers
# -----------------------------------------------------------------------------

@dataclass
class CrawlLogEntry:
    stage: str
    status: str
    message: str
    row_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "row_count": self.row_count,
        }


@dataclass
class DirectiveRecord:
    id: str
    title: str
    section: str
    category: str
    year: str
    source_link: str
    filename: str
    cached: bool = False
    downloaded: bool = False
    description: str = ""
    document_no: str = ""
    subcategory: str = ""
    launch_date: str = ""
    created: str = ""
    modified: str = ""
    local_path: str = ""
    source_type: str = "Live FSCA"
    downloadable: bool = True
    status: str = "Ready"
    warning: str = ""
    file_size_bytes: int = 0
    source_priority: int = 50

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "section": self.section,
            "category": self.category,
            "year": self.year,
            "source_link": self.source_link,
            "filename": self.filename,
            "cached": self.cached,
            "downloaded": self.downloaded,
            "description": self.description,
            "document_no": self.document_no,
            "subcategory": self.subcategory,
            "launch_date": self.launch_date,
            "created": self.created,
            "modified": self.modified,
            "local_path": self.local_path,
            "source_type": self.source_type,
            "downloadable": self.downloadable,
            "status": self.status,
            "warning": self.warning,
            "file_size_bytes": self.file_size_bytes,
        }


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _now_timestamp() -> float:
    return time.time()


def _stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _strip(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
    text = unquote(text)
    text = text.replace("\u200e", "").replace("\u200f", "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _lower(value: Any) -> str:
    return _strip(value).lower()


def _first_non_empty(values: Dict[str, Any], candidates: Sequence[str]) -> str:
    for key in candidates:
        if "/" in key:
            # Allow flattened and nested SharePoint File fields.
            value = _get_nested(values, key)
        else:
            value = values.get(key)
        text = _strip(value)
        if text:
            return text
    return ""


def _get_nested(values: Dict[str, Any], dotted_key: str) -> Any:
    current: Any = values
    for part in dotted_key.split("/"):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _to_absolute_url(url: str, base: str = PUBLIC_HOST) -> str:
    url = _strip(url)
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, quote(url, safe="/:?&=#%()[]@$,;+-_.!'*"))


def _url_path_filename(url: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    return safe_filename(name) if name else ""


def _normalise_category(value: str, haystack: str = "") -> str:
    value = _strip(value)
    value_l = value.lower()
    hay_l = _lower(f"{value} {haystack}")

    for canonical in FSCA_DIRECTIVE_CATEGORIES:
        if value == canonical or value_l == canonical.lower():
            return canonical

    for alias, canonical in CATEGORY_ALIASES.items():
        if alias in hay_l:
            return canonical

    # If no category can be inferred, default to Insurer/Micro Insurer only for LT/ST
    # style directive names; otherwise keep Unknown so the UI/log does not lie.
    if re.search(r"\b(?:ltst|lt&st|long-term|short-term|insurance|insurer)\b", hay_l):
        return "Insurer / Micro Insurer"

    return "Unknown"


def _is_real_category(value: str) -> bool:
    return value in FSCA_DIRECTIVE_CATEGORIES


def _extract_directive_no(text: str) -> str:
    text = _strip(text)
    match = DIRECTIVE_NO_RE.search(text)
    if not match:
        # Sometimes it appears as 159.A.i without the word Directive.
        simple = re.search(r"\b(\d{1,4}\.[A-Za-z]\.[A-Za-z])\b", text)
        return simple.group(1) if simple else ""
    return re.sub(r"\s+", "", match.group("num"))


def _valid_year(year: str) -> bool:
    if not year or not re.fullmatch(r"\d{4}", str(year)):
        return False
    numeric = int(year)
    return MIN_DIRECTIVE_YEAR <= numeric <= MAX_DIRECTIVE_YEAR


def _year_from_date_text(value: str) -> str:
    text = _strip(value)
    if not text:
        return ""

    # ISO / SharePoint dates.
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return text[:4] if _valid_year(text[:4]) else ""
        if "T" in text and re.match(r"^\d{4}", text):
            return text[:4] if _valid_year(text[:4]) else ""
    except Exception:
        pass

    # HTTP dates.
    try:
        parsed = parsedate_to_datetime(text)
        year = str(parsed.year)
        return year if _valid_year(year) else ""
    except Exception:
        pass

    # Month names / day month year / year-month-day.
    for match in DATE_RE.finditer(text):
        groups = [g for g in match.groups() if g]
        years = [g for g in groups if re.fullmatch(r"19\d{2}|20\d{2}", g)]
        if years and _valid_year(years[-1]):
            return years[-1]

    return ""


def _extract_launch_year(values: Dict[str, Any], haystack: str) -> str:
    # Priority 1: explicit issue/publication/launch date fields.
    for key in FIELD_CANDIDATES["issue_date"]:
        raw = _first_non_empty(values, [key])
        year = _year_from_date_text(raw)
        if year:
            return year

    # Priority 2: explicit year fields.
    raw_year = _first_non_empty(values, FIELD_CANDIDATES["year"])
    if _valid_year(raw_year):
        return raw_year

    # Priority 3: parse the directive title/file/document number. SharePoint Created
    # and Modified often reflect a migration date rather than the directive's year.
    candidates = YEAR_RE.findall(_strip(haystack))
    valid = [year for year in candidates if _valid_year(year)]
    if valid:
        # Use earliest valid year in title/file text, because a range like 2025-2028
        # should not accidentally choose the later future year as launch year.
        return sorted(valid)[0]

    # Priority 4: use Created/Modified only when the directive itself has no year.
    for key in FIELD_CANDIDATES["created"] + FIELD_CANDIDATES["modified"]:
        raw = _first_non_empty(values, [key])
        year = _year_from_date_text(raw)
        if year:
            return year

    return "Unknown"


def _looks_like_directive_text(text: str) -> bool:
    text_l = _lower(text)
    if "directive" in text_l:
        return True
    if re.search(r"\b\d{1,4}\.[a-z]\.[a-z]\b", text_l):
        return True
    if "long-term" in text_l and "short-term" in text_l:
        return True
    if "ltst" in text_l or "lt&st" in text_l:
        return True
    return False


def _looks_like_non_directive(text: str) -> bool:
    text_l = _lower(text)
    blockers = [
        "regulatory strategy",
        "annual report",
        "strategic plan",
        "press release",
        "general publication",
        "vacancy",
        "tender",
        "newsletter",
    ]
    return any(blocker in text_l for blocker in blockers)


def _is_pdf_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or ".pdf" in path


def _content_is_pdf(content: bytes, content_type: str = "") -> bool:
    if content[:5] == b"%PDF-":
        return True
    ctype = content_type.lower().split(";")[0].strip()
    # application/octet-stream is only a hint. Accepting it without a PDF
    # signature can save a SharePoint login/error page with a .pdf extension.
    if ctype == "application/pdf" and b"%PDF-" in content[:1024]:
        return True
    return False


def _flatten_sharepoint_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten selected nested SharePoint File fields while retaining originals."""
    values = dict(item)
    file_obj = item.get("File")
    if isinstance(file_obj, dict):
        for key, value in file_obj.items():
            values[f"File/{key}"] = value
    return values


def _dedupe_records(records: Iterable[DirectiveRecord]) -> List[DirectiveRecord]:
    by_key: Dict[str, DirectiveRecord] = {}

    for record in records:
        if not record.title and not record.filename:
            continue
        if _looks_like_non_directive(f"{record.title} {record.filename} {record.description}"):
            # Keep only if it explicitly says directive too.
            if not _looks_like_directive_text(f"{record.title} {record.filename} {record.description}"):
                continue

        directive_no = _extract_directive_no(f"{record.document_no} {record.title} {record.filename}")
        filename_key = record.filename.lower()
        url_key = record.source_link.lower()
        local_key = record.local_path.lower()
        key = directive_no.lower() or filename_key or url_key or local_key or record.id

        existing = by_key.get(key)
        if existing is None:
            by_key[key] = record
            continue

        # Prefer records that are actually downloadable and have better source priority.
        existing_score = (10 if existing.downloadable else 0) + existing.source_priority
        new_score = (10 if record.downloadable else 0) + record.source_priority
        if new_score > existing_score:
            by_key[key] = record

    records_out = list(by_key.values())
    records_out.sort(key=lambda r: (r.section, r.year if r.year != "Unknown" else "9999", r.title))
    return records_out


def _category_counts_from_static_html(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for match in re.finditer(r"Category1\s*:\s*(.*?)\s*[\u200e\u200f\s]*\((\d+)\)", text, flags=re.I):
        category = _normalise_category(match.group(1))
        if _is_real_category(category):
            counts[category] = int(match.group(2))
    return counts


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


# -----------------------------------------------------------------------------
# Main crawler service
# -----------------------------------------------------------------------------

class CrawlerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.cache_seconds = DEFAULT_CACHE_SECONDS
        self.session = self._create_session()
        self.last_records: List[Dict[str, Any]] = []
        self.last_log: List[Dict[str, Any]] = []
        self.last_category_counts: Dict[str, int] = {}
        self.last_crawl_time: float = 0.0

    # ------------------------------------------------------------------
    # Session / logging
    # ------------------------------------------------------------------

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json;odata=nometadata, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return session

    def _log(self, logs: List[CrawlLogEntry], stage: str, status: str, message: str, row_count: int = 0) -> None:
        logs.append(CrawlLogEntry(stage=stage, status=status, message=message, row_count=row_count))

    def _logs_to_dicts(self, logs: List[CrawlLogEntry]) -> List[Dict[str, Any]]:
        return [entry.to_dict() for entry in logs]

    # ------------------------------------------------------------------
    # Record creation
    # ------------------------------------------------------------------

    def _stable_id(self, *parts: object) -> str:
        """Class wrapper around the module-level stable ID helper.

        Older patches/call sites sometimes used self._stable_id(...), while this
        module originally exposed _stable_id(...) as a top-level helper. Keeping
        this wrapper makes both styles safe and prevents AttributeError crashes.
        """
        return _stable_id("|".join(str(part or "") for part in parts))

    def _make_record(
        self,
        *,
        title: str = "",
        source_link: str = "",
        file_url: str = "",
        filename: str = "",
        file_name: str = "",
        section: str = "Unknown",
        category: str = "Unknown",
        year: str = "Unknown",
        description: str = "",
        document_no: str = "",
        doc_no: str = "",
        subcategory: str = "",
        launch_date: str = "",
        created: str = "",
        modified: str = "",
        local_path: str = "",
        source_type: str = "Live FSCA",
        source: str = "",
        downloadable: bool = True,
        download_ready: Optional[bool] = None,
        status: str = "Ready",
        warning: str = "",
        file_size_bytes: int = 0,
        source_priority: int = 50,
        cached: Optional[bool] = None,
        downloaded: Optional[bool] = None,
        **extra: Any,
    ) -> DirectiveRecord:
        """Create a normalized directive record.

        This method intentionally accepts several alias names because the
        frontend, older crawler patches, and reference-library paths have used
        slightly different names over time: doc_no/document_no, file_name/filename,
        file_url/source_link, source/source_type, and download_ready/downloadable.
        Accepting them here keeps the crawler stable instead of throwing 500s.
        """
        del extra  # accepted for forward compatibility; DirectiveRecord is strict.

        title = _strip(title)
        description = _strip(description)
        document_no = _strip(document_no or doc_no)
        subcategory = _strip(subcategory)
        source_link = _strip(source_link or file_url)
        local_path = _strip(local_path)
        filename = _strip(filename or file_name)

        if source and source_type == "Live FSCA":
            source_type = source
        if download_ready is not None:
            downloadable = bool(download_ready)

        if not filename:
            filename = _url_path_filename(source_link)
        if not filename and local_path:
            filename = Path(local_path).name
        if not filename:
            name_base = document_no or title or self._stable_id(source_link, local_path)
            filename = f"{safe_filename(name_base)}.pdf"
        filename = safe_filename(filename)

        if filename and not Path(filename).suffix and (source_link or local_path):
            filename = f"{filename}.pdf"

        if not title:
            title = Path(filename).stem

        haystack = " ".join(
            [
                title,
                description,
                document_no,
                subcategory,
                filename,
                source_link,
                local_path,
            ]
        )
        normalised_category = _normalise_category(category, haystack)
        normalised_section = _normalise_category(section, haystack)
        if not _is_real_category(normalised_section):
            normalised_section = normalised_category
        if not _is_real_category(normalised_category):
            normalised_category = normalised_section
        if not _is_real_category(normalised_section):
            normalised_section = "Unknown"
        if not _is_real_category(normalised_category):
            normalised_category = "Unknown"

        if not _valid_year(year):
            year = _extract_launch_year({}, " ".join([launch_date, created, modified, haystack]))

        cached_path = self.settings.downloaded_dir / filename
        if cached is None:
            cached = cached_path.exists()
        if downloaded is None:
            downloaded = bool(cached)

        if not source_link and not local_path:
            downloadable = False
            status = "No source URL"

        record_id = self._stable_id(document_no, title, source_link, local_path, filename)

        return DirectiveRecord(
            id=record_id,
            title=title,
            section=normalised_section,
            category=normalised_category,
            year=year or "Unknown",
            source_link=source_link,
            filename=filename,
            cached=bool(cached),
            downloaded=bool(downloaded),
            description=description,
            document_no=document_no or _extract_directive_no(haystack),
            subcategory=subcategory,
            launch_date=launch_date,
            created=created,
            modified=modified,
            local_path=local_path,
            source_type=source_type,
            downloadable=bool(downloadable),
            status=status,
            warning=warning,
            file_size_bytes=file_size_bytes,
            source_priority=source_priority,
        )

    # ------------------------------------------------------------------
    # SharePoint REST crawling
    # ------------------------------------------------------------------

    def _sharepoint_get(self, url: str, params: Optional[Dict[str, str]] = None, timeout: int = 45) -> requests.Response:
        headers = {"Accept": "application/json;odata=nometadata"}
        return self.session.get(url, params=params, headers=headers, timeout=timeout, verify=False)

    def _parse_sp_json_items(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("value"), list):
            return payload["value"]
        d = payload.get("d")
        if isinstance(d, dict):
            if isinstance(d.get("results"), list):
                return d["results"]
            if isinstance(d.get("GetListDataAsStream"), dict):
                stream = d["GetListDataAsStream"]
                if isinstance(stream.get("Row"), list):
                    return stream["Row"]
        if isinstance(payload.get("Row"), list):
            return payload["Row"]
        if isinstance(payload.get("ListData"), dict) and isinstance(payload["ListData"].get("Row"), list):
            return payload["ListData"]["Row"]
        return []

    def _normalise_sp_item(self, raw_item: Dict[str, Any], source_type: str, source_priority: int) -> Optional[DirectiveRecord]:
        values = _flatten_sharepoint_item(raw_item)

        title = _first_non_empty(values, FIELD_CANDIDATES["title"])
        description = _first_non_empty(values, FIELD_CANDIDATES["description"])
        document_no = _first_non_empty(values, FIELD_CANDIDATES["document_no"])
        category = _first_non_empty(values, FIELD_CANDIDATES["category"])
        subcategory = _first_non_empty(values, FIELD_CANDIDATES["subcategory"])
        file_ref = _first_non_empty(values, FIELD_CANDIDATES["file_ref"])
        file_leaf = _first_non_empty(values, FIELD_CANDIDATES["file_leaf"])
        created = _first_non_empty(values, FIELD_CANDIDATES["created"])
        modified = _first_non_empty(values, FIELD_CANDIDATES["modified"])
        launch_date = _first_non_empty(values, FIELD_CANDIDATES["issue_date"])
        file_size = _safe_int(_first_non_empty(values, FIELD_CANDIDATES["file_size"]))

        haystack = " ".join([title, description, document_no, category, subcategory, file_ref, file_leaf])
        identity_text = " ".join([title, description, document_no, file_leaf])
        explicit_category = _normalise_category(category, category)
        explicit_directive = _looks_like_directive_text(identity_text)

        # Only accept an explicit directive identity or one of the three required
        # SharePoint categories. Do not infer record type from a parent folder such
        # as "Regulatory Frameworks/Archived Documents"; that previously pulled
        # more than 1,000 unrelated reports into the Directives result set.
        if _looks_like_non_directive(haystack) and not _looks_like_directive_text(haystack):
            return None
        if not explicit_directive and not _is_real_category(explicit_category):
            return None

        source_link = _to_absolute_url(file_ref, PUBLIC_HOST)
        if not source_link and values.get("EncodedAbsUrl"):
            source_link = _strip(values.get("EncodedAbsUrl"))
        if not source_link and values.get("LinkingUrl"):
            source_link = _strip(values.get("LinkingUrl"))

        filename = file_leaf or _url_path_filename(source_link)
        if not filename:
            filename = safe_filename(title or document_no or "directive") + ".pdf"

        section = explicit_category if _is_real_category(explicit_category) else _normalise_category(subcategory, identity_text)
        year = _extract_launch_year(values, haystack)
        if year == "Unknown":
            year = _extract_launch_year({}, " ".join([created, modified, title, filename]))

        downloadable = bool(source_link) and filename.lower().endswith(".pdf")
        status = "Ready" if downloadable else ("Unsupported file type" if source_link else "No source URL")

        return self._make_record(
            title=title or filename,
            source_link=source_link,
            filename=filename,
            section=section,
            category=section,
            year=year,
            description=description,
            document_no=document_no,
            subcategory=subcategory,
            launch_date=launch_date,
            created=created,
            modified=modified,
            source_type=source_type,
            downloadable=downloadable,
            status=status,
            file_size_bytes=file_size,
            source_priority=source_priority,
        )

    def _normalise_sp_items(self, items: List[Dict[str, Any]], source_type: str, source_priority: int) -> List[DirectiveRecord]:
        records: List[DirectiveRecord] = []
        for item in items:
            try:
                record = self._normalise_sp_item(item, source_type, source_priority)
                if record:
                    records.append(record)
            except Exception:
                # Bad rows should not kill the crawler.
                continue
        return _dedupe_records(records)

    def _build_list_item_endpoints(self) -> List[Tuple[str, Dict[str, str], str]]:
        endpoints: List[Tuple[str, Dict[str, str], str]] = [
            (
                f"{DIRECTIVES_LIST_SITE}/_api/web/lists(guid'{DIRECTIVES_LIST_GUID}')/items",
                {
                    "$top": "5000",
                    "$select": "*,File/ServerRelativeUrl,File/Name,File/Length",
                    "$expand": "File",
                },
                "FSCA Directives web-part list",
            )
        ]
        select = ",".join(
            [
                "Id",
                "Title",
                "Description",
                "Document_x0020_No",
                "Document_x0020_no",
                "Category1",
                "Category",
                "Subcategory",
                "Subcategory0",
                "Year0",
                "Year",
                "Issue_x0020_Date",
                "Publication_x0020_Date",
                "Created",
                "Modified",
                "FileRef",
                "FileLeafRef",
                "File/ServerRelativeUrl",
                "File/Name",
                "File/Length",
            ]
        )

        for site in CANDIDATE_SHAREPOINT_SITES:
            for list_title in CANDIDATE_LIST_TITLES:
                endpoints.append(
                    (
                        f"{site}/_api/web/lists/getbytitle('{list_title}')/items",
                        {
                            "$top": "5000",
                            "$select": select,
                            "$expand": "File",
                        },
                        f"SharePoint list title: {list_title} @ {site}",
                    )
                )
        return endpoints

    def _crawl_sharepoint_list_items(self, logs: List[CrawlLogEntry]) -> List[DirectiveRecord]:
        all_records: List[DirectiveRecord] = []
        for endpoint, params, label in self._build_list_item_endpoints():
            try:
                response = self._sharepoint_get(endpoint, params=params)
                if response.status_code in {401, 403, 404}:
                    self._log(logs, "Crawl", "Warning", f"{label} unavailable: HTTP {response.status_code}", 0)
                    continue
                response.raise_for_status()
                payload = response.json()
                items = self._parse_sp_json_items(payload)
                records = self._normalise_sp_items(items, source_type="SharePoint REST", source_priority=90)
                self._log(logs, "Crawl", "Completed", f"{label} returned {len(items)} item(s).", len(items))
                if records:
                    self._log(logs, "Results", "Completed", f"Normalised {len(records)} directive row(s) from {label}.", len(records))
                    all_records.extend(records)
                    # Do not stop immediately. Another endpoint may expose better File URLs.
            except Exception as exc:
                self._log(logs, "Crawl", "Warning", f"{label} failed: {type(exc).__name__}: {exc}", 0)
        return _dedupe_records(all_records)

    def _crawl_sharepoint_list_inventory(self, logs: List[CrawlLogEntry]) -> List[DirectiveRecord]:
        """Discover lists first, then query any list whose title/folder resembles Directives."""
        records: List[DirectiveRecord] = []
        for site in CANDIDATE_SHAREPOINT_SITES:
            endpoint = f"{site}/_api/web/lists"
            params = {
                "$select": "Title,Id,Hidden,ItemCount,RootFolder/ServerRelativeUrl",
                "$expand": "RootFolder",
                "$filter": "Hidden eq false",
                "$top": "5000",
            }
            try:
                response = self._sharepoint_get(endpoint, params=params)
                if response.status_code in {401, 403, 404}:
                    self._log(logs, "Discover", "Warning", f"List inventory unavailable for {site}: HTTP {response.status_code}", 0)
                    continue
                response.raise_for_status()
                lists = self._parse_sp_json_items(response.json())
                self._log(logs, "Discover", "Completed", f"Discovered {len(lists)} SharePoint list(s) at {site}.", len(lists))
                for sp_list in lists:
                    title = _strip(sp_list.get("Title"))
                    root = _strip(_get_nested(sp_list, "RootFolder/ServerRelativeUrl"))
                    haystack = f"{title} {root}"
                    if "directive" not in haystack.lower():
                        continue
                    items_endpoint = f"{site}/_api/web/lists/getbytitle('{title}')/items"
                    try:
                        item_response = self._sharepoint_get(
                            items_endpoint,
                            params={
                                "$top": "5000",
                                "$select": "*,File/ServerRelativeUrl,File/Name,File/Length",
                                "$expand": "File",
                            },
                        )
                        item_response.raise_for_status()
                        items = self._parse_sp_json_items(item_response.json())
                        normalised = self._normalise_sp_items(items, source_type=f"SharePoint discovered: {title}", source_priority=95)
                        if normalised:
                            self._log(logs, "Results", "Completed", f"Discovered list {title} produced directive rows.", len(normalised))
                            records.extend(normalised)
                    except Exception as exc:
                        self._log(logs, "Discover", "Warning", f"Could not query discovered list {title}: {type(exc).__name__}: {exc}", 0)
            except Exception as exc:
                self._log(logs, "Discover", "Warning", f"List discovery failed for {site}: {type(exc).__name__}: {exc}", 0)
        return _dedupe_records(records)

    def _crawl_render_list_data_as_stream(self, logs: List[CrawlLogEntry]) -> List[DirectiveRecord]:
        records: List[DirectiveRecord] = []
        for site in CANDIDATE_SHAREPOINT_SITES:
            for list_url in CANDIDATE_LIST_URLS:
                quoted_list_url = quote(list_url, safe="/")
                endpoint = f"{site}/_api/web/GetList(@listUrl)/RenderListDataAsStream?@listUrl='{quoted_list_url}'"
                body = {
                    "parameters": {
                        "RenderOptions": 2,
                        "AllowMultipleValueFilterForTaxonomyFields": True,
                        "AddRequiredFields": True,
                    }
                }
                headers = {
                    "Accept": "application/json;odata=nometadata",
                    "Content-Type": "application/json;odata=nometadata",
                }
                try:
                    response = self.session.post(endpoint, data=json.dumps(body), headers=headers, timeout=45, verify=False)
                    if response.status_code in {401, 403, 404}:
                        self._log(logs, "Crawl", "Warning", f"RenderListData unavailable for {list_url}: HTTP {response.status_code}", 0)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    rows = self._parse_sp_json_items(payload)
                    normalised = self._normalise_sp_items(rows, source_type="SharePoint RenderListData", source_priority=85)
                    self._log(logs, "Crawl", "Completed", f"RenderListData returned {len(rows)} row(s) for {list_url}.", len(rows))
                    if normalised:
                        records.extend(normalised)
                except Exception as exc:
                    self._log(logs, "Crawl", "Warning", f"RenderListData failed for {list_url}: {type(exc).__name__}: {exc}", 0)
        return _dedupe_records(records)

    # ------------------------------------------------------------------
    # HTML crawling
    # ------------------------------------------------------------------

    def _crawl_public_html(self, logs: List[CrawlLogEntry]) -> Tuple[List[DirectiveRecord], Dict[str, int]]:
        records: List[DirectiveRecord] = []
        category_counts: Dict[str, int] = {}
        url = self.settings.fsca_directives_url or DEFAULT_DIRECTIVES_PAGE
        try:
            response = self.session.get(url, timeout=45, verify=False)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text("\n", strip=True)
            category_counts = _category_counts_from_static_html(text)
            if category_counts:
                self._log(logs, "Crawl", "Completed", "Public page exposes grouped directive category counts.", sum(category_counts.values()))
            else:
                self._log(logs, "Crawl", "Completed", "Fetched public FSCA Directives page.", 1)

            # Static HTML often only has group rows, but scan links anyway.
            for anchor in soup.find_all("a", href=True):
                label = _strip(anchor.get_text(" ", strip=True))
                href = _strip(anchor.get("href"))
                if not href or href.lower().startswith("javascript") or "{item" in href.lower():
                    continue
                full_url = _to_absolute_url(href, url)
                haystack = f"{label} {href} {full_url}"
                filename = _url_path_filename(full_url) or f"{safe_filename(label)}.pdf"
                # The page contains unrelated PDF navigation links. Only keep
                # file-level links that identify themselves as directives.
                if not _looks_like_directive_text(f"{label} {filename} {full_url}"):
                    continue
                if _looks_like_non_directive(haystack) and not _looks_like_directive_text(haystack):
                    continue
                category = _normalise_category("", haystack)
                if not _is_real_category(category):
                    category = "Unknown"
                records.append(
                    self._make_record(
                        title=label or filename,
                        source_link=full_url,
                        filename=filename,
                        section=category,
                        category=category,
                        year=_extract_launch_year({}, haystack),
                        description="Parsed from public Directives HTML link.",
                        source_type="Public HTML",
                        source_priority=40,
                    )
                )

            records = _dedupe_records(records)
            if records:
                self._log(logs, "Results", "Completed", "Parsed file-level links from public HTML.", len(records))
            elif category_counts:
                self._log(logs, "Results", "Warning", "Public HTML shows category groups but not individual downloadable file rows.", sum(category_counts.values()))
            return records, category_counts
        except Exception as exc:
            self._log(logs, "Crawl", "Warning", f"Public HTML crawl failed: {type(exc).__name__}: {exc}", 0)
            return [], category_counts

    # ------------------------------------------------------------------
    # Reference/local directives
    # ------------------------------------------------------------------

    def _reference_directives(self, logs: List[CrawlLogEntry]) -> List[DirectiveRecord]:
        reference_dir = self.settings.reference_directives_root
        reference_dir.mkdir(parents=True, exist_ok=True)
        pdfs = sorted(reference_dir.glob("*.pdf"))
        records: List[DirectiveRecord] = []
        known_titles = {
            "101": "Directive 101.A.i (LT&ST) - Directors and Managing Executive",
            "159": "Directive 159.A.i (LT&ST) - Outsourcing",
        }
        known_years = {
            "101": "2011",
            "159": "2012",
        }
        for path in pdfs:
            stem = path.stem
            directive_no = _extract_directive_no(path.name) or _extract_directive_no(stem)
            numeric = re.search(r"\b(\d{1,4})\b", path.name)
            key = numeric.group(1) if numeric else directive_no.split(".")[0]
            title = known_titles.get(key, stem)
            year = known_years.get(key, _extract_launch_year({}, f"{path.name} {stem}"))
            haystack = f"{title} {path.name} {directive_no}"
            category = _normalise_category("Insurer / Micro Insurer", haystack)
            records.append(
                self._make_record(
                    title=title,
                    source_link=self.settings.fsca_directives_url or DEFAULT_DIRECTIVES_PAGE,
                    filename=path.name,
                    section=category,
                    category=category,
                    year=year,
                    description="Bundled reference directive available for reliable offline/demo workflow.",
                    document_no=directive_no,
                    local_path=str(path),
                    source_type="Reference file",
                    downloadable=True,
                    status="Ready",
                    source_priority=70,
                )
            )
        if records:
            self._log(logs, "Reference", "Completed", "Loaded bundled reference directive PDF(s).", len(records))
        else:
            self._log(logs, "Reference", "Warning", f"No reference PDFs found in {reference_dir}.", 0)
        return records

    def _already_downloaded_records(self, logs: List[CrawlLogEntry]) -> List[DirectiveRecord]:
        records: List[DirectiveRecord] = []
        folder = self.settings.downloaded_dir
        folder.mkdir(parents=True, exist_ok=True)
        for path in sorted(folder.glob("*.pdf")):
            haystack = path.name
            directive_no = _extract_directive_no(haystack)
            if not directive_no and not _looks_like_directive_text(haystack):
                continue
            category = _normalise_category("", haystack)
            if not _is_real_category(category):
                category = "Insurer / Micro Insurer" if directive_no else "Unknown"
            records.append(
                self._make_record(
                    title=path.stem,
                    source_link="",
                    filename=path.name,
                    section=category,
                    category=category,
                    year=_extract_launch_year({}, haystack),
                    description="Already present in downloaded directive library.",
                    document_no=directive_no,
                    local_path=str(path),
                    source_type="Downloaded library",
                    downloadable=True,
                    cached=True,
                    status="Cached",
                    source_priority=60,
                )
            )
        if records:
            self._log(logs, "Cache", "Completed", "Loaded already downloaded directive PDF(s).", len(records))
        return records

    # ------------------------------------------------------------------
    # Main crawl / cache
    # ------------------------------------------------------------------

    def _crawl_live_and_reference(self, force: bool = False) -> Dict[str, Any]:
        if not force and self.last_records and (_now_timestamp() - self.last_crawl_time) < self.cache_seconds:
            return {
                "records": self.last_records,
                "logs": self.last_log,
                "category_counts": self.last_category_counts,
                "from_cache": True,
            }

        logs: List[CrawlLogEntry] = []
        self._log(logs, "Configure", "Completed", f"Using FSCA Directives source: {self.settings.fsca_directives_url or DEFAULT_DIRECTIVES_PAGE}", 0)
        self._log(logs, "Configure", "Completed", "Crawler mode: SharePoint REST first, public HTML second, reference PDFs last.", 0)

        live_records: List[DirectiveRecord] = []
        category_counts: Dict[str, int] = {}

        # 1. Try direct list-item endpoints.
        rest_records = self._crawl_sharepoint_list_items(logs)
        live_records.extend(rest_records)

        # 2. Try list discovery if direct endpoints did not produce enough rows.
        if len(live_records) < 5:
            discovered_records = self._crawl_sharepoint_list_inventory(logs)
            live_records.extend(discovered_records)

        # 3. Try RenderListDataAsStream if normal list endpoints fail/under-return.
        if len(live_records) < 5:
            render_records = self._crawl_render_list_data_as_stream(logs)
            live_records.extend(render_records)

        # 4. Public HTML gives category counts and sometimes links.
        html_records, counts = self._crawl_public_html(logs)
        category_counts.update(counts)
        # HTML navigation links are not directive rows. Use them only if the real
        # SharePoint list is unavailable; otherwise the authoritative 55-row list
        # is the complete result set.
        if not live_records:
            live_records.extend(html_records)

        live_records = _dedupe_records(live_records)
        if live_records:
            self._log(logs, "Results", "Completed", "Live crawler produced directive record(s).", len(live_records))
        else:
            self._log(logs, "Results", "Warning", "Live crawler did not produce file-level directive records in this environment.", 0)

        # 5. Always merge reference/downloaded records so Download Selected works in demos.
        reference_records = self._reference_directives(logs)
        downloaded_records = self._already_downloaded_records(logs)
        all_records = _dedupe_records([*live_records, *reference_records, *downloaded_records])

        # Keep only real three categories where known. Unknown records remain visible only if
        # they are clearly direct files, but filter dropdown remains the three categories.
        good_records: List[DirectiveRecord] = []
        for record in all_records:
            if record.section == "Unknown":
                record.warning = record.warning or "Category could not be confidently determined."
            good_records.append(record)

        if not good_records:
            self._log(logs, "Results", "Failed", "No directives were found from live FSCA, downloaded library, or reference files.", 0)
        else:
            self._log(logs, "Results", "Completed", "Crawler result set ready for filtering and download.", len(good_records))

        self.last_records = [record.to_dict() for record in good_records]
        self.last_log = self._logs_to_dicts(logs)
        self.last_category_counts = category_counts
        self.last_crawl_time = _now_timestamp()

        return {
            "records": self.last_records,
            "logs": self.last_log,
            "category_counts": category_counts,
            "from_cache": False,
        }

    def crawl(self) -> Dict[str, Any]:
        return self._crawl_live_and_reference(force=True)

    # ------------------------------------------------------------------
    # Filtering / metadata
    # ------------------------------------------------------------------

    def _filter_records(self, records: List[Dict[str, Any]], section: Optional[str], year: Optional[str]) -> List[Dict[str, Any]]:
        filtered = records
        if section and section != "All":
            filtered = [record for record in filtered if record.get("section") == section or record.get("category") == section]
        if year and year != "All":
            filtered = [record for record in filtered if str(record.get("year")) == str(year)]
        return filtered

    def _available_years(self, records: List[Dict[str, Any]], section: Optional[str] = None) -> List[str]:
        relevant = records
        if section and section != "All":
            relevant = [record for record in records if record.get("section") == section or record.get("category") == section]
        years = sorted({str(record.get("year")) for record in relevant if _valid_year(str(record.get("year")))}, reverse=True)
        return ["All"] + years

    def _kpis_for_records(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        domains = sorted({record.get("section") for record in records if record.get("section") and record.get("section") != "Unknown"})
        downloaded = sum(1 for record in records if record.get("downloaded"))
        cached = sum(1 for record in records if record.get("cached"))
        return {
            "total_directives": len(records),
            "domains": len(domains),
            "downloaded": downloaded,
            "cached": cached,
        }

    def search(
        self,
        section: str | None = None,
        year: str | None = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        crawl_result = self._crawl_live_and_reference(force=force_refresh)
        base_records = list(crawl_result["records"])
        filtered = self._filter_records(base_records, section, year)
        logs = list(crawl_result["logs"])
        logs.append(
            CrawlLogEntry(
                "Filter",
                "Completed",
                f"Applied filters against cached crawl: section={section or 'All'}, directive launch year={year or 'All'}.",
                len(filtered),
            ).to_dict()
        )
        if year and year != "All" and not filtered:
            available = self._available_years(base_records, section)
            logs.append(
                CrawlLogEntry(
                    "Filter",
                    "Warning",
                    f"No directives matched launch year {year}. Available years for this category: {', '.join(available[1:]) or 'none'}.",
                    0,
                ).to_dict()
            )
        return {
            "records": filtered,
            "logs": logs,
            "category_counts": crawl_result.get("category_counts", {}),
            "available_years": self._available_years(base_records, section),
            "kpis": self._kpis_for_records(filtered),
        }

    def metadata(self) -> Dict[str, Any]:
        crawl_result = self._crawl_live_and_reference(force=False)
        records = list(crawl_result["records"])
        return {
            "sections": ["All"] + FSCA_DIRECTIVE_CATEGORIES,
            "years": self._available_years(records),
            "source_url": self.settings.fsca_directives_url or DEFAULT_DIRECTIVES_PAGE,
            "category_counts": crawl_result.get("category_counts", {}),
        }

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------

    def _find_record(self, directive_id: str) -> Optional[Dict[str, Any]]:
        if not self.last_records:
            self._crawl_live_and_reference(force=False)
        for record in self.last_records:
            if record.get("id") == directive_id:
                return record
        return None

    def _reference_match(self, record: Dict[str, Any]) -> Optional[Path]:
        reference_dir = self.settings.reference_directives_root
        if not reference_dir.exists():
            return None
        haystack = " ".join(
            [
                str(record.get("document_no", "")),
                str(record.get("title", "")),
                str(record.get("filename", "")),
                str(record.get("description", "")),
            ]
        )
        directive_no = _extract_directive_no(haystack)
        # Reference substitution is safe only for an exact directive identity
        # (for example 159.A.i). Generic words such as "directive" or an ordinal
        # SharePoint Document No. must never cause Directive 101 to be copied under
        # an unrelated live filename.
        if not directive_no:
            return None
        candidates = sorted(reference_dir.glob("*.pdf"))
        normalized_no = directive_no.lower().replace(" ", "").split("(", 1)[0]
        for path in candidates:
            candidate_no = _extract_directive_no(path.name)
            candidate_normalized = candidate_no.lower().replace(" ", "").split("(", 1)[0]
            if candidate_no and candidate_normalized == normalized_no:
                return path
        return None

    def _copy_pdf_to_library(self, source: Path, desired_filename: str, logs: List[CrawlLogEntry], message_prefix: str) -> Dict[str, Any]:
        if not source.exists():
            raise FileNotFoundError(str(source))
        filename = safe_filename(desired_filename or source.name)
        if not filename.lower().endswith(".pdf"):
            filename = f"{Path(filename).stem}.pdf"
        target = self.settings.downloaded_dir / filename
        if target.exists():
            self._log(logs, "Download", "Cached", f"Already available: {target.name}", 1)
        else:
            target = unique_path(self.settings.downloaded_dir, filename)
            shutil.copy2(source, target)
            self._log(logs, "Download", "Completed", f"{message_prefix}: {target.name}", 1)
        return {"filename": target.name, "path": str(target), "size_bytes": target.stat().st_size, "cached": True}

    def _discover_pdf_link_from_html(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url, timeout=45, verify=False)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" in content_type or response.content[:5] == b"%PDF-":
                return url
            soup = BeautifulSoup(response.text, "html.parser")
            candidate_links: List[str] = []
            for anchor in soup.find_all("a", href=True):
                href = _strip(anchor.get("href"))
                label = _strip(anchor.get_text(" ", strip=True))
                full = _to_absolute_url(href, url)
                if _is_pdf_url(full) or "download" in _lower(label) or "pdf" in _lower(label):
                    candidate_links.append(full)
            for script in soup.find_all("script"):
                text = script.get_text("\n")
                for match in re.finditer(r"['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]", text, flags=re.I):
                    candidate_links.append(_to_absolute_url(match.group(1), url))
            return candidate_links[0] if candidate_links else None
        except Exception:
            return None

    def _download_url_to_library(self, url: str, filename: str, logs: List[CrawlLogEntry]) -> Dict[str, Any]:
        if not url:
            raise ValueError("No source URL")
        final_url = url
        if not _is_pdf_url(url):
            discovered = self._discover_pdf_link_from_html(url)
            if discovered:
                final_url = discovered
        response = self.session.get(final_url, timeout=90, verify=False)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        content = response.content or b""
        if len(content) < 200:
            raise ValueError("Downloaded content is too small to be a directive PDF")
        if not _content_is_pdf(content, content_type) and not _is_pdf_url(final_url):
            raise ValueError(f"Downloaded content is not a PDF. Content-Type={content_type or 'unknown'}")
        filename = safe_filename(filename or _url_path_filename(final_url) or "directive.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{Path(filename).stem}.pdf"
        target = unique_path(self.settings.downloaded_dir, filename)
        target.write_bytes(content)
        self._log(logs, "Download", "Completed", f"Downloaded {target.name}", 1)
        return {"filename": target.name, "path": str(target), "size_bytes": target.stat().st_size, "cached": False}

    def download_selected(self, directive_ids: List[str]) -> Dict[str, Any]:
        if not self.last_records:
            self._crawl_live_and_reference(force=False)
        logs: List[CrawlLogEntry] = []
        downloaded: List[Dict[str, Any]] = []

        for directive_id in directive_ids:
            record = self._find_record(directive_id)
            if not record:
                self._log(logs, "Download", "Failed", f"Unknown directive id: {directive_id}", 0)
                continue

            title = str(record.get("title") or record.get("filename") or directive_id)
            filename = str(record.get("filename") or f"{directive_id}.pdf")
            local_path = str(record.get("local_path") or "")

            try:
                # 1. Existing local/reference/downloaded file.
                if local_path and Path(local_path).exists():
                    copied = self._copy_pdf_to_library(Path(local_path), filename, logs, "Copied directive into crawler library")
                    downloaded.append({**record, **copied, "downloaded": True, "cached": True})
                    continue

                # 2. Already cached by filename.
                cached_path = self.settings.downloaded_dir / filename
                if cached_path.exists():
                    self._log(logs, "Download", "Cached", f"Already available: {cached_path.name}", 1)
                    downloaded.append({**record, "filename": cached_path.name, "path": str(cached_path), "downloaded": True, "cached": True})
                    continue

                # 3. Reference match by directive number/title.
                reference = self._reference_match(record)
                if reference:
                    copied = self._copy_pdf_to_library(reference, filename or reference.name, logs, "Copied matching reference directive")
                    downloaded.append({**record, **copied, "downloaded": True, "cached": True})
                    continue

                # 4. Live URL download / HTML PDF discovery.
                source_link = str(record.get("source_link") or "")
                if source_link:
                    dl = self._download_url_to_library(source_link, filename, logs)
                    downloaded.append({**record, **dl, "downloaded": True, "cached": bool(dl.get("cached"))})
                    continue

                raise ValueError("No local file, reference match, or source URL available")
            except Exception as exc:
                self._log(logs, "Download", "Failed", f"Failed to download {title}: {type(exc).__name__}: {exc}", 0)

        # Update cache flags.
        downloaded_ids = {item.get("id") for item in downloaded}
        updated: List[Dict[str, Any]] = []
        for record in self.last_records:
            if record.get("id") in downloaded_ids:
                match = next((item for item in downloaded if item.get("id") == record.get("id")), {})
                updated.append({**record, "downloaded": True, "cached": True, "filename": match.get("filename", record.get("filename"))})
            else:
                updated.append(record)
        self.last_records = updated
        self.last_log = [*self.last_log, *self._logs_to_dicts(logs)]
        return {"downloaded": downloaded, "logs": self._logs_to_dicts(logs)}

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def library(self) -> List[Dict[str, Any]]:
        folder = self.settings.downloaded_dir
        folder.mkdir(parents=True, exist_ok=True)
        files: List[Dict[str, Any]] = []
        for path in sorted(folder.glob("*.pdf")):
            files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
        return files


crawler_service = CrawlerService()
