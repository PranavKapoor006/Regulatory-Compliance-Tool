from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.services.storage import safe_filename, unique_path


FSCA_DIRECTIVE_CATEGORIES = [
    "Insurer / Micro Insurer",
    "Joint FSCA / PA Directives",
    "Retirement Fund",
]

# Public SharePoint library discovered from the FSCA regulatory framework page.
REGULATORY_FRAMEWORK_LIST_GUID = "AF181750-83A5-4CA5-8D52-B9A6F1B0608C"
REGULATORY_FRAMEWORK_SITE = "https://www2.fsca.co.za/Regulatory%20Frameworks"


class CrawlerService:
    """Crawler for FSCA Directives.

    The FSCA page is SharePoint-backed. In a normal browser, SharePoint expands grouped
    rows with JavaScript. A simple HTML request often only returns group headings.
    This service therefore uses a layered strategy:

    1. Try SharePoint REST against the Regulatory Framework Documents list.
    2. Fall back to parsing public HTML links if SharePoint REST is blocked.
    3. Fall back to bundled reference directives so the utility remains demo-ready
       even when the public site returns only grouped rows in the current network.

    The fallback is clearly recorded in Crawl Log and can be replaced with live rows
    as soon as the SharePoint endpoint exposes downloadable documents.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 FSCA Regulatory Compliance Tool"
                ),
                "Accept": "application/json;odata=nometadata, text/html;q=0.9, */*;q=0.8",
            }
        )
        self.last_records: List[Dict[str, Any]] = []
        self.last_log: List[Dict[str, Any]] = []

    def _id_for(self, value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]

    def _log(self, logs: List[Dict[str, Any]], stage: str, status: str, message: str, row_count: int = 0) -> None:
        logs.append({"stage": stage, "status": status, "message": message, "row_count": row_count})

    def _year_from_text(self, text: str) -> str:
        match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
        return match.group(1) if match else "Unknown"

    def _directive_no_from_text(self, text: str) -> str:
        match = re.search(r"Directive\s+([0-9A-Za-z.]+(?:\s*\([^)]+\))?)", text, flags=re.I)
        if match:
            return match.group(1).replace(" ", "")
        match = re.search(r"\b(\d{2,3}\.[A-Z]\.[A-Za-z])\b", text)
        return match.group(1) if match else ""

    def _section_from_text(self, text: str) -> str:
        lowered = text.lower()
        if "retirement" in lowered or "pension" in lowered:
            return "Retirement Fund"
        if "joint" in lowered or "prudential authority" in lowered or " pa " in f" {lowered} ":
            return "Joint FSCA / PA Directives"
        if "long-term" in lowered or "short-term" in lowered or "insur" in lowered or "ltst" in lowered:
            return "Insurer / Micro Insurer"
        return "Directives"

    def _record(
        self,
        *,
        title: str,
        source_link: str,
        filename: str,
        section: str,
        year: str,
        category: str | None = None,
        description: str = "",
        local_path: str | None = None,
        source_type: str = "Live FSCA",
    ) -> Dict[str, Any]:
        safe_name = safe_filename(filename or f"{title}.pdf")
        cached_path = self.settings.downloaded_dir / safe_name
        key = local_path or source_link or title
        return {
            "id": self._id_for(key),
            "title": title.strip() or safe_name,
            "section": section or "Directives",
            "category": category or section or "Directives",
            "year": year or "Unknown",
            "source_link": source_link,
            "filename": safe_name,
            "cached": cached_path.exists(),
            "downloaded": cached_path.exists(),
            "description": description,
            "local_path": local_path,
            "source_type": source_type,
        }

    def _normalise_sharepoint_items(self, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for item in items:
            values = {str(k): v for k, v in item.items()}
            title = str(values.get("Title") or values.get("FileLeafRef") or values.get("Name") or "").strip()
            file_ref = str(values.get("FileRef") or values.get("FileDirRef") or "").strip()
            file_leaf = str(values.get("FileLeafRef") or Path(file_ref).name or "").strip()
            category = str(values.get("Category1") or values.get("Category") or values.get("Category0") or "").strip()
            year = str(values.get("Year") or values.get("_x0059_ear") or "").strip()
            doc_no = str(values.get("Document_x0020_no") or values.get("DocumentNo") or "").strip()
            doc_type = str(values.get("Document_x0020_Type") or values.get("Document Type") or "").strip()
            haystack = " ".join([title, file_ref, file_leaf, category, doc_no, doc_type])
            lowered = haystack.lower()

            # The Directives page groups documents into these Category1 values. Some
            # SharePoint rows do not expose the category cleanly, so filename/title
            # directive hints are also accepted.
            has_directive_hint = "directive" in lowered or "directives" in lowered
            has_directive_category = category in FSCA_DIRECTIVE_CATEGORIES
            if not (has_directive_hint or has_directive_category):
                continue
            if not file_ref and not file_leaf:
                continue

            if file_ref.startswith("http"):
                source_link = file_ref
            else:
                source_link = urljoin("https://www2.fsca.co.za", quote(file_ref, safe="/:()%&?=.,-_'"))
            filename = safe_filename(file_leaf or Path(urlparse(source_link).path).name or f"{self._id_for(source_link)}.pdf")
            section = category if category in FSCA_DIRECTIVE_CATEGORIES else self._section_from_text(haystack)
            records.append(
                self._record(
                    title=title or filename,
                    source_link=source_link,
                    filename=filename,
                    section=section,
                    category=category or section,
                    year=year or self._year_from_text(haystack),
                    description=doc_no or doc_type,
                    source_type="SharePoint REST",
                )
            )
        return self._dedupe(records)

    def _crawl_sharepoint_rest(self, logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        endpoints = [
            (
                f"{REGULATORY_FRAMEWORK_SITE}/_api/web/lists(guid'{REGULATORY_FRAMEWORK_LIST_GUID}')/items",
                {
                    "$select": (
                        "Id,Title,FileRef,FileLeafRef,Category,Category1,Year,"
                        "Document_x0020_no,Document_x0020_Type,Modified,Created"
                    ),
                    "$top": "5000",
                },
            ),
            (
                f"{REGULATORY_FRAMEWORK_SITE}/_api/web/lists/getbytitle('Regulatory Framework Documents')/items",
                {
                    "$select": (
                        "Id,Title,FileRef,FileLeafRef,Category,Category1,Year,"
                        "Document_x0020_no,Document_x0020_Type,Modified,Created"
                    ),
                    "$top": "5000",
                },
            ),
        ]
        for endpoint, params in endpoints:
            try:
                response = self.session.get(endpoint, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
                items = payload.get("value") or payload.get("d", {}).get("results") or []
                records = self._normalise_sharepoint_items(items)
                self._log(logs, "Crawl", "Completed", f"Queried SharePoint list endpoint: {endpoint}", len(items))
                if records:
                    self._log(logs, "Results", "Completed", "Normalised directive records from SharePoint metadata.", len(records))
                    return records
                self._log(logs, "Results", "Warning", "SharePoint returned rows, but no directive file links matched filters.", len(items))
            except Exception as exc:
                self._log(logs, "Crawl", "Warning", f"SharePoint REST endpoint unavailable: {type(exc).__name__}: {exc}", 0)
        return []

    def _crawl_public_html(self, logs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        records: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}
        url = self.settings.fsca_directives_url
        try:
            response = self.session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text("\n", strip=True)
            self._log(logs, "Crawl", "Completed", "Fetched FSCA Directives public page.", 1)

            for match in re.finditer(r"Category1\s*:\s*(.*?)\s*[\u200e\u200f]*\((\d+)\)", page_text):
                category_counts[match.group(1).strip()] = int(match.group(2))

            for anchor in soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True)
                href = anchor.get("href", "")
                full_url = urljoin(url, href)
                haystack = f"{label} {href}"
                if not re.search(r"directive|\.pdf|LTST|FSCA|FSB", haystack, re.I):
                    continue
                if href.lower().startswith("javascript") or "{item" in href.lower():
                    continue
                filename = safe_filename(Path(urlparse(full_url).path).name or f"{label}.pdf")
                if not filename or filename == "file":
                    continue
                section = self._section_from_text(haystack)
                records.append(
                    self._record(
                        title=label if len(label) > 5 else filename,
                        source_link=full_url,
                        filename=filename,
                        section=section,
                        year=self._year_from_text(haystack),
                        category=section,
                        source_type="Public HTML",
                    )
                )
            records = self._dedupe(records)
            if records:
                self._log(logs, "Results", "Completed", "Parsed downloadable directive links from public HTML.", len(records))
            elif category_counts:
                self._log(
                    logs,
                    "Results",
                    "Warning",
                    "Public HTML exposed category groups but not file-level links; using reference directives for demo continuity.",
                    sum(category_counts.values()),
                )
            return records, category_counts
        except Exception as exc:
            self._log(logs, "Crawl", "Warning", f"Public FSCA page unavailable: {type(exc).__name__}: {exc}", 0)
            return [], category_counts

    def _reference_directives(self, logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        reference_dir = self.settings.reference_directives_root
        pdfs = sorted(reference_dir.glob("*.pdf"))
        records: List[Dict[str, Any]] = []
        known_titles = {
            "101": "Directive 101.A.i (LT&ST) - Directors and Managing Executive",
            "159": "Directive 159.A.i (LT&ST) - Outsourcing",
        }
        for path in pdfs:
            name = path.name
            directive_no = self._directive_no_from_text(name) or self._directive_no_from_text(path.stem)
            title = next((known for key, known in known_titles.items() if key in name), path.stem)
            text_for_classification = f"{title} {name} {directive_no}"
            records.append(
                self._record(
                    title=title,
                    source_link=self.settings.fsca_directives_url,
                    filename=name,
                    section=self._section_from_text(text_for_classification),
                    category=self._section_from_text(text_for_classification),
                    year=self._year_from_text(text_for_classification),
                    description="Reference directive available for offline/demo crawl workflow.",
                    local_path=str(path),
                    source_type="Reference file",
                )
            )
        if records:
            self._log(
                logs,
                "Results",
                "Reference",
                "Loaded bundled reference directives because live FSCA did not expose downloadable file links in this environment.",
                len(records),
            )
        return records

    def _dedupe(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        unique: Dict[str, Dict[str, Any]] = {}
        for record in records:
            key = record.get("source_link") or record.get("local_path") or record.get("filename") or record["id"]
            unique[key] = record
        return list(unique.values())

    def crawl(self) -> Dict[str, Any]:
        logs: List[Dict[str, Any]] = []
        self._log(logs, "Configure", "Completed", f"Using FSCA Directives URL: {self.settings.fsca_directives_url}", 0)

        records = self._crawl_sharepoint_rest(logs)
        category_counts: Dict[str, int] = {}
        if not records:
            html_records, category_counts = self._crawl_public_html(logs)
            records = html_records
        if not records:
            records = self._reference_directives(logs)
        if not records:
            self._log(logs, "Results", "Failed", "No directives were found from live FSCA or local reference files.", 0)

        self.last_records = records
        self.last_log = logs
        return {"records": records, "logs": logs, "category_counts": category_counts}

    def search(self, section: str | None = None, year: str | None = None) -> Dict[str, Any]:
        # Always refresh on explicit crawl to pick up current website changes.
        crawl_result = self.crawl()
        records = crawl_result["records"]
        if section and section != "All":
            records = [r for r in records if r.get("section") == section or r.get("category") == section]
        if year and year != "All":
            records = [r for r in records if r.get("year") == year]

        logs = list(self.last_log)
        self._log(logs, "Filter", "Completed", f"Applied filters: section={section or 'All'}, year={year or 'All'}", len(records))
        return {"records": records, "logs": logs, "category_counts": crawl_result.get("category_counts", {})}

    def metadata(self) -> Dict[str, Any]:
        if not self.last_records:
            self.crawl()
        sections = sorted({r.get("section", "Directives") for r in self.last_records if r.get("section")})
        years = sorted({r.get("year", "Unknown") for r in self.last_records if r.get("year")}, reverse=True)
        return {
            "sections": ["All"] + sections,
            "years": ["All"] + years,
            "source_url": self.settings.fsca_directives_url,
        }

    def _copy_reference_file(self, record: Dict[str, Any], logs: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        local_path = record.get("local_path")
        if not local_path:
            return None
        source = Path(local_path)
        if not source.exists():
            self._log(logs, "Download", "Failed", f"Reference file is missing: {source.name}", 0)
            return None
        filename = safe_filename(record.get("filename") or source.name)
        target = self.settings.downloaded_dir / filename
        if target.exists():
            self._log(logs, "Download", "Cached", f"Already available: {target.name}", 1)
        else:
            target = unique_path(self.settings.downloaded_dir, filename)
            shutil.copy2(source, target)
            self._log(logs, "Download", "Completed", f"Copied reference directive into crawler library: {target.name}", 1)
        return {**record, "filename": target.name, "cached": True, "downloaded": True}

    def download_selected(self, directive_ids: List[str]) -> Dict[str, Any]:
        if not self.last_records:
            self.crawl()
        records_by_id = {record["id"]: record for record in self.last_records}
        downloaded: List[Dict[str, Any]] = []
        logs: List[Dict[str, Any]] = []

        for directive_id in directive_ids:
            record = records_by_id.get(directive_id)
            if not record:
                self._log(logs, "Download", "Failed", f"Unknown directive id: {directive_id}", 0)
                continue

            copied = self._copy_reference_file(record, logs)
            if copied:
                downloaded.append(copied)
                continue

            filename = safe_filename(record.get("filename") or f"{record['id']}.pdf")
            cached_path = self.settings.downloaded_dir / filename
            if cached_path.exists():
                downloaded.append({**record, "filename": cached_path.name, "cached": True, "downloaded": True})
                self._log(logs, "Download", "Cached", f"Already available: {cached_path.name}", 1)
                continue

            try:
                response = self.session.get(record["source_link"], timeout=60)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if len(response.content) < 100:
                    raise ValueError("Downloaded content is too small")
                if "pdf" not in content_type and not filename.lower().endswith(".pdf"):
                    # Preserve the file, but rename to PDF only if the URL says PDF.
                    raise ValueError(f"Downloaded content is not a PDF document: {content_type or 'unknown content type'}")
                target = unique_path(self.settings.downloaded_dir, filename)
                target.write_bytes(response.content)
                downloaded.append({**record, "filename": target.name, "cached": False, "downloaded": True})
                self._log(logs, "Download", "Completed", f"Downloaded {target.name}", 1)
            except Exception as exc:
                self._log(logs, "Download", "Failed", f"Failed to download {record['title']}: {type(exc).__name__}: {exc}", 0)

        downloaded_ids = {item["id"] for item in downloaded}
        self.last_records = [
            {**record, "downloaded": True, "cached": True} if record["id"] in downloaded_ids else record
            for record in self.last_records
        ]
        return {"downloaded": downloaded, "logs": logs}

    def library(self) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        for path in sorted(self.settings.downloaded_dir.glob("*.pdf")):
            files.append({"name": path.name, "path": str(path), "size_bytes": path.stat().st_size})
        return files


crawler_service = CrawlerService()
