# Final Handover Checklist

Release: `2026-08-23.1` demo repository

## Files to hand over

- Private GitHub repository release branch or approved release ZIP
- `README.md` inside the archive
- `docs/TEST_EVIDENCE.md` inside the archive

## Recipient installation

1. Extract the ZIP into a new project folder.
2. Create a new Python 3.12 virtual environment under `backend/.venv`.
3. Install `backend/requirements.txt`.
4. Copy `backend/.env.example` to `backend/.env`.
5. Keep both LLM feature flags disabled unless data-handling approval and a local API key are available.
6. Run the backend on `127.0.0.1:8000`.
7. Run `npm.cmd ci` and `npm.cmd run dev` in `frontend`.
8. Open `http://localhost:5173`.
9. Run `verify_running_app.ps1`.

## Acceptance checks

- [ ] Home page displays the transparent yellow/white EY logo without a box.
- [ ] Product headings display “RegulaMosaic” without duplicating “EY” beside the persistent logo.
- [ ] Home navigation is visible from every utility.
- [ ] Selecting Option A disables Option B, and selecting Option B disables Option A.
- [ ] Gap results provide executive status filters and an evidence-comparison drawer.
- [ ] Processing views show real stages and elapsed time without fabricated percentages.
- [ ] Navigation controls and workspace cards display the approved yellow-to-ivory gradient treatment.
- [ ] Library topic counts show `40/40`, `2/2`, and `8/8`.
- [ ] Topic selection sends zero FSCA requests.
- [ ] Obligation Extraction lists 50 bundled PDFs.
- [ ] A native PDF extraction produces Excel and CSV output.
- [ ] A scanned PDF extraction works when Tesseract is installed.
- [ ] Gap Review accepts an obligation register plus an internal-policy PDF.
- [ ] Gap Review output contains evidence, missing elements, recommendations, and review flags.
- [ ] Client-facing dashboard KPIs use factual counts rather than overall confidence percentages.
- [ ] Qualified-professional review warning is visible.

## Commands

```powershell
cd .\backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second window:

```powershell
cd .\frontend
npm.cmd ci
npm.cmd run dev
```

In a third window from the project root:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\verify_running_app.ps1
```

## Known limitations to communicate

- Controlled benchmarks are not a guarantee for unseen legal documents.
- Qualified compliance review is mandatory.
- OCR depends on scan quality and Tesseract availability.
- Legacy Word form/annexure files are intentionally excluded from the demo repository.
- Gemini is optional, disabled by default, subject to approved data handling, and affected by provider quota/token limits.
- Client-specific taxonomy, jurisdiction, model, and output requirements may require configuration.

## Ownership notes

- Runtime files belong under `backend/storage/` and are not included in the release archive.
- API keys belong only in local `backend/.env`; never commit or distribute them.
- Re-run all tests and both benchmark commands after any future change to extraction, evidence retrieval, classification, recommendations, or the bundled manifest.
