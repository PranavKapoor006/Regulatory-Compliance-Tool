from __future__ import annotations

from pathlib import Path
from typing import List, Tuple


def _needs_ocr(pages: List[dict]) -> bool:
    if not pages:
        return True
    combined = " ".join((page.get("text") or "").strip() for page in pages)
    return len(combined) < 80


def _extract_with_ocr(path: Path) -> Tuple[str, List[dict]]:
    """OCR fallback for image-based PDFs. Requires system tesseract."""
    import fitz
    import pytesseract
    from PIL import Image
    import io

    pages: List[dict] = []
    full_text_parts: List[str] = []
    doc = fitz.open(path)
    for index, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(image) or ""
        pages.append({"page": index, "text": text})
        full_text_parts.append(f"\n\n--- Page {index} ---\n{text}")
    doc.close()
    return "".join(full_text_parts).strip(), pages


def extract_pdf_text(path: Path) -> Tuple[str, List[dict]]:
    """Extract page-wise text from PDF.

    Native text extraction is attempted first. If the PDF is image-based and returns
    very little text, OCR is used as a fallback.
    """
    pages: List[dict] = []
    full_text_parts: List[str] = []
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(path)
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            pages.append({"page": index, "text": text})
            full_text_parts.append(f"\n\n--- Page {index} ---\n{text}")
        doc.close()
    except Exception:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append({"page": index, "text": text})
            full_text_parts.append(f"\n\n--- Page {index} ---\n{text}")

    if _needs_ocr(pages):
        try:
            return _extract_with_ocr(path)
        except Exception:
            # Keep native extraction output and let downstream process log reveal low text yield.
            pass
    return "".join(full_text_parts).strip(), pages


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
