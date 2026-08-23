# RegulaMosaic

Demo handover release: `2026-08-23.1`

RegulaMosaic is a local regulatory-intelligence workspace for extracting traceable obligations and comparing them with internal-policy evidence. This handover combines the validated `2026-08-06.2` extraction pipeline with the jurisdiction-neutral `2026-08-18.2-neutral-recommendations` gap-review pipeline and the final mentor-approved interface.

This local web application supports a review workflow with three utilities:

1. **FSCA Directive Library** — opens a checksummed, offline collection of 50 demo-ready official PDFs by topic.
2. **Obligation Extraction** — converts native or scanned directive PDFs into clause-level obligation registers with source-page traceability.
3. **Policy Gap Reviewer** — compares actionable obligations with internal-policy evidence and produces coverage findings and targeted recommendations.

AI-generated results are review assistance, not legal advice. At least one qualified compliance professional must review every output before use or implementation.

## Validated component versions

- Obligation extraction: `2026-08-06.2`
- Policy gap review: `2026-08-18.2-neutral-recommendations`
- Controlled benchmark: `2026-07-27.5`
- Offline PDF directive library: `2026-08-23-demo.1`
- Demo handover packaging and UI presentation: `2026-08-23.1`

## Offline directive library

All demo files are installed under `backend/bundled_directives/` and validated at startup against `manifest.json`. Legacy Word form/annexure files are intentionally excluded from this demo repository because they are not accepted by the extraction workflow and may contain sensitive form fields.

| Topic | Verified PDFs |
|---|---:|
| Insurer / Micro Insurer | 40 |
| Joint FSCA / PA Directives | 2 |
| Retirement Fund | 8 |
| **Total** | **50** |

Selecting or filtering a topic is a local operation and sends zero FSCA requests. There is no pull or refresh step. Every bundled file is a validated PDF and can be offered directly to Obligation Extraction.

## Project structure

```text
backend/
  app/
    core/
    routers/
    services/
  bundled_directives/       # 50 verified official PDFs plus integrity manifest
  tests/
  .env.example
  requirements.txt
  run.py
benchmark/
  aegis_v2/                 # verified full-population assessment
  run_benchmark.py
  run_aegis_benchmark.py
frontend/
  src/
  package.json
  package-lock.json
  vite.config.ts
docs/
verify_running_app.ps1
```

Runtime folders, virtual environments, dependency folders, secrets, and generated outputs are intentionally excluded from the handover archive.

## Windows setup

### Prerequisites

- Python 3.12
- Node.js 20 or later
- Tesseract OCR for scanned/rotated PDFs

Install Tesseract if required:

```powershell
winget install UB-Mannheim.TesseractOCR
```

### 1. Backend

```powershell
cd C:\path\to\FSCA-Regulatory-Compliance-Tool\backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep this window open. Confirm the API at `http://127.0.0.1:8000/api/health`.

### 2. Frontend

Open a second PowerShell window:

```powershell
cd C:\path\to\FSCA-Regulatory-Compliance-Tool\frontend
npm.cmd ci
npm.cmd run dev
```

Open `http://localhost:5173`. The Vite development server proxies `/api` to `http://127.0.0.1:8000`.

### 3. Optional Gemini review

The application works without an AI key. `ENABLE_LLM_EXTRACTION` and `ENABLE_LLM_GAP_REVIEW` are disabled by default. Before enabling Gemini, confirm the organization’s data-handling requirements because selected directive and policy text will be sent to the configured provider.

Update `backend/.env` only when approved:

```text
ENABLE_LLM_GAP_REVIEW=true
GEMINI_API_KEY=
```

## User workflow

1. Open **Library** and select one of the three topics. Confirm `40/40`, `2/2`, or `8/8`.
2. Open **Obligations**, select a bundled PDF or upload a directive/circular PDF, and run extraction.
3. Review the obligation register, sanitized source breakdown, statistics, review flags, and process log. Download Excel or CSV. The export is blocked if high-confidence OCR/page artifacts remain.
4. Open **Gap Review**, select the generated register or upload another register, then upload the internal-policy PDF.
5. Review coverage statuses, exact evidence citations, missing elements, recommendations, and manual-review flags. Download Excel or CSV.
6. Have a qualified compliance professional validate the output before use.

## Output files

Obligation extraction workbook:

- `Obligations`
- `Accuracy Review` (row-level internal quality information)
- `Text Breakdown`
- `Statistics`
- `Process Log`

Policy gap workbook:

- `Executive Summary`
- `Gap Assessment`
- `Statistics`
- `Process Log`

Client-facing dashboards and executive summaries use factual counts rather than overall accuracy/confidence percentages. Row-level internal quality fields remain available for validation and testing.

## Verification

Run all backend tests:

```powershell
cd C:\path\to\FSCA-Regulatory-Compliance-Tool\backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Build the frontend:

```powershell
cd C:\path\to\FSCA-Regulatory-Compliance-Tool\frontend
npm.cmd run build
```

Run the controlled Directive 159 benchmark:

```powershell
cd C:\path\to\FSCA-Regulatory-Compliance-Tool
backend\.venv\Scripts\python.exe benchmark\run_benchmark.py
```

Run the Aegis full-population benchmark:

```powershell
backend\.venv\Scripts\python.exe benchmark\run_aegis_benchmark.py `
  benchmark\aegis_v2\Aegis_v2_Verified_Assessment_2026-07-27.5.xlsx
```

With the backend running, execute the release verifier:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\verify_running_app.ps1
```

Expected release checks include:

- obligation pipeline `2026-08-06.2`;
- gap pipeline `2026-08-18.2-neutral-recommendations` and controlled benchmark `2026-07-27.5`;
- offline PDF library `2026-08-23-demo.1`;
- 50 bundled PDFs with exact `40 / 2 / 8` populations;
- zero runtime FSCA requests; and
- no pull/refresh controls.

## Controlled validation scope

The controlled Directive 159 benchmark verifies 56 known-answer coverage classifications, evidence grounding, missing elements, 66 recommendation packages, and 10 extraction checks. The Aegis benchmark verifies all 75 actionable rows and eight intentionally seeded policy shortcomings.

These results prove regression performance on known-answer fixtures. They do not establish legal accuracy for arbitrary regulators, directives, or internal policies.

## Known limitations

- OCR quality depends on scan quality and installed Tesseract language data. The export sanitizer removes high-confidence page headers, footnotes, and OCR debris without replacing qualified source review.
- The first OCR pass can be CPU-intensive; cached repeats are faster.
- Legacy Word form/annexure files are excluded from the demo repository; retain any approved archival copies outside GitHub if they are needed later.
- Taxonomy, jurisdiction, client output schema, and model settings may require configuration for a new client.
- Gemini usage is subject to provider token/quota limits and approved data-handling rules.
- Professional review remains mandatory for every production output.

See `docs/TEST_EVIDENCE.md` and `FINAL_HANDOVER_CHECKLIST.md` for the executed release gates and recipient checklist.
