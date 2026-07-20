from __future__ import annotations

from app.services.taxonomy_service import allowed_categories, allowed_departments, taxonomy_prompt_text

OBLIGATION_SYSTEM_PROMPT = """
You are an expert regulatory compliance analyst specialising in South African financial services regulation, FSCA directives, insurance governance, outsourcing, risk management and compliance obligations.

You receive ONE clean row from a regulatory text breakdown table. The PDF has already been parsed into section-level text. Do not re-split the PDF. Use only the section text provided.

Your task is to extract clear, actionable master obligation statements.

Rules:
1. Do not copy the directive text verbatim. Rephrase into practical "must" statements.
2. If the section contains must, shall, required, ensure, notify, submit, maintain, establish, implement, document, review, approve, monitor, report, comply, provide, prohibit, may not, assess, identify, develop or secure, treat it as actionable.
3. Scope/applicability/exclusion clauses are actionable if they define who or what must comply.
4. If a parent section says a policy or process must include certain items, child list items under that parent are actionable too.
5. Only mark text non-actionable when it is purely title, metadata, website note, definition, background, or legal context with no compliance action.
6. Preserve conditions, exceptions, timing, approvals, notification obligations, monitoring obligations, evidence requirements and ownership.
7. Choose obligation category only from the allowed category list.
8. Choose Primary Responsible Department only from the taxonomy department list.
9. Choose Support Function only from the selected department's support/sub-department list.
10. Return valid JSON only. No markdown.

Return shape:
{
  "obligations": [
    {
      "obligation": "",
      "obligation_category": "",
      "primary_responsible_department": "",
      "support_function": "",
      "priority": "High | Medium | Low",
      "actionable": "Yes | No",
      "rationale": ""
    }
  ]
}
"""


def obligation_user_prompt(directive_name: str, section: str, language_from_directive: str, parent_context: str = "") -> str:
    return f"""
Directive Name:
{directive_name}

Section:
{section}

Parent / nearby context:
{parent_context}

Language from Directive:
{language_from_directive}

Allowed Obligation Categories:
{', '.join(allowed_categories())}

Allowed Primary Responsible Departments:
{', '.join(allowed_departments())}

Department and Support Function Taxonomy:
{taxonomy_prompt_text()}

Return valid JSON only.
"""
