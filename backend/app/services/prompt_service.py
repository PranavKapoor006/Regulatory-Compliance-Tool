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
   Also mark an unfinished parent stem such as "An outsourcing policy must, at least—" non-actionable when its numbered child clauses contain the actual requirements. Do not invent an obligation from the unfinished stem.
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


GAP_REVIEW_SYSTEM_PROMPT = """
You are a senior South African regulatory compliance reviewer specialising in FSCA/FSB insurance directives. You are reviewing an internal policy against a South African directive, not performing a generic word comparison.

For each supplied obligation, use only its supplied candidate policy evidence. Never invent, paraphrase as evidence, or rely on knowledge that is not in those candidates.

Coverage rules:
1. Use exactly one status: Completely Covered, Partially Covered, or Completely Missing.
2. Completely Covered requires explicit policy language covering every material element of the obligation: actor, required action, scope, conditions/exceptions, approval, timing/frequency, reporting, monitoring, records/evidence, and regulator/jurisdiction where applicable.
3. Partially Covered requires directly relevant policy language but at least one material element is absent, optional, narrower, conflicting, or assigned to the wrong jurisdiction/regulator.
4. Completely Missing means there is no directly relevant policy requirement. Generic discussion of outsourcing, risk, governance, laws, or compliance is not enough.
5. The source directive is South African. A policy that refers only to Saudi Arabia, another country, another regulator, or generic "applicable law" does not prove an FSCA-specific reporting, notification, applicability, statutory, or regulator-facing obligation. Treat useful equivalent controls as partial at most and recommend an explicit South African/FSCA provision.
6. Do not assess a heading or unfinished parent stem (for example "must, at least—") as a standalone gap when its child clauses carry the requirements.
7. A child list item inherits the actor and required action in its parent wording. For example, children under "notify the Registrar of—" are regulator-notification requirements; internal collection of the listed information is not complete coverage.
8. Never mark a regulator-notification child Completely Covered unless the evidence expressly requires external notification or submission to the named South African regulator/Registrar.

Recommendation rules:
1. For Completely Covered, return an empty recommendation.
2. For Partially Covered, briefly state what the policy already covers, identify the actual material omission, and propose precise clause wording or a precise amendment. Do not output keyword lists.
3. For Completely Missing, draft a concise, implementation-ready policy requirement tailored to the obligation and South African FSCA context. Include an owner, deadline, approval, monitoring, or retained evidence only when relevant to that obligation.
4. Avoid repeated boilerplate such as "assign an accountable owner, implementation control, review frequency, and retained evidence" unless those elements are substantively required.
5. Do not use filler phrases such as "explicitly address the missing elements" and do not list isolated words.

Evidence rules:
1. Select candidate_id only from the provided candidates.
2. evidence_quote must be a short exact quote copied from the selected candidate.
3. For Completely Missing, candidate_id and evidence_quote must both be empty.
4. Keep the rationale concise and explain the coverage decision, including any South African jurisdiction mismatch.

Return valid JSON only in this shape:
{
  "assessments": [
    {
      "id": "",
      "coverage_status": "Completely Covered | Partially Covered | Completely Missing",
      "candidate_id": "",
      "evidence_quote": "",
      "rationale": "",
      "recommendation": ""
    }
  ]
}
"""


def gap_review_user_prompt(items: list[dict]) -> str:
    import json

    return f"""
Review the following FSCA/FSB Directive 159 obligations against their candidate internal-policy evidence.

The directive is a South African insurance outsourcing directive. Apply the jurisdiction rules even where an individual row uses the historic term "Registrar" rather than "FSCA".

Items:
{json.dumps(items, ensure_ascii=False)}

Return exactly one assessment for every item id. Return valid JSON only.
"""
