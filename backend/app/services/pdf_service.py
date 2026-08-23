from __future__ import annotations

import io
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import List, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

# Self-contained PDF/OCR extraction service.
# It force-loads backend/.env and auto-detects Tesseract on Windows.
BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BACKEND_DIR / ".env"
OCR_CACHE_VERSION = "v3"
if load_dotenv:
    load_dotenv(ENV_PATH)


def _env_value(name: str, default: str = "") -> str:
    return str(os.getenv(name, default)).strip().strip('"').strip("'")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_value(name, str(default)))
    except Exception:
        return default


def _clean_extracted_text(text: str) -> str:
    text = text or ""
    text = text.replace("\x0c", "\n")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufffd", "")
    text = text.replace("￾", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _readability_score(text: str) -> int:
    cleaned = _clean_extracted_text(text)
    words = re.findall(r"[A-Za-z][A-Za-z]{2,}", cleaned)
    section_hits = re.findall(r"(?m)^\s*(?:\d+(?:\.\d+)*\.?|ANNEXURE\s+[A-Z0-9]+)[ \t]+", cleaned, flags=re.I)
    regulatory_hits = re.findall(
        r"\b(?:directive|insurer|outsourc|registrar|policy|risk|compliance|board|management|function|application|scope|legislative|framework)\b",
        cleaned,
        flags=re.I,
    )
    common_hits = re.findall(r"\b(?:the|and|of|to|in|for|that|with|from|by|is|are|be|as|this)\b", cleaned, flags=re.I)
    symbol_count = sum(1 for char in cleaned if not (char.isalnum() or char.isspace() or char in ".,;:()/-'’&"))
    symbol_penalty = int((symbol_count / max(len(cleaned), 1)) * 600)
    return max(
        0,
        int(len(words) * 0.5)
        + (len(section_hits) * 35)
        + (len(regulatory_hits) * 20)
        + (len(common_hits) * 2)
        - symbol_penalty,
    )


def _ocr_cache_path(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache_dir = BACKEND_DIR / "storage" / "ocr_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest}-{OCR_CACHE_VERSION}.json"


def _load_ocr_cache(path: Path) -> Tuple[str, List[dict]] | None:
    cache_path = _ocr_cache_path(path)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        text = str(payload.get("text") or "")
        pages = payload.get("pages") or []
        if text and isinstance(pages, list):
            return text, pages
    except Exception:
        return None
    return None


def _save_ocr_cache(path: Path, text: str, pages: List[dict]) -> None:
    cache_path = _ocr_cache_path(path)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"text": text, "pages": pages}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)


def _needs_ocr(pages: List[dict]) -> bool:
    if not pages:
        return True
    combined = "\n".join((page.get("text") or "").strip() for page in pages)
    if _readability_score(combined) < 120:
        return True
    return any(
        int(page.get("score") or 0) < 60
        and (int(page.get("image_count") or 0) > 0 or not str(page.get("text") or "").strip())
        for page in pages
    )


def _candidate_tesseract_paths() -> List[str]:
    candidates: List[str] = []
    env_path = _env_value("TESSERACT_CMD")
    if env_path:
        candidates.append(env_path)
    path_hit = shutil.which("tesseract") or shutil.which("tesseract.exe")
    if path_hit:
        candidates.append(path_hit)
    candidates.extend([
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / r"AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ])
    unique: List[str] = []
    seen = set()
    for candidate in candidates:
        normalized = str(candidate).strip().strip('"').strip("'")
        if not normalized:
            continue
        key = normalized.lower()
        if key not in seen:
            unique.append(normalized)
            seen.add(key)
    return unique


def _configure_tesseract_or_raise() -> str:
    try:
        import pytesseract
    except Exception as exc:
        raise RuntimeError("pytesseract is not installed. Run: .\\.venv\\Scripts\\python.exe -m pip install pytesseract pillow") from exc

    errors: List[str] = []
    for candidate in _candidate_tesseract_paths():
        candidate_path = Path(candidate)
        if not candidate_path.exists():
            errors.append(f"Missing: {candidate}")
            continue
        try:
            tess_dir = str(candidate_path.parent)
            current_path = os.environ.get("PATH", "")
            if tess_dir.lower() not in current_path.lower():
                os.environ["PATH"] = tess_dir + os.pathsep + current_path
            pytesseract.pytesseract.tesseract_cmd = str(candidate_path)
            pytesseract.get_tesseract_version()
            return str(candidate_path)
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        "Tesseract OCR could not be detected by the backend. "
        f"Checked paths: {'; '.join(_candidate_tesseract_paths())}. Errors: {'; '.join(errors)}"
    )


def _ocr_page_image(image, page_number: int) -> dict:
    import pytesseract

    language = _env_value("OCR_LANGUAGE", "eng")
    angles = [0, 90, 270, 180]
    psms = [6, 4, 11]
    best = {"page": page_number, "text": "", "method": "ocr", "rotation": 0, "psm": 6, "score": 0, "error": ""}
    errors: List[str] = []

    base = image.convert("L")
    try:
        from PIL import ImageFilter, ImageOps
        base = ImageOps.autocontrast(base)
        base = base.filter(ImageFilter.SHARPEN)
    except Exception:
        pass

    for angle in angles:
        rotated = base.rotate(angle, expand=True) if angle else base
        for psm in psms:
            config = f"--oem 3 --psm {psm} -c preserve_interword_spaces=1"
            try:
                text = pytesseract.image_to_string(rotated, lang=language, config=config, timeout=120) or ""
                text = _clean_extracted_text(text)
                score = _readability_score(text)
                if score > int(best.get("score", 0)):
                    best = {"page": page_number, "text": text, "method": "ocr", "rotation": angle, "psm": psm, "score": score, "error": ""}
            except Exception as exc:
                errors.append(f"angle={angle}, psm={psm}: {type(exc).__name__}: {exc}")
    if not best["text"]:
        best["error"] = "; ".join(errors[:8])
    return best


def _extract_with_ocr(path: Path, native_pages: List[dict] | None = None) -> Tuple[str, List[dict]]:
    import fitz
    from PIL import Image

    tesseract_command = _configure_tesseract_or_raise()
    dpi_scale = _env_float("OCR_DPI_SCALE", 3.0)
    matrix = fitz.Matrix(dpi_scale, dpi_scale)
    pages: List[dict] = []
    full_text_parts: List[str] = []
    doc = fitz.open(path)
    native_combined = "\n".join(str(page.get("text") or "") for page in (native_pages or []))
    force_ocr_all = bool(native_pages) and _readability_score(native_combined) < 120
    try:
        for index, page in enumerate(doc, start=1):
            native = native_pages[index - 1] if native_pages and index <= len(native_pages) else None
            native_score = int((native or {}).get("score") or 0)
            native_text = str((native or {}).get("text") or "").strip()
            image_count = int((native or {}).get("image_count") or 0)
            should_ocr = (
                native is None
                or force_ocr_all
                or (native_score < 60 and (image_count > 0 or not native_text))
            )
            if should_ocr:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                result = _ocr_page_image(image, index)
                result["image_count"] = image_count
            else:
                result = dict(native)
            pages.append(result)
            header = (
                f"--- Page {index} | method={result.get('method', 'ocr')} "
                f"| rotation={result.get('rotation', 0)} | psm={result.get('psm', '')} "
                f"| score={result.get('score', 0)} ---"
            )
            full_text_parts.append(f"\n\n{header}\n{result.get('text', '')}")
    finally:
        doc.close()

    combined = "".join(full_text_parts).strip()
    readable_pages = [page for page in pages if _readability_score(page.get("text", "")) >= 60]
    if not readable_pages:
        diagnostics = "; ".join(
            f"p{page.get('page')}: rotation={page.get('rotation')}, psm={page.get('psm')}, score={page.get('score')}, error={page.get('error', '')}"
            for page in pages[:5]
        )
        raise RuntimeError(
            "OCR started successfully, but did not produce readable directive text. "
            f"Tesseract detected at: {tesseract_command}. Diagnostics: {diagnostics}"
        )
    return combined, pages


def extract_pdf_text(path: Path) -> Tuple[str, List[dict]]:
    path = Path(path)
    cached = _load_ocr_cache(path)
    if cached is not None:
        return cached
    pages: List[dict] = []
    full_text_parts: List[str] = []
    try:
        import fitz
        doc = fitz.open(path)
        try:
            for index, page in enumerate(doc, start=1):
                text = _clean_extracted_text(page.get_text("text") or "")
                pages.append({
                    "page": index,
                    "text": text,
                    "method": "native",
                    "score": _readability_score(text),
                    "image_count": len(page.get_images(full=True)),
                })
                full_text_parts.append(f"\n\n--- Page {index} | method=native ---\n{text}")
        finally:
            doc.close()
    except Exception:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        for index, page in enumerate(reader.pages, start=1):
            text = _clean_extracted_text(page.extract_text() or "")
            pages.append({
                "page": index,
                "text": text,
                "method": "native",
                "score": _readability_score(text),
                "image_count": 0,
            })
            full_text_parts.append(f"\n\n--- Page {index} | method=native ---\n{text}")

    if _needs_ocr(pages):
        text, ocr_pages = _extract_with_ocr(path, pages)
        _save_ocr_cache(path, text, ocr_pages)
        return text, ocr_pages
    return "".join(full_text_parts).strip(), pages


def extraction_summary(pages: List[dict]) -> str:
    native = sum(1 for page in pages if page.get("method") == "native")
    ocr = sum(1 for page in pages if page.get("method") == "ocr")
    rotations = sorted({str(page.get("rotation")) for page in pages if page.get("method") == "ocr"})
    avg_score = int(sum(int(page.get("score") or 0) for page in pages) / max(len(pages), 1))
    if ocr:
        return f"Extracted text from {len(pages)} page(s): {native} native page(s), {ocr} OCR page(s). OCR rotations used: {', '.join(rotations) or 'none'}. Average readability score: {avg_score}."
    return f"Extracted text from {len(pages)} page(s) using native PDF text. Average readability score: {avg_score}."


def page_lookup(pages: List[dict], query: str) -> str:
    query_terms = [term.lower() for term in query.split() if len(term) > 4][:12]
    best_page = "Not located"
    best_score = 0
    for page in pages:
        text = page.get("text", "").lower()
        score = sum(1 for term in query_terms if term in text)
        if score > best_score:
            best_score = score
            best_page = str(page.get("page", "Not located"))
    return best_page if best_score else "Not located"
