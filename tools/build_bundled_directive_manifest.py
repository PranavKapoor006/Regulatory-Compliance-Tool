from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


CATEGORY_FOLDERS = {
    "Insurer / Micro Insurer": "insurer_micro_insurer",
    "Joint FSCA / PA Directives": "joint_fsca_pa",
    "Retirement Fund": "retirement_fund",
}

EXPECTED_COUNTS = {
    "Insurer / Micro Insurer": 40,
    "Joint FSCA / PA Directives": 2,
    "Retirement Fund": 8,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _actual_type(path: Path) -> str:
    with path.open("rb") as handle:
        signature = handle.read(16)
    if signature.lstrip().startswith(b"%PDF-"):
        return "pdf"
    if signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return "doc"
    return "unknown"


def build(catalog_path: Path, bundle_root: Path, output_path: Path) -> dict[str, Any]:
    source = json.loads(catalog_path.read_text(encoding="utf-8"))
    rows = source.get("value")
    if not isinstance(rows, list):
        raise ValueError("The source catalog does not contain a value array.")

    records: list[dict[str, Any]] = []
    counts = {category: 0 for category in EXPECTED_COUNTS}
    missing: list[str] = []
    invalid: list[str] = []

    for row in rows:
        category = str(row.get("cr3ad_category1") or "").strip()
        folder = CATEGORY_FOLDERS.get(category)
        if not folder:
            continue
        filename = str(row.get("cr3ad_document_name") or "").strip()
        if filename.lower().endswith(".doc"):
            continue
        path = bundle_root / folder / filename
        if not path.is_file():
            missing.append(str(path))
            continue
        actual_type = _actual_type(path)
        if actual_type == "unknown":
            invalid.append(str(path))
            continue
        counts[category] += 1
        records.append(
            {
                "id": str(row.get("cr3ad_directiveid") or "").strip(),
                "title": str(row.get("cr3ad_name") or Path(filename).stem).strip(),
                "section": category,
                "category": category,
                "year": str(row.get("cr3ad_publisheddate") or "")[:4] or "Unknown",
                "filename": filename,
                "relative_path": f"{folder}/{filename}",
                "description": str(row.get("cr3ad_descriptions") or "").strip(),
                "subcategory": str(row.get("cr3ad_subcategory") or "").strip(),
                "document_type": actual_type,
                "file_size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "source_record_id": str(row.get("cr3ad_directiveid") or "").strip(),
                "source_modified": str(row.get("modifiedon") or "").strip(),
                "source_published": str(row.get("cr3ad_publisheddate") or "").strip(),
            }
        )

    if missing:
        raise ValueError(f"Missing {len(missing)} bundled files: {missing}")
    if invalid:
        raise ValueError(f"Invalid bundled file signatures: {invalid}")
    if counts != EXPECTED_COUNTS:
        raise ValueError(f"Category distribution mismatch: {counts}")
    if len(records) != 50:
        raise ValueError(f"Expected 50 records, found {len(records)}.")
    if len({record["id"] for record in records}) != 50:
        raise ValueError("Directive record identifiers are not unique.")
    if len({record["relative_path"] for record in records}) != 50:
        raise ValueError("Bundled relative paths are not unique.")

    records.sort(key=lambda item: (item["category"], item["title"].casefold()))
    payload = {
        "schema_version": 1,
        "bundle_version": "2026-08-23-demo.1",
        "mode": "fully-local",
        "network_access": False,
        "expected_directives": 50,
        "category_counts": counts,
        "source_page": "https://www.fsca.co.za/Supervisory-Information/?collapse=collapseEight",
        "records": records,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = build(args.catalog, args.bundle_root, args.output)
    print(
        json.dumps(
            {
                "records": len(payload["records"]),
                "category_counts": payload["category_counts"],
                "network_access": payload["network_access"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
