from pathlib import Path
import traceback

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.services.document_validation_service import valid_pdf_path
from app.services.obligation_service import extract_obligations_from_pdf
from app.services.crawler_service import crawler_service
from app.services.storage import save_upload

router = APIRouter(prefix="/api/obligations", tags=["Obligation Extraction"])


@router.get("/available-directives")
def available_directives():
    documents = crawler_service.library()
    return {
        "documents": documents,
        "source_mode": "direct-upload-or-bundled-library",
        "message": (
            "Upload a PDF directly or select one of the official PDFs bundled "
            "with the program."
        ),
    }


@router.post("/extract")
def extract_obligations(
    directive_name: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
):
    settings = get_settings()
    try:
        if file and file.filename:
            if not file.filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail="Only PDF directive/circular files are supported.")
            path = save_upload(file.file, file.filename, settings.uploads_dir)
            if not valid_pdf_path(path):
                raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")
            source_mode = "direct-upload"
        elif directive_name:
            safe_name = Path(directive_name).name
            path = crawler_service.resolve_bundled_pdf(safe_name)
            if safe_name != directive_name or path is None or not valid_pdf_path(path):
                raise HTTPException(
                    status_code=404,
                    detail="The selected bundled PDF is unavailable or failed integrity validation.",
                )
            source_mode = "bundled-library"
        else:
            raise HTTPException(status_code=400, detail="Upload a directive or circular PDF.")

        result = extract_obligations_from_pdf(path, input_mode=source_mode)
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Obligation extraction failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/outputs/{filename}")
def obligation_output(filename: str):
    settings = get_settings()
    path = settings.output_dir / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found.")
    return FileResponse(path, filename=path.name)
