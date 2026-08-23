from pathlib import Path
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.services import gap_service
from app.services.storage import save_upload

router = APIRouter(prefix="/api/gap", tags=["Policy Gap Reviewer"])


@router.get("/available-registers")
def available_registers():
    settings = get_settings()
    docs = []
    for ext in ("*.xlsx", "*.csv"):
        docs.extend(
            {"name": p.name, "size_bytes": p.stat().st_size}
            for p in sorted(settings.output_dir.glob(ext))
            if "obligation_extraction" in p.stem and "policy_gap_assessment" not in p.stem
        )
    return {"registers": docs}


@router.post("/review")
def gap_review(
    response: Response,
    register_file: UploadFile | None = File(default=None, alias="register"),
    register_name: str | None = Form(default=None),
    policy: UploadFile = File(...),
):
    settings = get_settings()
    try:
        if not policy.filename or not policy.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Upload the internal policy as a PDF.")
        if register_file and register_file.filename:
            if not register_file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
                raise HTTPException(status_code=400, detail="Upload an Excel or CSV obligation register.")
            register_path = save_upload(register_file.file, register_file.filename, settings.uploads_dir)
        elif register_name:
            register_path = settings.output_dir / Path(register_name).name
            if not register_path.exists() or register_path.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                raise HTTPException(status_code=404, detail="Selected obligation register was not found.")
        else:
            raise HTTPException(status_code=400, detail="Select a generated register or upload an Excel/CSV register.")
        policy_path = save_upload(policy.file, policy.filename, settings.uploads_dir)
        result = gap_service.review_policy_gaps(register_path, policy_path)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Pipeline-Version"] = gap_service.PIPELINE_VERSION
        response.headers["X-Pipeline-Run"] = result["pipeline"]["run_id"]
        return result
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
    return FileResponse(
        path,
        filename=path.name,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Pipeline-Version": gap_service.PIPELINE_VERSION,
        },
    )
