# Test evidence — 21 July 2026

## Automated

- Python compile check completed for all backend modules and tests.
- Backend unit/API suite: 14 tests executed.
- Frontend TypeScript and Vite production build completed successfully.
- Production bundle generated successfully (`dist/index.html`, CSS, and JavaScript assets).

## Live FSCA crawler

- Source: public FSCA Directives SharePoint list embedded by `Directives.aspx`.
- Authoritative result: 55 rows.
- Category reconciliation: 40 Insurer / Micro Insurer + 2 Joint FSCA / PA Directives + 13 Retirement Fund = 55.
- 49 PDF rows selectable; six non-PDF annexure/form rows marked unsupported.
- Unrelated archived reports and strategy documents excluded.
- Live Joint FSCA/PA PDF download verified with a `%PDF-` signature and 272,814-byte response.
- Exact-reference guard tested to prevent an unrelated live row from receiving Directive 101 contents.

## Obligation extraction

- Directive 101 native extraction: five pages; hierarchy preserved (`1`, `1.1`–`1.4`, `2`, `2.1`–`2.3`, `3`, `4`).
- Date text such as `30 January 2004` is not treated as a section.
- Directive 159 OCR: 10 scanned pages; rotation correction selected 90 degrees where required.
- Directive 159 cleaned breakdown: 94 rows including Introduction and Annexure A.
- OCR corrections verified for `5.1.2`, parent `7.5`, and children `7.5.1`–`7.5.9`.
- Unfinished parent clauses are retained for traceability but identified as non-actionable; their numbered child clauses carry the assessable requirements.
- OCR cache verification: cached read completed in approximately 0.001 seconds after the first full pass.
- Excel workbook sheet names verified: Obligations, Text Breakdown, Statistics, Process Log.

## Policy gap review

- Generated obligation register successfully assessed against a local synthetic outsourcing-policy fixture.
- Only three coverage statuses produced.
- KPI status totals reconciled exactly to Total Obligations.
- Completely missing rows contain no fabricated policy text/page evidence.
- Jurisdiction safeguards prevent evidence about a foreign regulator from being treated as full coverage of an FSCA/FSB obligation.
- Grounded Gemini responses are accepted only when they use an allowed evidence candidate and reproduce an exact policy quotation.
- Invalid or unavailable Gemini output falls back to the deterministic reviewer without stopping workbook generation.
- Recommendations describe the substantive policy change required and do not expose raw keyword-difference lists.
- Excel workbook sheet names verified: Executive Summary, Gap Assessment, Statistics, Process Log.
- Summary and assessment sheets were rendered and visually checked for wrapping, row height, status colours, and readable column widths.

## Pending external/UI evidence

- Capture final browser screenshots on the target Windows machine after starting both processes at `127.0.0.1:8000` and `localhost:5173`.
- Confirm final wording/classifications with the project mentor or compliance reviewer; deterministic results are intended for review, not automatic legal sign-off.
