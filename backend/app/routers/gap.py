from pathlib import Path
import traceback

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.services.gap_service import review_policy_gaps
from app.services.storage import save_upload

router = APIRouter(prefix="/api/gap", tags=["Policy Gap Reviewer"])


@router.get("/available-registers")
def available_registers():
    settings = get_settings()
    docs = []
    for ext in ("*.xlsx", "*.csv"):
        docs.extend({"name": p.name, "size_bytes": p.stat().st_size} for p in sorted(settings.output_dir.glob(ext)))
    return {"registers": docs}


@router.post("/review")
def gap_review(
    register: UploadFile = File(...),
    policy: UploadFile = File(...),
):
    settings = get_settings()
    try:
        if not register.filename or not register.filename.lower().endswith((".xlsx", ".xls", ".csv")):
            raise HTTPException(status_code=400, detail="Upload an Excel or CSV obligation register.")
        if not policy.filename or not policy.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Upload the internal policy as a PDF.")
        register_path = save_upload(register.file, register.filename, settings.uploads_dir)
        policy_path = save_upload(policy.file, policy.filename, settings.uploads_dir)
        return review_policy_gaps(register_path, policy_path)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Policy gap review failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/outputs/{filename}")
def gap_output(filename: str):
    settings = get_settings()
    path = settings.output_dir / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found.")
    return FileResponse(path, filename=path.name)
