# Final Release Test Evidence

Release: `2026-08-23.1` demo repository  
Original validation: 13 August 2026; demo-package verification: 23 August 2026  
Environment: Python 3.12 / Node frontend production build

## Release decision

All automated, benchmark, bundle-integrity, and local API smoke gates passed. The build is approved for final handover as a compliance-review assistant, subject to the limitations and mandatory professional-review requirement below.

## Automated gates

| Gate | Result |
|---|---:|
| Backend unit/API suite | 117/117 passed |
| Frontend TypeScript/Vite production build | Passed |
| Runtime API health | HTTP 200 / `ok` |
| Runtime diagnostics | HTTP 200 / `ok` |
| Client-facing release safeguards | Passed |
| Dynamic gap-workbook title | Passed |
| Mentor-requested naming and gradient UI | Passed |

The client-facing safeguard test verifies that the application does not display overall extraction/gap confidence percentages, does display the qualified-review warning, has no pull/refresh topic controls, uses the transparent white/yellow EY asset, and keeps the frontend/verifier backend port synchronized. Final UI verification also covers the RegulaMosaic product name, explicit Home navigation, mutually exclusive register/PDF inputs, executive result filters, the evidence-comparison drawer, and stage-based processing feedback without fabricated percentages.

## Offline library gates

| Gate | Result |
|---|---:|
| Bundled official PDF records | 50/50 |
| SHA-256 file verification | 50/50 |
| PDF signature verification | 50/50 |
| Insurer / Micro Insurer | 40/40 |
| Joint FSCA / PA Directives | 2/2 |
| Retirement Fund | 8/8 |
| Validated PDFs exposed to extraction | 50 |
| Legacy Word form files in demo repository | 0 |
| Topic-selection FSCA requests | 0 |
| Disabled bulk action | HTTP 409 as designed |

## Controlled accuracy gates

| Gate | Result |
|---|---:|
| Fresh scanned-PDF obligation extraction checks | 10/10 |
| Incorrect splitting | 0 |
| OCR-cleaning errors | 0 |
| Coverage classifications | 56/56 |
| Evidence grounding | 56/56 |
| Missing-element checks | 56/56 |
| Recommendation packages | 66/66 |
| False complete classifications | 0 |
| False missing classifications | 0 |

The obligation score above was generated from a fresh end-to-end run of the bundled 10-page scanned Directive 159 PDF. The exported Obligations and Text Breakdown sheets contained zero recognized OCR/footer artifacts. The same fresh register passed the complete controlled gap, evidence, missing-element, and recommendation suite.

## Aegis full-population gates

Both the verified assessment workbook and the latest supplied assessment workbook were scored independently.

| Gate | Result |
|---|---:|
| Actionable rows | 75/75 |
| Expected gap rows detected | 14/14 |
| Correct covered rows | 61/61 |
| False positives | 0 |
| False negatives | 0 |
| Seeded-shortcoming dimensions | 32/32 |

## Operational smoke test

The finished backend was started and called over HTTP. The following runtime behaviors were confirmed:

- `/api/health` returned obligation pipeline `2026-08-06.2`, gap pipeline `2026-08-18.2-neutral-recommendations`, crawler/library `2026-08-23-demo.1`, and `network_access=false`.
- `/api/diagnostics` returned nine checks and overall status `ok`.
- `/api/obligations/available-directives` returned 50 validated PDFs.
- Each topic returned its complete expected population with `network_requests=0`.
- `/api/crawler/cache-all` returned `409` because all 50 demo-ready PDFs are already bundled.

## Interpretation and release limitations

The benchmark percentages measure controlled, known-answer fixtures. They do not guarantee legal accuracy on arbitrary directives or policies. OCR quality, document structure, jurisdiction, taxonomy configuration, model selection, and provider quota can change real-world results.

AI-generated output must be reviewed by at least one qualified compliance professional before use or implementation.
