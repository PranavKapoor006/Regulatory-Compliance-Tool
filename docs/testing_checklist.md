# Testing Checklist

## Workflow 1: Crawler -> Obligation Extraction -> Policy Gap Reviewer

- [ ] Start backend and frontend.
- [ ] Open Home page.
- [ ] Open Web Crawler.
- [ ] Select Section/Category and Year.
- [ ] Start crawl.
- [ ] Confirm matching directives display.
- [ ] Select one or more directives.
- [ ] Download selected directives.
- [ ] Confirm KPIs update.
- [ ] Confirm Documents, Data Table, and Crawl Log tabs populate.
- [ ] Proceed to Obligation Extraction.
- [ ] Select downloaded directive.
- [ ] Start extraction.
- [ ] Confirm KPIs match obligation rows.
- [ ] Download Excel and CSV.
- [ ] Proceed to Policy Gap Reviewer.
- [ ] Upload obligation register and internal policy.
- [ ] Run gap assessment.
- [ ] Confirm KPIs match detailed results.
- [ ] Download Excel and CSV.

## Workflow 2: PDF Upload -> Obligation Extraction -> Register/Policy Upload -> Gap Assessment

- [ ] Open Obligation Extraction directly.
- [ ] Upload sample directive PDF.
- [ ] Run extraction.
- [ ] Confirm Text Breakdown tab preserves sections.
- [ ] Confirm obligation register columns are correct.
- [ ] Download Excel/CSV.
- [ ] Open Policy Gap Reviewer.
- [ ] Upload generated obligation register.
- [ ] Upload internal policy PDF.
- [ ] Run review.
- [ ] Confirm no fabricated policy text is shown.
- [ ] Confirm missing/partial coverage recommendations are meaningful.
- [ ] Confirm not applicable rows do not show plain NA.

## Edge Cases

- [ ] No directives available.
- [ ] Duplicate file download.
- [ ] Failed download.
- [ ] Upload unsupported file type.
- [ ] Upload register with missing columns.
- [ ] Upload blank/scanned PDF with poor text extraction.
- [ ] Confirm Home button works on every utility page.
