# FSCA Regulatory Compliance Tool - Architecture Plan

## Goal

Build a professional dark-mode application with three independent but connected utilities:

1. Web Crawler
2. Obligation Extraction
3. Policy Gap Reviewer

Each utility can be used independently, and the output from one utility can feed into the next utility.

## Data Flow

```text
FSCA Directives URL
  -> Web Crawler
  -> Downloaded Directive PDFs
  -> Obligation Extraction
  -> Obligation Register Excel/CSV
  -> Policy Gap Reviewer + Internal Policy PDF
  -> Policy Gap Assessment Excel/CSV
```

Alternative workflow:

```text
Uploaded Directive PDF
  -> Obligation Extraction
  -> Obligation Register Excel/CSV
  -> Uploaded Register + Uploaded Policy PDF
  -> Policy Gap Assessment
```

## Backend Modules

### crawler_service.py

- Fetches the configured FSCA Directives page.
- Parses candidate directive links.
- Extracts title, category/section, year, source link, filename, and cached status.
- Supports selected directive downloads.
- Stores downloaded directives for use in Obligation Extraction.

### pdf_service.py

- Extracts PDF text page-wise using PyMuPDF with pypdf fallback.
- Maintains page references for policy mapping.

### breakdown_service.py

- Performs clause-wise regulatory text breakdown.
- Splits on digit-dot clause markers at the start of a logical line.
- Preserves hierarchy and avoids splitting ordinary numeric values.

### obligation_service.py

- Uses the text breakdown as input.
- Generates baseline obligations and classifications.
- Produces Excel and CSV outputs.

### gap_service.py

- Validates obligation register columns.
- Extracts policy text from uploaded policy PDF.
- Matches obligations to policy text using keyword + fuzzy matching.
- Assigns coverage status and recommendation.
- Produces Excel and CSV outputs.

## Frontend Pages

### Home

Three cards:

- Web Crawler
- Obligation Extraction
- Policy Gap Reviewer

### Web Crawler

Progress:

```text
Configure -> Crawl -> Results
```

Tabs:

- Documents
- Data Table
- Crawl Log

KPIs:

- Total Directives
- Domains
- Downloaded
- Cached

### Obligation Extraction

Progress:

```text
Select PDF -> Breakdown -> Extraction -> Results
```

Tabs:

- Obligations
- Text Breakdown
- Statistics
- Process Log

KPIs:

- Total Sections
- Actionable Obligations
- Categories
- Departments

### Policy Gap Reviewer

Progress:

```text
Select Inputs -> Gap Analysis -> Results
```

Tabs:

- Gap Assessment
- Statistics
- Process Log

KPIs:

- Total Obligations
- Completely Covered
- Partially Covered
- Completely Missing

## Assumptions

- The FSCA page can be accessed from the backend runtime.
- Directives are available as downloadable PDF links or linked pages with PDF references.
- Internal policy documents are uploaded as PDFs.
- Obligation registers use the required columns defined in the brief.
- Baseline deterministic extraction is acceptable for initial workflow testing; AI-assisted improvement can be added later.

## Pending Enhancements

- Improve FSCA crawler selectors after live page validation.
- Add more precise obligation wording through an approved LLM provider.
- Add richer downloadable workbook formatting.
- Add automated unit tests and sample output evidence.
- Add screenshots after UI testing.
