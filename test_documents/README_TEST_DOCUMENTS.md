# FSCA Test Documents

Use these files to test the FSCA Regulatory Compliance Tool.

## Workflow 1 - PDF upload to obligation extraction
1. Open Obligation Extraction.
2. Upload `FSCA_Test_Directive_240A_Operational_Outsourcing.pdf`.
3. Run extraction.
4. Check Text Breakdown and Obligations tabs.
5. Download Excel/CSV outputs.

## Workflow 2 - Policy gap reviewer direct upload
1. Open Policy Gap Reviewer.
2. Upload `FSCA_Test_Obligation_Register.xlsx` or `.csv` as the obligation register.
3. Upload `ABC_Bank_Test_Outsourcing_Policy.pdf` as the internal policy.
4. Run gap assessment.

## Expected coverage mix
The policy is intentionally designed to produce a mix of:
- Completely Covered items
- Partially Covered items
- Completely Missing items
- Not Applicable / Informational items

Expected examples:
- Section 5.2 should likely be missing because the policy does not define FSCA notification within 10 business days.
- Section 4.1 should likely be partially covered because subcontracting risk is not included.
- Section 7 should likely be partially covered because exit options exist but tested exit plans are not mandatory.
- Section 8.1 should be informational, not plain NA.

These documents are synthetic and are not official FSCA or client documents.
