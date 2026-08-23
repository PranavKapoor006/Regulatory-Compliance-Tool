from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.benchmark_service import score_benchmark  # noqa: E402
from app.services.gap_service import review_policy_gaps  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the controlled South African Directive 159 policy-gap benchmark "
            "and compare the application result with the known-answer matrix."
        )
    )
    parser.add_argument(
        "--register",
        type=Path,
        default=ROOT / "benchmark" / "Directive_159_Benchmark_Register.xlsx",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "benchmark" / "South_African_Outsourcing_Benchmark_Policy.pdf",
    )
    parser.add_argument(
        "--known-answer",
        type=Path,
        default=ROOT / "benchmark" / "Directive_159_Known_Answer_Matrix.xlsx",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmark" / "benchmark_result.json",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Allow the configured Gemini reviewer for ambiguous rows.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for path in (args.register, args.policy, args.known_answer):
        if not path.exists():
            raise FileNotFoundError(path)

    if not args.enable_llm:
        os.environ["ENABLE_LLM_GAP_REVIEW"] = "false"

    gap_result = review_policy_gaps(args.register, args.policy)
    benchmark = score_benchmark(
        args.known_answer,
        gap_result["tabs"]["gap_assessment"],
        obligation_register_path=args.register,
    )
    snapshot_files = {}
    output_dir = get_settings().output_dir
    for file_type, generated_name in gap_result["output_files"].items():
        source = output_dir / generated_name
        extension = "xlsx" if file_type == "excel" else file_type
        target = ROOT / "benchmark" / f"Directive_159_Benchmark_Result.{extension}"
        shutil.copy2(source, target)
        snapshot_files[file_type] = str(target.relative_to(ROOT))
    result = {
        "benchmark_version": "2026-07-27.5",
        "gap_pipeline": gap_result["pipeline"],
        "gap_output_files": gap_result["output_files"],
        "benchmark_snapshots": snapshot_files,
        "scores": benchmark,
        "interpretation": (
            "These percentages are measured against the controlled known-answer "
            "benchmark. They are not a substitute for legal approval on production documents."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

    gap_score = benchmark["gap"]["coverage_status_accuracy_percentage"]
    obligation_score = benchmark["obligation_extraction"]["accuracy_percentage"]
    recommendation_score = benchmark["recommendations"]["accuracy_percentage"]
    return (
        0
        if (
            gap_score >= 70.0
            and obligation_score >= 80.0
            and recommendation_score == 100.0
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
