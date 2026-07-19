from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable

from app.core.config import get_settings


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "file"


def unique_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(filename)
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 2
    while True:
        candidate = folder / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_upload(file_obj, filename: str, target_folder: Path | None = None) -> Path:
    settings = get_settings()
    folder = target_folder or settings.uploads_dir
    target = unique_path(folder, filename)
    with target.open("wb") as out:
        shutil.copyfileobj(file_obj, out)
    return target


def list_files(folder: Path, extensions: Iterable[str] | None = None) -> list[dict]:
    extensions = {ext.lower() for ext in extensions} if extensions else None
    files: list[dict] = []
    if not folder.exists():
        return files
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if extensions and path.suffix.lower() not in extensions:
            continue
        files.append({"name": path.name, "path": str(path), "size_bytes": path.stat().st_size})
    return files


def output_path(filename: str) -> Path:
    settings = get_settings()
    return unique_path(settings.output_dir, filename)
