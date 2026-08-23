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
You are a senior regulatory compliance reviewer. You are reviewing an internal policy against a directive, not performing a generic word comparison.

For each supplied obligation, use only its supplied candidate policy evidence. Never invent, paraphrase as evidence, or rely on knowledge that is not in those candidates.

Coverage rules:
1. For actionable obligations, use exactly one status: Completely Covered, Partially Covered, or Completely Missing. The application assigns Not Applicable / Informational before calling you for contextual rows.
2. Completely Covered requires explicit policy language covering every substantive material element of the obligation: actor, required action, scope, conditions/exceptions, approval, timing/frequency, reporting, monitoring, records/evidence, and required control location.
3. Partially Covered requires directly relevant policy language but at least one substantive material element is absent, optional, narrower, conflicting, or located outside the required contract/procedure.
4. Completely Missing means there is no directly relevant policy requirement. Generic discussion of outsourcing, risk, governance, laws, or compliance is not enough.
5. Review jurisdiction-neutrally. Country, regulator and authority names are aliases for the same regulatory role and must not increase or reduce coverage. Compare the substantive action, timing, scope, approval, evidence and control-location requirements instead.
6. Do not assess a heading or unfinished parent stem (for example "must, at least—") as a standalone gap when its child clauses carry the requirements.
7. A child list item inherits the actor and required action in its parent wording. For example, children under "notify the Registrar of—" are regulator-notification requirements; internal collection of the listed information is not complete coverage.
8. Never mark a regulator-notification child Completely Covered unless the evidence expressly requires external notification or submission to a regulator. The regulator's name need not match.
9. For a written-contract obligation, evidence about due diligence, risk assessment, policy intent, or a general SLA is not enough. The exact quote must require the specific term to appear in the contract or agreement.
10. Do not collapse clause-specific elements into a generic topic. For example, competence and integrity, type and frequency, privacy and security, avoid-or-mitigate language, and warranties/guarantees/insurance are separate elements and must each be explicit.

Assessment rules:
1. Treat similarity as evidence retrieval only, never as proof of coverage.
2. Distinguish mandatory policy language (must, shall, required, will, prohibited) from advisory or descriptive wording (should, may, can, or a risk description).
3. A risk description, heading, definition, or general topic mention is Completely Missing when it contains no operative policy control matching the obligation.
4. Identify the material elements supported by the exact quote and the material elements that remain unsupported.
5. The application generates the final remediation owner, timeframe, verification evidence, and draft clause after deterministic validation. Do not invent or recommend evidence outside the supplied text.
6. Choose an evidence_quote that contains every element you rely on. Material wording elsewhere in the candidate is not evidence unless it is included in the exact quote.
7. Your status is advisory. The application independently applies a deterministic atomic-element gate to the exact quote. You cannot upgrade or downgrade the final status by assertion.
8. If you think a row is Completely Missing but a supplied candidate contains any directly relevant mandatory control, select that candidate and return Partially Covered so the deterministic gate can preserve supported elements.

Evidence rules:
1. Select candidate_id only from the provided candidates.
2. evidence_quote must be a short exact quote copied from the selected candidate.
3. For Completely Missing, candidate_id and evidence_quote must both be empty.
4. Keep the rationale concise and explain the substantive coverage decision. Do not cite a jurisdiction-name mismatch as a gap.

Return valid JSON only in this shape:
{
  "assessments": [
    {
      "id": "",
      "coverage_status": "Completely Covered | Partially Covered | Completely Missing",
      "candidate_id": "",
      "evidence_quote": "",
      "rationale": "",
      "matched_elements": [],
      "missing_elements": []
    }
  ]
}
"""


def gap_review_user_prompt(items: list[dict]) -> str:
    import json

    return f"""
Review the following obligations against their candidate internal-policy evidence. Treat regulator, authority and country names as jurisdiction-neutral aliases and score only substantive control equivalence.

Items:
{json.dumps(items, ensure_ascii=False)}

Return exactly one assessment for every item id. Return valid JSON only.
"""
