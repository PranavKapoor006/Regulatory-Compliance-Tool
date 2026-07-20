from pathlib import Path
import traceback

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.services.obligation_service import extract_obligations_from_pdf
from app.services.storage import save_upload

router = APIRouter(prefix="/api/obligations", tags=["Obligation Extraction"])


@router.get("/available-directives")
def available_directives():
    settings = get_settings()
    docs = [
        {"name": p.name, "size_bytes": p.stat().st_size}
        for p in sorted(settings.downloaded_dir.glob("*.pdf"))
    ]
    return {"documents": docs}


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
        elif directive_name:
            path = settings.downloaded_dir / Path(directive_name).name
            if not path.exists():
                raise HTTPException(status_code=404, detail="Selected crawler directive was not found.")
        else:
            raise HTTPException(status_code=400, detail="Select a downloaded directive or upload a PDF.")

        result = extract_obligations_from_pdf(path)
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
