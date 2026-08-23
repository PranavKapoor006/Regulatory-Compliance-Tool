from __future__ import annotations

"""
FSCA Directives Web Crawler
===========================

This module is intentionally self-contained and defensive. The current FSCA
Supervisory Information page embeds all directive rows in one Directives accordion,
so the production crawl uses one bounded HTML request instead of probing multiple
legacy SharePoint endpoints.

Design goals
------------
1. Read the public FSCA Directives accordion in one bounded request.
2. Enforce a hard request budget, refresh cooldown, host allow-list and single-flight
   lock. Never let filtering or page initialization trigger a live request.
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
The public FSCA page currently exposes all 55 file rows in static HTML. Older
SharePoint parsing helpers remain only for backward-compatible tests and are not used
by the production crawl path.
"""

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import zipfile
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

EXPECTED_CATEGORY_COUNTS: Dict[str, int] = {
    "Insurer / Micro Insurer": 40,
    "Joint FSCA / PA Directives": 2,
    "Retirement Fund": 8,
}

PUBLIC_DIRECTIVE_COUNT = 55

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
DEFAULT_DIRECTIVES_PAGE = "https://www.fsca.co.za/Supervisory-Information/?collapse=collapseEight"
PUBLIC_HOST = "https://www.fsca.co.za"
REGULATORY_FRAMEWORKS_SITE = "https://www2.fsca.co.za/Regulatory%20Frameworks"

# Candidate sites. The mentor screenshot showed an Enforcement-Matters subsite while
# the public URL is under Regulatory Frameworks. Include both and a few conservative
# variants so the crawler survives site restructuring.
CANDIDATE_SHAREPOINT_SITES: List[str] = [REGULATORY_FRAMEWORKS_SITE]

# Embedded by the FSCA Directives.aspx web part. This public document library
# currently contains the exact 55 rows shown by the three grouped categories.
DIRECTIVES_LIST_GUID = "1196F9B8-9C72-4A43-9397-C02988E27043"
# The Directives.aspx page and its embedded list belong to the Regulatory Frameworks
# SharePoint web. A list GUID is scoped to its owning web, so querying the same GUID
# under Enforcement-Matters returns no usable rows even though the GUID is correct.
DIRECTIVES_LIST_SITE = REGULATORY_FRAMEWORKS_SITE
EXPECTED_DIRECTIVE_COUNT = 50
CACHE_SCHEMA_VERSION = 3
CRAWLER_VERSION = "2026-08-23-demo.1"
CRAWLER_EXECUTION_BLOCKED = False
CRAWLER_DISABLED_MESSAGE = "The FSCA crawler is unavailable."
ALLOWED_NETWORK_HOSTS = {"www.fsca.co.za"}
BUNDLED_LIBRARY_ROOT = Path(__file__).resolve().parents[2] / "bundled_directives"
BUNDLED_MANIFEST_PATH = BUNDLED_LIBRARY_ROOT / "manifest.json"
BUNDLED_MANIFEST_SCHEMA_VERSION = 1
NETWORK_ACCESS_ENABLED = False

# Candidate list titles and server-relative list folders. SharePoint deployments often
# use a display title, while the internal root folder may differ.
CANDIDATE_LIST_TITLES: List[str] = [
    "Directives",
    "Regulatory Framework Documents",
    "Documents",
    "Regulatory Frameworks",
]

CANDIDATE_LIST_URLS: List[str] = ["/Regulatory Frameworks/Directives"]

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
        "LaunchDate",
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
DEFAULT_CACHE_SECONDS = max(900, int(os.getenv("FSCA_CRAWLER_CACHE_SECONDS", "21600")))
REFRESH_COOLDOWN_SECONDS = max(60, int(os.getenv("FSCA_CRAWLER_REFRESH_COOLDOWN_SECONDS", "300")))
CRAWL_TOTAL_TIMEOUT_SECONDS = min(45.0, max(20.0, float(os.getenv("FSCA_CRAWLER_TOTAL_TIMEOUT_SECONDS", "45"))))
CRAWL_REQUEST_TIMEOUT_SECONDS = min(35.0, max(10.0, float(os.getenv("FSCA_CRAWLER_REQUEST_TIMEOUT_SECONDS", "30"))))
MIN_REQUEST_INTERVAL_SECONDS = max(0.5, float(os.getenv("FSCA_CRAWLER_MIN_REQUEST_INTERVAL_SECONDS", "1.0")))
MAX_CRAWL_REQUESTS = 2
MAX_DOWNLOAD_BATCH = EXPECTED_DIRECTIVE_COUNT
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 75 * 1024 * 1024


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


def _valid_pdf_path(path: Path) -> bool:
    """Return True only for a readable file with a genuine PDF signature."""
    try:
        if not path.is_file() or path.stat().st_size < 200:
            return False
        with path.open("rb") as handle:
            return _content_is_pdf(handle.read(1024), "application/pdf")
    except OSError:
        return False


def _valid_bundled_path(path: Path, expected_sha256: str = "") -> bool:
    """Validate one immutable bundled document without assuming it is a PDF."""
    try:
        resolved_root = BUNDLED_LIBRARY_ROOT.resolve()
        resolved_path = path.resolve()
        if resolved_root not in resolved_path.parents:
            return False
        if not resolved_path.is_file() or resolved_path.stat().st_size < 200:
            return False
        with resolved_path.open("rb") as handle:
            signature = handle.read(1024)
        is_pdf = signature.lstrip().startswith(b"%PDF-")
        is_legacy_word = signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        if not (is_pdf or is_legacy_word):
            return False
        if expected_sha256:
            digest = hashlib.sha256()
            with resolved_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected_sha256.lower():
                return False
        return True
    except OSError:
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
        # Runtime discovery is deliberately offline. The official documents and
        # their checksummed catalog ship with the application.
        self.session = None
        self.last_records: List[Dict[str, Any]] = []
        self.last_log: List[Dict[str, Any]] = []
        self.last_category_counts: Dict[str, int] = {}
        self.last_crawl_time: float = 0.0
        self._crawl_deadline: Optional[float] = None
        self._operation_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_budget_remaining = 0
        self._last_network_request_at = 0.0
        self._load_bundled_library()

    @property
    def _manifest_path(self) -> Path:
        return BUNDLED_MANIFEST_PATH

    def _load_bundled_library(self) -> bool:
        """Load and integrity-check the immutable 50-PDF demo catalog."""
        try:
            payload = json.loads(BUNDLED_MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        if payload.get("schema_version") != BUNDLED_MANIFEST_SCHEMA_VERSION:
            return False
        if payload.get("network_access") is not False:
            return False
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            return False

        records: List[Dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, dict) or not raw.get("id"):
                return False
            relative_path = Path(str(raw.get("relative_path") or ""))
            local_path = BUNDLED_LIBRARY_ROOT / relative_path
            if not _valid_bundled_path(local_path, str(raw.get("sha256") or "")):
                return False
            record = dict(raw)
            record.update(
                {
                    "source_link": "",
                    "local_path": str(local_path),
                    "source_type": "Bundled official FSCA file",
                    "downloadable": True,
                    "cached": True,
                    "downloaded": True,
                    "status": "Available locally",
                    "warning": "",
                }
            )
            records.append(record)

        category_counts = {
            category: sum(
                1
                for record in records
                if record.get("category") == category
            )
            for category in FSCA_DIRECTIVE_CATEGORIES
        }
        if len(records) != EXPECTED_DIRECTIVE_COUNT:
            return False
        if category_counts != EXPECTED_CATEGORY_COUNTS:
            return False
        self.last_records = records
        self.last_category_counts = category_counts
        self.last_crawl_time = _now_timestamp()
        self.last_log = [
            CrawlLogEntry(
                "Offline bundle",
                "Completed",
                "Validated all 50 bundled official FSCA PDFs and their SHA-256 checksums.",
                len(records),
            ).to_dict(),
            CrawlLogEntry(
                "Network",
                "Disabled",
                "Topic selection and filtering use local files only; zero FSCA requests are permitted at runtime.",
                0,
            ).to_dict(),
        ]
        return True

    def _read_persistent_cache(self) -> Tuple[List[Dict[str, Any]], Dict[str, int], float]:
        """Return the validated bundled row set without touching runtime storage."""
        if not self.last_records and not self._load_bundled_library():
            return [], {}, 0.0
        return list(self.last_records), dict(self.last_category_counts), self.last_crawl_time

    def _load_persistent_cache(self) -> bool:
        return self._load_bundled_library()

    def _save_persistent_cache(
        self,
        records: List[Dict[str, Any]],
        category_counts: Dict[str, int],
    ) -> None:
        """Compatibility no-op: the signed bundle manifest is immutable at runtime."""
        return None

    def _cache_status(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        official = [
            record for record in records
            if record.get("section") in FSCA_DIRECTIVE_CATEGORIES
            or record.get("category") in FSCA_DIRECTIVE_CATEGORIES
        ]
        files_cached = sum(
            1
            for record in official
            if _valid_bundled_path(
                Path(str(record.get("local_path") or "")),
                str(record.get("sha256") or ""),
            )
        )
        category_status = self._category_status(records)
        return {
            "crawler_version": CRAWLER_VERSION,
            "expected": EXPECTED_DIRECTIVE_COUNT,
            "rows_cached": len(official),
            "files_cached": files_cached,
            "files_bundled": files_cached,
            "complete": all(item["complete"] for item in category_status.values()),
            "category_status": category_status,
            "manifest_path": str(self._manifest_path),
            "bundled_dir": str(BUNDLED_LIBRARY_ROOT),
            "network_access": False,
        }

    def _category_status(self, records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Measure each official category independently.

        A combined total can hide a missing category when a duplicate row is
        incorrectly assigned elsewhere. Category completeness is therefore the
        crawler's authoritative acceptance check.
        """
        status: Dict[str, Dict[str, Any]] = {}
        for category, expected in EXPECTED_CATEGORY_COUNTS.items():
            category_records = [
                record
                for record in records
                if record.get("section") == category or record.get("category") == category
            ]
            indexed = len(category_records)
            bundled = sum(
                1
                for record in category_records
                if _valid_bundled_path(
                    Path(str(record.get("local_path") or "")),
                    str(record.get("sha256") or ""),
                )
            )
            pdfs = sum(
                1
                for record in category_records
                if str(record.get("document_type") or "").lower() == "pdf"
            )
            status[category] = {
                "category": category,
                "expected": expected,
                "indexed": indexed,
                "files_bundled": bundled,
                "pdfs_bundled": pdfs,
                "pdfs_cached": pdfs,
                "complete": indexed == expected and bundled == expected,
            }
        return status

    def _complete_category_population(self, records: List[Dict[str, Any]]) -> bool:
        status = self._category_status(records)
        return all(item["complete"] for item in status.values())

    def _category_counts(self, records: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            category: int(item["indexed"])
            for category, item in self._category_status(records).items()
        }

    # ------------------------------------------------------------------
    # Session / logging
    # ------------------------------------------------------------------

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=2,
            status=0,
            allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36 "
                    "EY-Regulatory-Compliance-Tool/2026.07"
                ),
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "close",
                "Referer": DEFAULT_DIRECTIVES_PAGE,
            }
        )
        return session

    def _begin_request_budget(self, request_limit: int) -> None:
        self._request_budget_remaining = max(0, int(request_limit))

    def _source_url(self) -> str:
        configured = str(self.settings.fsca_directives_url or "").strip()
        parsed = urlparse(configured)
        if parsed.scheme == "https" and (parsed.hostname or "").lower() in ALLOWED_NETWORK_HOSTS:
            return configured
        return DEFAULT_DIRECTIVES_PAGE

    def _polite_request(
        self,
        method: str,
        url: str,
        *,
        max_attempts: int = 2,
        **kwargs: Any,
    ) -> requests.Response:
        """Reject every runtime network request.

        The complete official population is shipped in ``bundled_directives``.
        Keeping this fail-closed boundary means legacy helper code cannot contact
        FSCA even if it is called accidentally.
        """
        if not NETWORK_ACCESS_ENABLED:
            raise RuntimeError(
                "Runtime FSCA network access is disabled; use the bundled 50-PDF directive library."
            )
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_NETWORK_HOSTS:
            raise ValueError("Crawler network access is restricted to https://www.fsca.co.za.")

        attempts = max(1, min(2, int(max_attempts)))
        last_response: Optional[requests.Response] = None
        for attempt in range(attempts):
            with self._request_lock:
                if self._request_budget_remaining <= 0:
                    raise RuntimeError("Crawler request budget exhausted; no further FSCA requests were sent.")
                elapsed = time.monotonic() - self._last_network_request_at
                if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                    time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
                self._request_budget_remaining -= 1
                self._last_network_request_at = time.monotonic()

            response = self.session.request(method, url, verify=True, **kwargs)
            last_response = response
            if response.status_code not in {429, 503} or attempt + 1 >= attempts:
                return response

            retry_after = response.headers.get("Retry-After", "").strip()
            delay = min(10.0, 2.0 ** attempt)
            if retry_after:
                try:
                    delay = min(10.0, max(delay, float(retry_after)))
                except ValueError:
                    try:
                        retry_time = parsedate_to_datetime(retry_after).timestamp()
                        delay = min(10.0, max(delay, retry_time - time.time()))
                    except (TypeError, ValueError, OverflowError):
                        pass
            response.close()
            time.sleep(max(0.0, delay))

        if last_response is None:
            raise RuntimeError("FSCA request did not start.")
        return last_response

    def _bounded_response_bytes(self, response: requests.Response, maximum_bytes: int) -> bytes:
        content_length = _safe_int(response.headers.get("content-length"))
        if content_length and content_length > maximum_bytes:
            response.close()
            raise ValueError(f"FSCA response exceeds the {maximum_bytes // (1024 * 1024)} MB safety limit.")
        chunks: List[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > maximum_bytes:
                response.close()
                raise ValueError(f"FSCA response exceeds the {maximum_bytes // (1024 * 1024)} MB safety limit.")
            chunks.append(chunk)
        return b"".join(chunks)

    def _log(self, logs: List[CrawlLogEntry], stage: str, status: str, message: str, row_count: int = 0) -> None:
        logs.append(CrawlLogEntry(stage=stage, status=status, message=message, row_count=row_count))

    def _logs_to_dicts(self, logs: List[CrawlLogEntry]) -> List[Dict[str, Any]]:
        return [entry.to_dict() for entry in logs]

    def _network_timeout(self, maximum: float = CRAWL_REQUEST_TIMEOUT_SECONDS) -> Tuple[float, float]:
        """Bound each request and the complete crawl so a blocked FSCA site cannot hang the API."""
        read_timeout = max(0.5, min(maximum, CRAWL_REQUEST_TIMEOUT_SECONDS))
        if self._crawl_deadline is not None:
            remaining = self._crawl_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Live FSCA crawl exceeded the {CRAWL_TOTAL_TIMEOUT_SECONDS:g}-second safety limit"
                )
            read_timeout = max(0.5, min(read_timeout, remaining))
        return (read_timeout, read_timeout)

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

    def _sharepoint_get(self, url: str, params: Optional[Dict[str, str]] = None, timeout: int = 7) -> requests.Response:
        if CRAWLER_EXECUTION_BLOCKED or self.session is None:
            raise RuntimeError(CRAWLER_DISABLED_MESSAGE)
        headers = {"Accept": "application/json;odata=nometadata"}
        return self._polite_request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=self._network_timeout(float(timeout)),
            max_attempts=1,
        )

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
        base = f"{DIRECTIVES_LIST_SITE}/_api/web/lists(guid'{DIRECTIVES_LIST_GUID}')/items"
        # Start with the smallest useful payload. The FSCA server has been observed
        # closing connections when ``$select=*`` and an expanded File object are
        # requested together. FileRef/FileLeafRef are sufficient to construct the
        # public PDF URL for a document-library item.
        compact_select = ",".join(
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
            ]
        )
        endpoints: List[Tuple[str, Dict[str, str], str]] = [
            (
                base,
                {
                    "$top": str(PUBLIC_DIRECTIVE_COUNT),
                    "$select": compact_select,
                },
                "FSCA Directives web-part list",
            ),
            # Some older SharePoint list schemas reject a select clause when one
            # optional internal column is absent. Retrying the same authoritative
            # list without a select is safer and still much smaller than File expand.
            (
                base,
                {"$top": str(PUBLIC_DIRECTIVE_COUNT)},
                "FSCA Directives web-part list (schema-neutral)",
            ),
        ]
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
                    # This GUID endpoint is the authoritative list embedded by the
                    # public Directives page. Avoid slower duplicate probes once it works.
                    if len(_dedupe_records(all_records)) >= PUBLIC_DIRECTIVE_COUNT:
                        break
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
                    response = self._polite_request(
                        "POST",
                        endpoint,
                        data=json.dumps(body),
                        headers=headers,
                        timeout=self._network_timeout(45),
                        max_attempts=1,
                    )
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
        url = self._source_url()
        try:
            response = self._polite_request(
                "GET",
                url,
                timeout=self._network_timeout(CRAWL_REQUEST_TIMEOUT_SECONDS),
                stream=True,
                max_attempts=2,
                headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
            )
            response.raise_for_status()
            html_bytes = self._bounded_response_bytes(response, MAX_HTML_BYTES)
            soup = BeautifulSoup(html_bytes, "html.parser")
            directives_root = soup.select_one("#collapseEight")
            if directives_root is None:
                raise ValueError("The FSCA Directives section (#collapseEight) was not found.")
            declared_counts = _category_counts_from_static_html(
                html_bytes.decode("utf-8", errors="ignore")
            )

            category_panels = {
                "Insurer / Micro Insurer": "subsubCollapseDRC",
                "Joint FSCA / PA Directives": "subsubCollapseDRC1",
                "Retirement Fund": "subsubCollapseDRC2",
            }
            for category, panel_id in category_panels.items():
                panel = directives_root.select_one(f"#{panel_id}")
                if panel is None:
                    self._log(logs, "Parse", "Warning", f"FSCA category panel was not found: {category}.", 0)
                    category_counts[category] = 0
                    continue

                category_records: List[DirectiveRecord] = []
                for row in panel.select("tr[onclick]"):
                    onclick = _strip(row.get("onclick"))
                    link_match = re.search(r"window\.open\(\s*['\"]([^'\"]+)['\"]", onclick, flags=re.I)
                    cell = row.find("td")
                    title = _strip(cell.get_text(" ", strip=True) if cell else "")
                    if not link_match or not title:
                        continue
                    source_link = urljoin(PUBLIC_HOST, html.unescape(link_match.group(1)))
                    parsed_link = urlparse(source_link)
                    if parsed_link.scheme != "https" or (parsed_link.hostname or "").lower() not in ALLOWED_NETWORK_HOSTS:
                        continue
                    filename = f"{safe_filename(title)}.pdf"
                    category_records.append(
                        self._make_record(
                            title=title,
                            source_link=source_link,
                            filename=filename,
                            section=category,
                            category=category,
                            year=_extract_launch_year({}, title),
                            description="Official FSCA directive listed in the Supervisory Information Directives section.",
                            document_no=_extract_directive_no(title),
                            source_type="FSCA Supervisory Information",
                            downloadable=True,
                            status="Ready",
                            source_priority=100,
                        )
                    )
                category_records = _dedupe_records(category_records)
                category_counts[category] = len(category_records)
                declared = declared_counts.get(category)
                if declared is not None and declared != len(category_records):
                    self._log(
                        logs,
                        "Validate",
                        "Warning",
                        (
                            f"{category} advertises {declared} directive(s), but "
                            f"{len(category_records)} unique row(s) were parsed."
                        ),
                        len(category_records),
                    )
                records.extend(category_records)
                self._log(logs, "Parse", "Completed", f"Parsed {category}.", len(category_records))

            records = _dedupe_records(records)
            self._log(
                logs,
                "Crawl",
                "Completed",
                "Fetched and parsed the official FSCA Directives section in one bounded request.",
                len(records),
            )
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
            if not _valid_pdf_path(path):
                self._log(logs, "Reference", "Warning", f"Ignored invalid reference PDF: {path.name}", 0)
                continue
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
            if not _valid_pdf_path(path):
                self._log(logs, "Cache", "Warning", f"Ignored invalid downloaded PDF: {path.name}", 0)
                continue
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
        if CRAWLER_EXECUTION_BLOCKED:
            raise RuntimeError(CRAWLER_DISABLED_MESSAGE)
        # A persisted row manifest is the fast path after every backend restart.
        # Explicit refresh still performs a live crawl, but can retain these rows
        # if the public SharePoint site temporarily under-returns.
        if not self.last_records:
            self._load_persistent_cache()
        previous_records = list(self.last_records)
        previous_counts = dict(self.last_category_counts)
        cache_age = _now_timestamp() - self.last_crawl_time if self.last_crawl_time else float("inf")
        if force and self.last_records and cache_age < REFRESH_COOLDOWN_SECONDS:
            logs = list(self.last_log)
            logs.append(
                CrawlLogEntry(
                    "Safety",
                    "Completed",
                    f"Live refresh suppressed by the {REFRESH_COOLDOWN_SECONDS}-second cooldown; cached rows were returned and no FSCA request was sent.",
                    len(self.last_records),
                ).to_dict()
            )
            return {
                "records": self.last_records,
                "logs": logs,
                "category_counts": self.last_category_counts,
                "from_cache": True,
                "refresh_suppressed": True,
                "cache_status": self._cache_status(self.last_records),
            }
        if not force and self.last_records and (_now_timestamp() - self.last_crawl_time) < self.cache_seconds:
            return {
                "records": self.last_records,
                "logs": self.last_log,
                "category_counts": self.last_category_counts,
                "from_cache": True,
                "cache_status": self._cache_status(self.last_records),
            }

        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("A crawler operation is already running. Wait for it to finish instead of sending another request.")
        try:
            logs: List[CrawlLogEntry] = []
            self._crawl_deadline = time.monotonic() + CRAWL_TOTAL_TIMEOUT_SECONDS
            self._begin_request_budget(MAX_CRAWL_REQUESTS)
            self._log(logs, "Configure", "Completed", f"Using FSCA Directives source: {self._source_url()}", 0)
            self._log(
                logs,
                "Safety",
                "Completed",
                f"One crawl at a time; one normal request, at most {MAX_CRAWL_REQUESTS} with temporary-error retry; {REFRESH_COOLDOWN_SECONDS}-second refresh cooldown.",
                0,
            )

            live_records, category_counts = self._crawl_public_html(logs)
            live_records = _dedupe_records(live_records)
            live_population_complete = self._complete_category_population(
                [record.to_dict() for record in live_records]
            )
            if live_population_complete:
                self._log(
                    logs,
                    "Cache",
                    "Completed",
                    (
                        "Official FSCA page returned the complete category population: "
                        "40 Insurer / Micro Insurer, 2 Joint FSCA / PA Directives, "
                        "and 13 Retirement Fund."
                    ),
                    len(live_records),
                )
            elif live_records:
                self._log(
                    logs,
                    "Results",
                    "Warning",
                    (
                        f"FSCA returned {len(live_records)} rows, but one or more "
                        "category populations were incomplete; a prior complete cache "
                        "will be retained when available."
                    ),
                    len(live_records),
                )

            if live_records:
                self._log(logs, "Results", "Completed", "Live crawler produced directive record(s).", len(live_records))
            else:
                self._log(logs, "Results", "Warning", "Live FSCA page did not produce directive records; cached/local records are preserved.", 0)

        # 5. Always merge reference/downloaded records so Download Selected works in demos.
            reference_records = self._reference_directives(logs)
            downloaded_records = self._already_downloaded_records(logs)
            persistent_records: List[DirectiveRecord] = []
            if previous_records and not live_population_complete:
                for raw in previous_records:
                    try:
                        persistent_records.append(self._make_record(**raw, source_priority=80))
                    except Exception:
                        continue
                if persistent_records:
                    self._log(
                        logs,
                        "Cache",
                        "Completed",
                        "Retained persistent FSCA rows while the live source was incomplete.",
                        len(persistent_records),
                    )
            all_records = _dedupe_records([
                *live_records,
                *persistent_records,
                *reference_records,
                *downloaded_records,
            ])

        # Keep only real three categories where known. Unknown records remain visible only if
        # they are clearly direct files, but filter dropdown remains the three categories.
            good_records: List[DirectiveRecord] = []
            for record in all_records:
                if record.section == "Unknown":
                    record.warning = record.warning or "Category could not be confidently determined."
                good_records.append(record)

            if not good_records:
                self._log(logs, "Results", "Failed", "No directives were found from live FSCA, downloaded library, or reference files.", 0)
                self.last_log = self._logs_to_dicts(logs)
                raise RuntimeError(
                    "The FSCA page did not return directive rows and no prior cache was available. "
                    "No repeated requests were sent; try again after the cooldown or upload the PDF directly."
                )
            else:
                self._log(logs, "Results", "Completed", "Crawler result set ready for filtering and download.", len(good_records))

            candidate_records = [record.to_dict() for record in good_records]
            if (
                previous_records
                and self._complete_category_population(previous_records)
                and not self._complete_category_population(candidate_records)
            ):
                self._log(
                    logs,
                    "Safety",
                    "Completed",
                    "Rejected an incomplete live category population and retained the last complete cache.",
                    len(previous_records),
                )
                candidate_records = previous_records

            self.last_records = candidate_records
            self.last_log = self._logs_to_dicts(logs)
            self.last_category_counts = self._category_counts(self.last_records)
            self.last_crawl_time = _now_timestamp()
            self._save_persistent_cache(self.last_records, self.last_category_counts)

            return {
                "records": self.last_records,
                "logs": self.last_log,
                "category_counts": self.last_category_counts,
                "from_cache": False,
                "refresh_suppressed": False,
                "cache_status": self._cache_status(self.last_records),
            }
        finally:
            self._crawl_deadline = None
            self._operation_lock.release()

    def crawl(self) -> Dict[str, Any]:
        return self.search(section="All", year="All")

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
        cached_only: bool = False,
    ) -> Dict[str, Any]:
        if section and section != "All" and section not in FSCA_DIRECTIVE_CATEGORIES:
            raise ValueError(
                "Choose one of the official FSCA directive categories: "
                + ", ".join(FSCA_DIRECTIVE_CATEGORIES)
                + "."
            )
        if not self.last_records and not self._load_bundled_library():
            raise RuntimeError(
                "The bundled FSCA directive manifest is missing, incomplete, or failed integrity validation."
            )
        base_records = list(self.last_records)
        filtered = self._filter_records(base_records, section, year)
        logs = list(self.last_log)
        logs.append(
            CrawlLogEntry(
                "Local topic selection",
                "Completed",
                (
                    f"Returned the complete bundled topic population for "
                    f"section={section or 'All'}; year={year or 'All'}; "
                    "FSCA requests sent=0."
                ),
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
        category_status = self._category_status(base_records)
        selected_status = (
            category_status.get(str(section))
            if section and section != "All"
            else None
        )
        return {
            "records": filtered,
            "logs": logs,
            "category_counts": self.last_category_counts,
            "category_status": category_status,
            "selected_category_status": selected_status,
            "complete": (
                bool(selected_status and selected_status["complete"])
                if selected_status
                else all(item["complete"] for item in category_status.values())
            ),
            "available_years": self._available_years(base_records, section),
            "kpis": self._kpis_for_records(filtered),
            "cache_status": self._cache_status(base_records),
            "from_cache": True,
            "from_bundle": True,
            "refresh_suppressed": bool(force_refresh),
            "network_requests": 0,
        }

    def metadata(self) -> Dict[str, Any]:
        if not self.last_records:
            self._load_bundled_library()
        records = list(self.last_records)
        return {
            "sections": ["All"] + FSCA_DIRECTIVE_CATEGORIES,
            "years": self._available_years(records),
            "source_url": "",
            "category_counts": self.last_category_counts,
            "category_status": self._category_status(records),
            "expected_category_counts": EXPECTED_CATEGORY_COUNTS,
            "source_state": "bundled",
            "mode": "fully-local",
            "network_access": False,
            "cache_status": self._cache_status(records),
            "safety": {
                "single_flight": True,
                "normal_requests_per_crawl": 0,
                "maximum_requests_per_crawl": 0,
                "refresh_cooldown_seconds": 0,
                "cache_seconds": 0,
                "minimum_request_interval_seconds": 0,
                "maximum_download_batch": MAX_DOWNLOAD_BATCH,
                "automatic_bulk_download": False,
                "topic_selection_network_requests": 0,
                "runtime_network_disabled": True,
            },
        }

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------

    def _find_record(self, directive_id: str) -> Optional[Dict[str, Any]]:
        if not self.last_records:
            self._load_bundled_library()
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
            if not _valid_pdf_path(path):
                continue
            candidate_no = _extract_directive_no(path.name)
            candidate_normalized = candidate_no.lower().replace(" ", "").split("(", 1)[0]
            if candidate_no and candidate_normalized == normalized_no:
                return path
        return None

    def _copy_pdf_to_library(self, source: Path, desired_filename: str, logs: List[CrawlLogEntry], message_prefix: str) -> Dict[str, Any]:
        if not _valid_pdf_path(source):
            raise ValueError(f"Source is not a valid PDF: {source.name}")
        filename = safe_filename(desired_filename or source.name)
        if not filename.lower().endswith(".pdf"):
            filename = f"{Path(filename).stem}.pdf"
        target = self.settings.downloaded_dir / filename
        if _valid_pdf_path(target):
            self._log(logs, "Download", "Cached", f"Already available: {target.name}", 1)
        else:
            target = unique_path(self.settings.downloaded_dir, filename)
            shutil.copy2(source, target)
            self._log(logs, "Download", "Completed", f"{message_prefix}: {target.name}", 1)
        return {"filename": target.name, "path": str(target), "size_bytes": target.stat().st_size, "cached": True}

    def _download_url_to_library(self, url: str, filename: str, logs: List[CrawlLogEntry]) -> Dict[str, Any]:
        if CRAWLER_EXECUTION_BLOCKED or self.session is None:
            raise RuntimeError(CRAWLER_DISABLED_MESSAGE)
        if not url:
            raise ValueError("No source URL")
        response = self._polite_request(
            "GET",
            url,
            timeout=(5, 30),
            stream=True,
            max_attempts=2,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        content = self._bounded_response_bytes(response, MAX_PDF_BYTES)
        if len(content) < 200:
            raise ValueError("Downloaded content is too small to be a directive PDF")
        if not _content_is_pdf(content, content_type):
            raise ValueError(f"Downloaded content is not a PDF. Content-Type={content_type or 'unknown'}")
        filename = safe_filename(filename or _url_path_filename(url) or "directive.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{Path(filename).stem}.pdf"
        target = unique_path(self.settings.downloaded_dir, filename)
        target.write_bytes(content)
        self._log(logs, "Download", "Completed", f"Downloaded {target.name}", 1)
        return {"filename": target.name, "path": str(target), "size_bytes": target.stat().st_size, "cached": False}

    def _download_record(self, record: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[CrawlLogEntry]]:
        logs: List[CrawlLogEntry] = []
        directive_id = str(record.get("id") or "unknown")
        title = str(record.get("title") or record.get("filename") or directive_id)
        filename = str(record.get("filename") or f"{directive_id}.pdf")
        local_path = str(record.get("local_path") or "")

        try:
            if local_path and _valid_pdf_path(Path(local_path)):
                copied = self._copy_pdf_to_library(Path(local_path), filename, logs, "Copied directive into crawler library")
                return {**record, **copied, "downloaded": True, "cached": True}, logs

            cached_path = self.settings.downloaded_dir / safe_filename(filename)
            if _valid_pdf_path(cached_path):
                self._log(logs, "Download", "Cached", f"Already available: {cached_path.name}", 1)
                return {
                    **record,
                    "filename": cached_path.name,
                    "path": str(cached_path),
                    "downloaded": True,
                    "cached": True,
                }, logs

            reference = self._reference_match(record)
            if reference:
                copied = self._copy_pdf_to_library(reference, filename or reference.name, logs, "Copied matching reference directive")
                return {**record, **copied, "downloaded": True, "cached": True}, logs

            source_link = str(record.get("source_link") or "")
            if source_link:
                dl = self._download_url_to_library(source_link, filename, logs)
                return {**record, **dl, "downloaded": True, "cached": bool(dl.get("cached"))}, logs

            raise ValueError("No local file, reference match, or source URL available")
        except Exception as exc:
            self._log(logs, "Download", "Failed", f"Failed to download {title}: {type(exc).__name__}: {exc}", 0)
            return None, logs

    def download_selected(self, directive_ids: List[str]) -> Dict[str, Any]:
        if not self.last_records:
            self._load_bundled_library()
        unique_ids = list(dict.fromkeys(str(item) for item in directive_ids if item))
        if len(unique_ids) > MAX_DOWNLOAD_BATCH:
            raise ValueError(
                f"Select at most {MAX_DOWNLOAD_BATCH} bundled directives per action."
            )
        logs: List[CrawlLogEntry] = []
        downloaded: List[Dict[str, Any]] = []
        for directive_id in unique_ids:
            record = self._find_record(directive_id)
            if not record:
                self._log(logs, "Local bundle", "Failed", f"Unknown directive id: {directive_id}", 0)
                continue
            path = Path(str(record.get("local_path") or ""))
            if not _valid_bundled_path(path, str(record.get("sha256") or "")):
                self._log(logs, "Local bundle", "Failed", f"Integrity check failed: {record.get('filename')}", 0)
                continue
            downloaded.append({**record, "path": str(path)})
        self._log(
            logs,
            "Local bundle",
            "Completed",
            f"Resolved {len(downloaded)} selected file(s) locally; FSCA requests sent=0.",
            len(downloaded),
        )
        return {
            "downloaded": downloaded,
            "failed": len(unique_ids) - len(downloaded),
            "logs": self._logs_to_dicts(logs),
            "cache_status": self._cache_status(self.last_records),
            "network_requests": 0,
        }

    def export_selected(self, directive_ids: List[str]) -> Tuple[Path, Dict[str, Any]]:
        result = self.download_selected(directive_ids)
        downloaded = result.get("downloaded") or []
        valid_paths: List[Path] = []
        for item in downloaded:
            path = Path(str(item.get("path") or ""))
            if _valid_bundled_path(path, str(item.get("sha256") or "")):
                valid_paths.append(path)
        if not valid_paths:
            raise ValueError("None of the selected bundled directives passed integrity validation.")
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive = unique_path(self.settings.output_dir, f"FSCA_directives_{timestamp}.zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            used_names: set[str] = set()
            for path in valid_paths:
                name = path.name
                if name in used_names:
                    name = f"{path.stem}_{_stable_id(str(path))}{path.suffix}"
                used_names.add(name)
                bundle.write(path, arcname=name)
        return archive, result

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def library(self) -> List[Dict[str, Any]]:
        if not self.last_records:
            self._load_bundled_library()
        files: List[Dict[str, Any]] = []
        for record in self.last_records:
            path = Path(str(record.get("local_path") or ""))
            if not _valid_pdf_path(path):
                continue
            files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "modified": str(record.get("source_modified") or ""),
                    "category": str(record.get("category") or ""),
                    "title": str(record.get("title") or path.stem),
                    "bundled": True,
                }
            )
        return sorted(files, key=lambda item: (item["category"], item["title"].casefold()))

    def resolve_bundled_pdf(self, filename: str) -> Optional[Path]:
        """Resolve a dropdown filename to a checksummed local PDF."""
        if Path(filename).name != filename:
            return None
        if not self.last_records:
            self._load_bundled_library()
        for record in self.last_records:
            if str(record.get("filename") or "") != filename:
                continue
            path = Path(str(record.get("local_path") or ""))
            if _valid_pdf_path(path) and _valid_bundled_path(
                path,
                str(record.get("sha256") or ""),
            ):
                return path
        return None


crawler_service = CrawlerService()
