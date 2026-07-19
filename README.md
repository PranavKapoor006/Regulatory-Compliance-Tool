# FSCA Regulatory Compliance Tool

A modular compliance utility suite for FSCA regulatory directives with three connected workflows:

1. **Web Crawler** - discovers and downloads FSCA directives from the configured source URL.
2. **Obligation Extraction** - breaks a directive into clause-wise regulatory text and generates an obligation register.
3. **Policy Gap Reviewer** - compares obligations against an uploaded internal policy and generates a coverage/gap assessment.

This is the initial project foundation. It includes working backend routes, a professional dark-mode frontend shell, file upload/download flows, regulatory text breakdown logic, baseline obligation generation, and baseline policy matching.

## Project Structure

```text
backend/
  app/
    core/config.py
    models/schemas.py
    routers/crawler.py
    routers/obligations.py
    routers/gap.py
    services/
      crawler_service.py
      pdf_service.py
      breakdown_service.py
      obligation_service.py
      gap_service.py
  requirements.txt
  .env.example
  run.py
frontend/
  src/App.tsx
  src/App.css
  src/main.tsx
  package.json
docs/
  architecture_plan.md
  testing_checklist.md
```

## Setup

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Backend runs at:

```text
http://127.0.0.1:8000
```

### Frontend

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Frontend runs at:

```text
http://localhost:5173/
```

## Environment Variables

Use `backend/.env.example` as the template.

```text
APP_NAME=FSCA Regulatory Compliance Tool
FRONTEND_ORIGIN=http://localhost:5173
FSCA_DIRECTIVES_URL=https://www2.fsca.co.za/Regulatory%20Frameworks/Pages/Directives.aspx
STORAGE_ROOT=storage
MAX_UPLOAD_MB=75
```

## Text Breakdown Logic

The regulatory text breakdown is performed before obligation generation.

Current logic:

- Extract PDF text page-wise.
- Normalize whitespace while preserving line boundaries.
- Detect digit-based clause markers only at the beginning of a logical line.
- Supported markers include `1`, `1.`, `2.1`, `2.2`, and `2.2.1`.
- Hierarchical references are preserved as section identifiers.
- Text before the first numbered clause is stored as `Introduction`.
- Numbers inside dates, amounts, percentages, and sentences are not split because they do not appear as line-start clause markers.

## Output Structures

### Obligation Register

```text
Section
Language from Directive
Obligation
Obligation Category
Primary Responsible Department
Support Function
Priority
Actionable
```

### Policy Gap Assessment

```text
Section
Language from Directive
Obligation
Obligation Category
Primary Responsible Department
Support Function
Coverage Status
Policy Gap and Recommendations
Policy Page
Corresponding Policy Text
Priority
```

## Notes

- The current obligation extraction and gap reviewer use deterministic baseline logic so that the end-to-end workflow can be tested without an LLM dependency.
- The crawler is designed to use the configured FSCA Directives page. Website structure may require selector tuning after live testing.
- The policy gap reviewer only uses evidence from the uploaded policy PDF. It does not fabricate supporting policy text.
- Completely covered obligations do not receive unnecessary remediation recommendations.
- Not applicable/informational items return a reasoning sentence rather than plain `NA`.

## Next Implementation Milestones

1. Validate crawler parsing against the live FSCA page.
2. Improve obligation generation quality using a configurable AI/LLM layer if approved.
3. Add richer Excel formatting and dashboard-style statistics sheets.
4. Add screenshots and generated sample outputs after first full workflow test.
5. Add automated tests for text breakdown and column validation.
