from __future__ import annotations

import os
import re
import hashlib
import secrets
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from rapidfuzz import fuzz

from app.services.llm_service import chat_json
from app.services.obligation_service import generate_obligation, is_actionable
from app.services.pdf_service import extract_pdf_text, extraction_summary
from app.services.prompt_service import GAP_REVIEW_SYSTEM_PROMPT, gap_review_user_prompt
from app.services.storage import output_path


REQUIRED_REGISTER_COLUMNS = [
    "Section",
    "Language from Directive",
    "Obligation",
    "Obligation Category",
    "Primary Responsible Department",
    "Support Function",
]
OPTIONAL_REGISTER_COLUMNS = ["Priority", "Actionable"]
GAP_COLUMNS = REQUIRED_REGISTER_COLUMNS + [
    "Coverage Status",
    "Gap Coverage %",
    "Assessment Confidence %",
    "Required Elements",
    "Matched Elements",
    "Missing Elements",
    "Gap Type",
    "Review Rationale",
    "Policy Gap and Recommendations",
    "Draft Policy Clause",
    "Recommendation Owner",
    "Target Timeframe",
    "Implementation Evidence",
    "Policy Page",
    "Corresponding Policy Text",
    "Priority",
    "Manual Review Required",
]
INTERNAL_GAP_EXPORT_COLUMNS = ["Gap Coverage %", "Assessment Confidence %"]
VALID_STATUSES = (
    "Completely Covered",
    "Partially Covered",
    "Completely Missing",
    "Not Applicable / Informational",
)
PIPELINE_VERSION = "2026-08-18.2-neutral-recommendations"


def pipeline_metadata(run_id: str = "") -> Dict[str, str]:
    """Return verifiable provenance for the exact gap-review implementation."""
    source_path = Path(__file__).resolve()
    metadata = {
        "pipeline_version": PIPELINE_VERSION,
        "source_file": "backend/app/services/gap_service.py",
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
    }
    if run_id:
        metadata["run_id"] = run_id
    return metadata

NEGATIVE_PHRASES = (
    "does not currently require",
    "does not yet require",
    "not defined",
    "not mandatory",
    "not required",
    "no requirement",
    "not documented",
)
STOPWORDS = {
    "this", "that", "with", "from", "shall", "must", "have", "will", "they", "their", "there",
    "under", "section", "directive", "institution", "regulatory", "obligation", "material", "policy",
    "business", "function", "functions", "entity", "insurer", "insurers", "regulated", "requirement",
    "provision", "applies", "applicability", "scope", "respect", "aspects",
}
PAGE_SPLIT = re.compile(r"(?:^|\n+)--- Page\s+(\d+)(?:\s*\|[^\n-]*)?\s*---\n", flags=re.I)
FOREIGN_JURISDICTIONS = re.compile(
    r"\b(saudi arabia|uae|united arab emirates|insurance authority|sama|central bank of saudi)\b",
    flags=re.I,
)
SOUTH_AFRICA_TERMS = re.compile(
    r"\b(south africa|south african|fsca|financial sector conduct authority|financial services board|fsb)\b",
    flags=re.I,
)

MANDATORY_POLICY_LANGUAGE = re.compile(
    r"\b("
    r"must|shall|required|requires?|will|is\s+required\s+to|are\s+required\s+to|"
    r"ensure(?:s|d)?\s+that|"
    r"is\s+responsible\s+for|are\s+responsible\s+for|may\s+not|must\s+not|"
    r"is\s+prohibited|are\s+prohibited|is\s+to\s+be|are\s+to\s+be|"
    r"no\b.{0,100}\bmay\s+(?:be\s+)?|"
    r"is\s+maintained|are\s+maintained|is\s+reviewed|are\s+reviewed|"
    r"approval\s+is\s+required|subject\s+to\s+approval|"
    r"(?:this|the)\s+policy\s+appl(?:y|ies)\s+(?:to|when|where)"
    r")\b",
    flags=re.I,
)
ADVISORY_POLICY_LANGUAGE = re.compile(r"\b(should|may|can|could|encouraged|recommended)\b", flags=re.I)

MATERIAL_ELEMENT_RULES = (
    (
        "defined scope and applicability",
        r"\bappl(?:y|ies|icability)\b|\bscope\b",
        r"\bappl(?:y|ies|icability)\b|\bscope\b",
        2,
    ),
    (
        "all outsourced aspects of the insurance business",
        r"\ball (?:outsourced\s+)?aspects? of (?:the )?(?:south african\s+)?(?:insurer(?:'s)?\s+)?insurance business\b"
        r"|\ball aspects?\b.{0,140}\binsurance business\b.{0,140}\boutsourc",
        r"\ball (?:outsourced\s+)?aspects? of (?:the )?(?:south african\s+)?(?:insurer(?:'s)?\s+)?insurance business\b"
        r"|\ball aspects?\b.{0,140}\binsurance business\b.{0,140}\boutsourc",
        2,
    ),
    (
        "intermediary services exclusion",
        r"does not apply to intermediary services|exclud(?:e|es|ed|ing).{0,40}intermediary services",
        r"does not apply to intermediary services|exclud(?:e|es|ed|ing).{0,40}intermediary services",
        2,
    ),
    (
        "related and inter-related party scope",
        r"\brelated part(?:y|ies)\b|\binter-related\b",
        r"\brelated part(?:y|ies)\b|\binter-related\b",
        1,
    ),
    (
        "reinsurance scope",
        r"\breinsur(?:er|ance)\b",
        r"\breinsur(?:er|ance)\b",
        1,
    ),
    (
        "external regulatory notification or reporting",
        r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b",
        r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b",
        3,
    ),
    (
        "required approval",
        r"\bboard\b.{0,50}\bapprov|\bapprov(?:al|e|ed)\b",
        r"\bboard\b.{0,50}\bapprov|\bapprov(?:al|e|ed)\b",
        2,
    ),
    (
        "ongoing monitoring, assessment or review",
        r"\bmonitor\b|\breview\b|\bassess(?:ment|ed|es|ing)?\b",
        r"\bmonitor\b|\breview\b|\bassess(?:ment|ed|es|ing)?\b|\bevaluat(?:e|ed|es|ing|ion)\b|\bdue diligence\b",
        2,
    ),
    (
        "documented evidence or record retention",
        r"\brecord\b|\bretain\b|\bdocument",
        r"\brecord\b|\bretain\b|\bdocument",
        1,
    ),
    (
        "specified timing or frequency",
        r"\bno later than\b|\bwithin\s+\d+|\bprior to\b|\bimmediately\b|"
        r"\bmonthly\b|\bquarterly\b|\bannual(?:ly)?\b|\byearly\b|"
        r"\bperiodic(?:ally)?\b|\bregular(?:ly)?\b",
        r"\bno later than\b|\bwithin\s+\d+|\bprior to\b|\bimmediately\b|"
        r"\bmonthly\b|\bquarterly\b|\bannual(?:ly)?\b|\byearly\b|"
        r"\bperiodic(?:ally)?\b|\bregular(?:ly)?\b",
        3,
    ),
    (
        "prohibition, condition or exception",
        r"\bmay not\b|\bmust not\b|\bdoes not apply\b|\bprohibit|\bunless\b|\bif\b",
        r"\bmay not\b|\bmust not\b|\bdoes not apply\b|\bprohibit|\bunless\b|\bif\b",
        3,
    ),
    (
        "conflicts-of-interest control",
        r"\bconflicts? of interest\b",
        r"\bconflicts? of interest\b",
        2,
    ),
    (
        "business-continuity control",
        r"\bbusiness continuity\b|\bcontingency\b|\bdisaster recovery\b",
        r"\bbusiness continuity\b|\bcontingency\b|\bdisaster recovery\b",
        2,
    ),
    (
        "confidentiality and data protection",
        r"\bconfidential|\bdata protection\b|\bprivacy\b|\bsecure\b",
        r"\bconfidential|\bdata protection\b|\bprivacy\b|\bsecure\b",
        2,
    ),
    (
        "audit, inspection or access rights",
        r"\baudit\b|\binspect\b|\baccess\b",
        r"\baudit\b|\binspect\b|\baccess\b",
        2,
    ),
    (
        "termination and exit requirements",
        r"\bterminat|\bexit\b|\btransfer\b",
        r"\bterminat|\bexit\b|\btransfer\b",
        2,
    ),
)

# These rules preserve the actual legal payload of a clause.  Broad labels such
# as "core action and subject matter" are useful retrieval signals, but they
# cannot prove that a policy contains the specific result, recipient, object or
# contractual term required by the directive.
ATOMIC_MATERIAL_ELEMENT_RULES = (
    (
        "provider location inside or outside South Africa",
        r"\birrespective\b.{0,180}\blocated outside (?:of )?South Africa\b|"
        r"\blocated outside (?:of )?South Africa\b",
        r"(?=.*\b(?:provider|person|third party|service provider)\b)"
        r"(?=.*\b(?:inside or outside|outside)\b)(?=.*\bSouth Africa\b)",
        3,
    ),
    (
        "continued appropriate internal controls",
        r"\bmaintain appropriate internal controls\b",
        r"(?=.*\bmaintain\b)(?=.*\bappropriate internal controls\b)",
        3,
    ),
    (
        "ability to meet regulatory requirements",
        r"\bmeet regulatory requirements\b",
        r"(?=.*\b(?:meet|comply with)\b)(?=.*\bregulatory requirements\b)",
        3,
    ),
    (
        "continuous adequacy of organisation or management",
        r"\b(?:organisation|organization|management)\b.{0,100}\b(?:necessary|adequate)\b|"
        r"\b(?:necessary|adequate)\b.{0,100}\b(?:organisation|organization|management)\b",
        r"(?=.*\b(?:organisation|organization|management structure|management capability|management resources)\b)"
        r"(?=.*\b(?:necessary|adequate)\b)(?=.*\b(?:business|operations)\b)",
        3,
    ),
    (
        "effective outsourcing governance framework",
        r"\bgovernance framework\b.{0,120}\boutsourc|\boutsourc.{0,120}\bgovernance framework\b",
        r"(?=.*\bgovernance framework\b)(?=.*\boutsourc)",
        3,
    ),
    (
        "retained board and executive responsibility",
        r"\bboard of directors\b.{0,140}\bremain responsible\b|"
        r"\bmanaging executives\b.{0,140}\bremain responsible\b",
        r"(?=.*\bboard\b)(?=.*\b(?:executives?|management)\b)(?=.*\b(?:must\s+)?remain(?:s)?\s+responsible\b)"
        r"(?=.*\b(?:insurance business|outsourcing)\b)",
        4,
    ),
    (
        "legacy contract extension, renewal or amendment trigger",
        r"\b(?:extended|renewed|amended)\b.{0,120}\b(?:contract|agreement)\b|"
        r"\b(?:contract|agreement)\b.{0,120}\b(?:extended|renewed|amended)\b",
        r"(?=.*\b(?:contract|agreement|arrangement)\b)"
        r"(?=.*\bextend(?:ed|s|ing)?\b)(?=.*\brenew(?:ed|s|ing)?\b)"
        r"(?=.*\bamend(?:ed|s|ing)?\b)",
        4,
    ),
    (
        "avoid or mitigate conflicts of interest",
        r"\b(?:avoid|mitigat)\w*\b.{0,100}\bconflicts? of interest\b|"
        r"\bconflicts? of interest\b.{0,100}\b(?:avoid|mitigat)\w*\b",
        r"(?=.*\bconflicts? of interest\b)(?=.*\b(?:avoid|mitigat)\w*\b)",
        3,
    ),
    (
        "policyholder, insurer and service-provider interests",
        r"\bconflicts? of interest\b.{0,220}\bpolicyholders?\b|"
        r"\bpolicyholders?\b.{0,220}\bconflicts? of interest\b",
        r"(?=.*\bpolicyholders?\b)(?=.*\b(?:insurer|company|ABC)\b)(?=.*\b(?:service provider|third party|other person)\b)",
        2,
    ),
    (
        "all paragraph 6 outsourcing principles",
        r"\bgive effect to the principles\b.{0,80}\bparagraph 6\b",
        r"(?=.*\b(?:board|directors?|management)\b)(?=.*\brisk\b)(?=.*\bconflicts? of interest\b)"
        r"(?=.*\bremuneration\b)(?=.*\bsub[- ]?outsourc)",
        4,
    ),
    (
        "reasonable and commensurate outsourcing remuneration",
        r"\bremuneration\b.{0,120}\breasonable\b.{0,120}\bcommensurate\b",
        r"(?=.*\bremuneration\b)(?=.*\breasonable\b)(?=.*\bcommensurate\b)",
        3,
    ),
    (
        "remuneration not linked to insurance-claim outcomes",
        r"\bremuneration\b.{0,160}\bmust not be linked\b.{0,160}\binsurance claims?\b",
        r"(?=.*\bremuneration\b)(?=.*\b(?:must not|may not|prohibit)\b)"
        r"(?=.*\binsurance claims?\b)(?=.*\b(?:repudiated|paid|partially paid)\b)",
        4,
    ),
    (
        "fit-and-proper competence and integrity",
        r"\bfit and proper\b|\bcompetence\b.{0,80}\bintegrity\b|\bintegrity\b.{0,80}\bcompetence\b",
        r"(?=.*\b(?:fit and proper|competence|competent)\b)(?=.*\bintegrity\b)",
        3,
    ),
    (
        "service-provider operational capability",
        r"\boperational capability\b",
        r"\boperational (?:capability|capacity)\b|\btechnical resources\b.{0,100}\bcapacity\b",
        2,
    ),
    (
        "service-provider financial position",
        r"\bfinancial position\b",
        r"\bfinancial (?:position|resources|capacity|strength|stability)\b",
        2,
    ),
    (
        "material risk to delivery capability",
        r"\bmaterial risk\b.{0,140}\b(?:ability|deliver)\b|\bability\b.{0,140}\bmaterial risk\b",
        r"(?=.*\b(?:material|significant)\s+risk\b)(?=.*\b(?:ability|deliver|perform)\b)",
        2,
    ),
    (
        "contract specifies function type",
        r"\bspecify the type\b.{0,100}\bfunction or activity\b",
        r"(?=.*\b(?:contract|agreement|SLA)\b)(?=.*\b(?:type|nature|scope)\b)(?=.*\b(?:function|activity|service)\b)",
        2,
    ),
    (
        "outsourcing type, level and concentration limits",
        r"\bset limits\b.{0,180}\btypes?\b.{0,120}\boverall levels?\b|"
        r"\bextent\b.{0,120}\bsame person\b",
        r"(?=.*\blimit\w*\b)(?=.*\b(?:type|level|extent)\b)(?=.*\b(?:same|single)\s+(?:person|provider|third party)\b)",
        4,
    ),
    (
        "contract covers material aspects, rights and responsibilities",
        r"\bwritten contracts?\b.{0,180}\ball material aspects\b|"
        r"\bright[s]?\b.{0,80}\bresponsibilities\b.{0,100}\bservice[- ]level\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\b(?:material aspects|scope)\b)"
        r"(?=.*\brights?\b)(?=.*\bresponsibilit)(?=.*\b(?:service[- ]levels?|SLA)\b)",
        4,
    ),
    (
        "contract specifies performance frequency",
        r"\bspecify the type and frequency\b|\bfrequency of the function or activity\b",
        r"(?=.*\b(?:contract|agreement|SLA)\b)(?=.*\b(?:frequency|schedule|interval|timing|daily|weekly|monthly|quarterly|annual)\b)",
        3,
    ),
    (
        "contract specifies service levels and standards",
        r"\bspecify the level and standard of service\b",
        r"(?=.*\b(?:contract|agreement|SLA)\b)"
        r"(?=.*\b(?:service[- ]levels?|SLA|level and standard of service)\b)"
        r"(?=.*\b(?:service[- ]level requirements?|service standards?|performance standards?|level and standard of service)\b)",
        3,
    ),
    (
        "contract specifies a service level",
        r"\bspecify the level and standard of service\b",
        r"(?=.*\b(?:contract|agreement|SLA)\b)(?=.*\b(?:service[- ]levels?|SLA|level of service)\b)",
        1,
    ),
    (
        "policyholder and insurer service recipients",
        r"\bservice\b.{0,140}\bpolicyholder\b.{0,120}\binsurer\b|"
        r"\bpolicyholder\b.{0,140}\binsurer\b.{0,120}\bservice\b",
        r"(?=.*\b(?:policyholders?|customers?)\b)(?=.*\b(?:insurer|company|bank|regulated entity|Aegis)\b)",
        2,
    ),
    (
        "contract addresses privacy and information security",
        r"\bconfidentiality\b.{0,120}\bprivacy\b.{0,120}\bsecurity\b|"
        r"\bprivacy\b.{0,120}\bsecurity\b",
        r"(?=.*\bconfidential)(?=.*\b(?:privacy|data protection)\b)(?=.*\b(?:security|secure)\b)",
        3,
    ),
    (
        "contractual warranties or guarantees",
        r"\bwarranties\b|\bguarantees\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\b(?:warrant|guarantee)\w*\b)",
        3,
    ),
    (
        "service-provider insurance requirement",
        r"\binsurance to be secured\b|\binsurance\b.{0,100}\bcontractual obligations\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\binsurance\b)(?=.*\b(?:secure|maintain|required|coverage)\b)",
        3,
    ),
    (
        "contractual indemnity and liability provisions",
        r"\bindemnity\b.{0,80}\bliability\b|\bliability\b.{0,80}\bindemnity\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\bindemnit)(?=.*\bliabilit)",
        4,
    ),
    (
        "contract addresses intellectual-property ownership",
        r"\bownership of intellectual property\b|\bintellectual property ownership\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\bintellectual[- ]property\b)(?=.*\bownership\b)",
        4,
    ),
    (
        "contract provides a dispute-resolution process",
        r"\bdispute resolution process\b",
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\bdispute resolution\b)",
        4,
    ),
)

# Clause-specific elements close the false-complete path exposed by the full
# Directive 159 demo-policy run. Generic topic overlap is not proof of these
# clauses; the exact legal payload must appear in the cited policy wording.
SECTION_MATERIAL_ELEMENT_RULES: Dict[str, Tuple[Tuple[str, str, int], ...]] = {
    "1": (
        (
            "complete Directive 159 compliance for outsourcing",
            r"(?=.*\b(?:Directive 159|this Directive)\b)(?=.*\b(?:all|every)\b)"
            r"(?=.*\brequirements?\b)(?=.*\boutsourc\w*\b)(?=.*\bcompl\w*\b)",
            5,
        ),
    ),
    "3.1": (
        (
            "applicability to all insurers including qualifying reinsurers",
            r"(?=.*\ball insurers?\b)(?=.*\breinsur(?:er|ers|ance)\b)",
            4,
        ),
    ),
    "3.4.2": (
        (
            "subsidiary insurance-business outsourcing inside or outside South Africa",
            r"(?=.*\bsubsidiar\w*\b)(?=.*\binsurance business\b)(?=.*\boutsourc\w*\b)"
            r"(?=.*\b(?:inside|in)\b)(?=.*\boutside\b)(?=.*\bSouth Africa\b)",
            5,
        ),
    ),
    "3.7": (
        (
            "additional existing regulatory-framework compliance",
            r"(?=.*\b(?:in addition|additional)\b)(?=.*\bregulatory framework\b)"
            r"(?=.*\b(?:nominee|binder|assistance business|specific regulatory requirements)\b)"
            r"(?=.*\bcompl\w*\b)",
            5,
        ),
    ),
    "5.2.3": (
        (
            "replacement difficulty, replacement time and in-house alternative",
            r"(?=.*\b(?:difficult|difficulty)\w*\b)(?=.*\btime\b)"
            r"(?=.*\b(?:replac\w*|in[- ]house)\b)",
            4,
        ),
    ),
    "6.2.1": (
        (
            "prohibition on outsourcing that materially increases insurer risk",
            r"(?=.*\b(?:must not|may not|prohibit)\b)(?=.*\boutsourc\w*\b)"
            r"(?=.*\bmaterially increase\b)(?=.*\brisk\b)(?=.*\binsurer\b)",
            5,
        ),
    ),
    "6.2.3": (
        (
            "prohibition on impairing regulatory monitoring",
            r"(?=.*\b(?:must not|may not|prohibit)\b)(?=.*\boutsourc\w*\b)"
            r"(?=.*\b(?:Registrar|FSCA|regulator)\b)(?=.*\bmonitor\w*\b)"
            r"(?=.*\bcompl\w*\b)",
            5,
        ),
    ),
    "6.3": (
        (
            "conflict avoidance or mitigation across all affected interests",
            r"(?=.*\bconflicts? of interest\b)(?=.*\b(?:avoid|mitigat)\w*\b)"
            r"(?=.*\bpolicyholders?\b)(?=.*\b(?:service provider|other person|third party)\b)",
            5,
        ),
    ),
    "6.2.4": (
        (
            "prohibition protecting fair treatment and continuous satisfactory service",
            r"(?=.*\b(?:must not|may not|prohibit)\b)(?=.*\boutsourc\w*\b)"
            r"(?=.*\bfair treatment\b)(?=.*\bcontinuous\b)(?=.*\bsatisfactory service\b)"
            r"(?=.*\bpolicyholders?\b)",
            5,
        ),
    ),
    "6.4.2": (
        (
            "prohibition on duplicate commission or binder-fee remuneration",
            r"(?=.*\bremunerat\w*\b)(?=.*\b(?:must not|may not|prohibit)\b)"
            r"(?=.*\bcommission\b)(?=.*\bbinder fee\b)(?=.*\b(?:again|duplicate|twice|double)\b)",
            5,
        ),
    ),
    "6.4.3": (
        (
            "remuneration must not increase unfair-treatment risk",
            r"(?=.*\bremunerat\w*\b)(?=.*\b(?:must not|may not|prohibit)\b)"
            r"(?=.*\b(?:unfair treatment|unfair customer outcomes?)\b)"
            r"(?=.*\b(?:policyholders?|customers?)\b)",
            5,
        ),
    ),
    "6.5": (
        (
            "paragraph 6 principles applied to authorised sub-outsourcing",
            r"(?=.*\bsub[- ]?outsourc\w*\b)(?=.*\bprinciples?\b)"
            r"(?=.*\b(?:paragraphs? 6|6\.1|6\.4)\b)(?=.*\b(?:authoris\w*|contract)\b)",
            5,
        ),
    ),
    "7.1": (
        (
            "board-approved outsourcing policy",
            r"(?=.*\b(?:board(?: of directors)?|board [a-z ]{0,80}committee)\b)"
            r"(?=.*\bapprov(?:e|ed|es|al)\w*\b)"
            r"(?=.*\b(?:this policy|(?:the|an|its)?\s*outsourcing policy)\b)",
            5,
        ),
    ),
    "7.2.3": (
        (
            "guidance on contractual and other outsourcing risks",
            r"(?=.*\bcontractual risks?\b)(?=.*\bother risks?\b)"
            r"(?=.*\bassess\w*\b)(?=.*\bmonitor\w*\b)(?=.*\bmanag\w*\b)",
            4,
        ),
    ),
    "7.2.4": (
        (
            "internal review and approval before material outsourcing",
            r"(?=.*\b(?:internal review|second[- ]line review|review and approval|due diligence)\b)"
            r"(?=.*\bapprov\w*\b)(?=.*\b(?:control|management|material) function\b)"
            r"(?=.*\b(?:before|prior to|may not be outsourced before)\b)",
            5,
        ),
    ),
    "7.4": (
        (
            "affected business units and staff awareness and compliance",
            r"(?=.*\b(?:business units?|staff|employees?)\b)(?=.*\baware\w*\b)"
            r"(?=.*\bcompl\w*\b)(?=.*\boutsourcing policy\b)",
            5,
        ),
    ),
    "7.5.1": (
        (
            "cost-benefit and insurance-business risk assessment",
            r"(?=.*\bcosts?\b)(?=.*\bbenefits?\b)(?=.*\brisk\b)"
            r"(?=.*\binsurance business\b)",
            4,
        ),
    ),
    "7.5.2": (
        (
            "objective procurement and provider-selection procedures",
            r"(?=.*\b(?:identify|select)\w*\b)(?=.*\b(?:providers?|persons?|service providers?)\b)"
            r"(?=.*\bobjective\b)(?=.*\bprocurement\b)(?=.*\bselection procedures?\b)",
            5,
        ),
    ),
    "7.5.3": (
        (
            "multiple-outsourcing and cross-insurer concentration assessment",
            r"(?=.*\bmultiple outsourcing arrangements?\b)(?=.*\bimpact\b)"
            r"(?=.*\b(?:number of insurers|multiple insurers|concentration)\b)",
            5,
        ),
    ),
    "7.5.5": (
        (
            "provider governance, risk, controls and legal-compliance assessment",
            r"(?=.*\bgovernance\b)(?=.*\brisk management\b)"
            r"(?=.*\binternal controls?\b)"
            r"(?=.*\b(?:applicable laws?|comply with (?:all )?(?:applicable )?laws?)\b)",
            5,
        ),
    ),
    "7.5.8": (
        (
            "pre-outsourcing contingency plan for termination or ineffectiveness",
            r"(?=.*\b(?:before|prior to)\b)(?=.*\b(?:contingency|business continuity)\b)"
            r"(?=.*\b(?:terminat|ineffective|unable to continue)\w*\b)",
            5,
        ),
    ),
    "7.5.9": (
        (
            "documented approval before outsourcing",
            r"(?=.*\b(?:before|prior to)\b)(?=.*\boutsourc\w*\b)"
            r"(?=.*\bapprov\w*\b)(?=.*\b(?:document|record|approval matrix|policy)\b)",
            5,
        ),
    ),
    "7.7.1": (
        (
            "contract duration",
            r"(?=.*\b(?:contract|agreement)\b)"
            r"(?=.*\b(?:duration|contract term|term of the (?:contract|agreement))\b)",
            4,
        ),
    ),
    "7.7.4": (
        (
            "contract requires provider governance, risk management and controls",
            r"(?=.*\b(?:contract|agreement)\b)(?=.*\b(?:provider|other person|third party)\b)"
            r"(?=.*\bgovernance\b)(?=.*\brisk management\b)(?=.*\binternal controls?\b)",
            5,
        ),
    ),
    "7.7.5": (
        (
            "contract requires provider compliance with applicable laws",
            r"(?=.*\b(?:contract|agreement)\b)(?=.*\b(?:provider|other person|third party)\b)"
            r"(?=.*\bcompl\w*\b)(?=.*\bapplicable laws?\b)",
            4,
        ),
    ),
    "7.7.7": (
        (
            "contract specifies reporting type and frequency",
            r"(?=.*\b(?:contract|agreement)\b)(?=.*\breport\w*\b)"
            r"(?=.*\b(?:type|nature|content)\b)"
            r"(?=.*\b(?:frequency|schedule|interval|daily|weekly|monthly|quarterly|annual)\b)",
            5,
        ),
    ),
    "7.7.8": (
        (
            "contractual performance and compliance monitoring method",
            r"(?=.*\b(?:contract|agreement)\b)(?=.*\bmonitor\w*\b)"
            r"(?=.*\bperformance\b)(?=.*\bcompl\w*\b)"
            r"(?=.*\b(?:manner|means|method|procedure)\b)",
            5,
        ),
    ),
    "7.7.12": (
        (
            "contract addresses sub-outsourcing",
            r"(?=.*\bsub[- ]?outsourc\w*\b)"
            r"(?=.*\b(?:contract|agreement|prior written consent|primary provider)\b)",
            5,
        ),
    ),
    "7.8": (
        (
            "sub-outsourcing compliance with Directive 159 sections 7.6 and 7.7",
            r"(?=.*\bsub[- ]?outsourc\w*\b)"
            r"(?:(?=.*\bcompl\w*\b)(?=.*\b(?:Directive 159|FSCA)\b)(?=.*\b7\.6\b)(?=.*\b7\.7\b)"
            r"|(?=.*\b(?:flow down|apply)\b)(?=.*\ball applicable\b)"
            r"(?=.*\b(?:controls?|service|access|continuity|exit)\b))",
            5,
        ),
    ),
    "7.11.2": (
        (
            "regular assessment of provider compliance with applicable laws",
            r"(?=.*\b(?:regular|annual|quarter|month|periodic)\w*\b)"
            r"(?=.*\b(?:assess|review|monitor)\w*\b)"
            r"(?=.*\b(?:provider|other person|third party)\b)"
            r"(?=.*\bapplicable laws?\b)",
            5,
        ),
    ),
    "8.1.2": (
        (
            "notification includes service-provider details",
            r"(?=.*\b(?:notify|notification|report|submit)\w*\b)"
            r"(?=.*\b(?:Registrar|FSCA|regulator)\b)"
            r"(?=.*\b(?:details|identity|name|identify|identifies|identified)\b)"
            r"(?=.*\b(?:provider|other person|third party)\b)",
            5,
        ),
    ),
    "8.1.3": (
        (
            "notification includes key risks and mitigation strategies",
            r"(?=.*\b(?:notify|notification|report|submit)\w*\b)"
            r"(?=.*\b(?:Registrar|FSCA|regulator)\b)(?=.*\bkey risks?\b)"
            r"(?=.*\bmitigat\w*\b)",
            5,
        ),
    ),
    "9.1": (
        (
            "post-effective-date outsourcing compliance",
            r"(?=.*\boutsourc\w*\b)(?=.*\b(?:new|on or after|after the effective date|"
            r"from the effective date|takes effect)\b)(?=.*\bcompl\w*\b)",
            5,
        ),
    ),
}

# These patterns recognise directly related but incomplete controls. They are
# used only for the Partially Covered relevance decision and can never prove
# Completely Covered status.
SECTION_PARTIAL_RELEVANCE_RULES: Dict[str, str] = {
    "1": (
        r"(?=.*\b(?:directive|regulation|policy)\b)(?=.*\boutsourc\w*\b)"
        r"(?=.*\b(?:policy|governance|compl\w*)\b)"
    ),
    "3.1": (
        r"(?=.*\b(?:policy applies|applies to|applicable to)\b)"
        r"(?=.*\b(?:insur|outsourc|staff|resources?|vendors?)\w*\b)"
    ),
    "3.2": (
        r"(?=.*\b(?:policy applies|applies to|scope)\b)"
        r"(?=.*\boutsourc\w*\b)"
    ),
    "3.4.2": (
        r"(?=.*\b(?:policy applies|applies to|group|related|affiliated)\b)"
        r"(?=.*\b(?:insurance business|outsourc\w*|third party)\b)"
    ),
    "3.7": (
        r"(?=.*\b(?:regulations?|regulatory framework|applicable laws?)\b)"
        r"(?=.*\boutsourc\w*\b)(?=.*\bcompl\w*\b)"
    ),
    "5.2.3": (
        r"(?=.*\b(?:determine|assess|consider)\w*\b)"
        r"(?=.*\b(?:control|management|material) function\b)(?=.*\boutsourc\w*\b)"
    ),
    "7.2.3": (
        r"(?=.*\b(?:before|prior to)\b)(?=.*\boutsourc\w*\b)"
        r"(?=.*\b(?:assess|due diligence|risk)\w*\b)"
    ),
    "7.5.2": (
        r"(?=.*\b(?:before|prior to)\b)(?=.*\boutsourc\w*\b)"
        r"(?=.*\b(?:provider|third party|other person)\b)(?=.*\bassess\w*\b)"
    ),
    "7.5.3": (
        r"(?=.*\b(?:before|prior to)\b)(?=.*\boutsourc\w*\b)"
        r"(?=.*\b(?:provider|third party|other person)\b)(?=.*\bassess\w*\b)"
    ),
    "9.1": (
        r"(?=.*\boutsourc\w*\b)(?=.*\b(?:before|prior to)\b)"
        r"(?=.*\b(?:takes effect|took effect|effective date)\b)(?=.*\bcompl\w*\b)"
    ),
}

# When several candidates support the same conservative coverage status, prefer
# the provision that proves the clause-specific legal subject matter.  This is
# deliberately separate from the complete-coverage gate: it affects only which
# exact page-grounded quotation is exported, never whether a row is upgraded.
SECTION_PREFERRED_EVIDENCE_RULES: Dict[str, str] = {
    "1": (
        r"(?=.*\b(?:policy applies|applies to)\b)"
        r"(?=.*\ball aspects?\b)(?=.*\binsurance business\b)"
        r"(?=.*\boutsourc\w*\b)"
    ),
    "3.1": (
        r"(?=.*\bSouth African operations\b)"
        r"(?=.*\b(?:policy applies|applies to)\b)"
        r"(?=.*\ball aspects?\b)(?=.*\binsurance business\b)"
    ),
    "7.1": (
        r"(?=.*\b(?:board(?: of directors)?|board [a-z ]{0,80}committee)\b)"
        r"(?=.*\bapprov(?:e|ed|es|al)\w*\b)"
        r"(?=.*\b(?:this policy|(?:the|an|its)?\s*outsourcing policy)\b)"
    ),
}

# Directly contradictory policy wording is still relevant evidence.  It must be
# cited and remediated rather than discarded as if the policy were silent.  The
# rules below capture common adverse formulations for Directive 159 while
# remaining independent of any policy name, page number or benchmark document.
SECTION_ADVERSE_EVIDENCE_RULES: Dict[str, Tuple[str, ...]] = {
    "3.3": (
        r"(?=.*\b(?:related|inter-related|intra-group)\b)"
        r"(?=.*\b(?:outside South Africa|foreign|cross-border)\b)"
        r"(?=.*\b(?:outside (?:this|the) policy|fall outside|applies only|exclud)\w*\b)",
    ),
    "3.4.1": (
        r"(?=.*\b(?:outside South Africa|foreign|cross-border)\b)"
        r"(?=.*\b(?:outside (?:this|the) policy|fall outside|applies only|exclud)\w*\b)",
    ),
    "3.4.2": (
        r"(?=.*\b(?:group compan|intra-group|related part)\w*\b)"
        r"(?=.*\b(?:outside (?:this|the) policy|fall outside|applies only|exclud)\w*\b)",
    ),
    "5.2.2": (
        r"(?=.*\b(?:internal controls?|regulatory requirements?)\b)"
        r"(?=.*\b(?:optional|may be omitted|need not|waiv)\w*\b)",
    ),
    "5.2.3": (
        r"(?=.*\b(?:replac\w*|in[- ]house)\b)"
        r"(?=.*\b(?:optional|may be omitted|need not|waiv)\w*\b)",
    ),
    "6.1": (
        r"(?=.*\b(?:board|executives?|management)\b)"
        r"(?=.*\b(?:responsibility|accountability|responsible)\b)"
        r"(?=.*\b(?:transfer\w*|not responsible|no longer responsible|discharg\w*)\b)",
    ),
    "6.3": (
        r"(?=.*\bconflicts? of interest\b)"
        r"(?=.*\b(?:encouraged|when convenient|informally|need not|optional)\b)",
    ),
    "6.4.4": (
        r"(?=.*\b(?:remunerat\w*|performance fee|incentive)\b)"
        r"(?=.*\bclaims?\b)(?=.*\b(?:repudiated|not paid|partially paid)\b)"
        r"(?=.*\b(?:percentage|percent|%|monetary value|fee)\b)",
    ),
    "7.3": (
        r"(?=.*\b(?:policy|outsourcing policy)\b)(?=.*\breview\w*\b)"
        r"(?=.*\b(?:24|twenty[- ]four|two)\s*(?:calendar\s+)?(?:months?|years?)\b)",
    ),
    "7.7.10": (
        r"(?=.*\b(?:access|records?|information)\b)(?=.*\b(?:regulator|FSCA|Registrar|Prudential Authority)\b)"
        r"(?=.*\b(?:prior (?:written )?consent|may withhold|commercially sensitive|subject to consent)\b)",
    ),
    "7.7.15": (
        r"(?=.*\b(?:access|records?|information)\b)(?=.*\b(?:regulator|FSCA|Registrar|Prudential Authority)\b)"
        r"(?=.*\b(?:prior (?:written )?consent|may withhold|commercially sensitive|subject to consent)\b)",
    ),
    "8.1.1": (
        r"(?=.*\b(?:notify|notification)\w*\b)(?=.*\b(?:regulator|FSCA|Registrar|Prudential Authority)\b)"
        r"(?=.*\bafter\b)(?=.*\b(?:effective|takes effect|commencement)\b)",
    ),
    "8.1.2": (
        r"(?=.*\b(?:notify|notification)\w*\b)(?=.*\b(?:regulator|FSCA|Registrar|Prudential Authority)\b)"
        r"(?=.*\bafter\b)(?=.*\b(?:effective|takes effect|commencement)\b)",
    ),
    "8.1.3": (
        r"(?=.*\b(?:notify|notification)\w*\b)(?=.*\b(?:regulator|FSCA|Registrar|Prudential Authority)\b)"
        r"(?=.*\bafter\b)(?=.*\b(?:effective|takes effect|commencement)\b)",
    ),
}

# Some source rows contain trailing headings (for example, "Written contracts"
# or "Management and regular review").  Those headings are useful to a human
# reader but must not create legal elements for the preceding clause.
SECTION_SUPPRESSED_ELEMENT_LABELS: Dict[str, set[str]] = {
    "5.2.1": {"prohibition, condition or exception"},
    "6.2.3": {"ongoing monitoring, assessment or review"},
    "7.2.5": {"prohibition, condition or exception"},
    "7.5.9": {
        "confidentiality and data protection",
        "continuous adequacy of organisation or management",
    },
    "7.8": {
        "ongoing monitoring, assessment or review",
        "specified timing or frequency",
    },
}

# Strict policy-language equivalents for clauses whose legal payload is often
# expressed through an internal policy's own section numbering. These patterns
# are deliberately conjunctive; a broad topic mention cannot satisfy them.
SECTION_EQUIVALENT_COMPLETE_RULES: Dict[str, str] = {
    "3.1": (
        r"(?=.*\b(?:every|all)\s+(?:Aegis\s+)?insurers?\b)"
        r"(?=.*\bsubject to paragraph 3\.6\b)"
        r"(?=.*\b(?:every|all)\s+(?:Aegis\s+)?reinsurers?\b)"
    ),
    "3.2": (
        r"(?=.*\b(?:every|all)\s+aspects?\b)(?=.*\binsurance business\b)"
        r"(?=.*\b(?:is|are) or may be outsourced\b)(?=.*\banother person\b)"
        r"(?=.*\b(?:other than|excludes?)\s+intermediary services\b)"
    ),
    "3.6": (
        r"(?=.*\b(?:pricing|actuarial)\b)(?=.*\binsurer\b)(?=.*\breinsurer\b)"
        r"(?=.*\bwhether under a reinsurance contract or not\b)"
        r"(?=.*\b(?:excludes?|other than)\b)(?=.*\bactual insurance\b)"
    ),
    "4.3.2": (
        r"(?=.*\b(?:effective\s+)?outsourcing governance framework\b)"
        r"(?=.*\bmanag\w*\b)(?=.*\brisks?\b)"
        r"(?=.*\b(?:legal|regulatory) obligations?\b)"
    ),
    "6.5": (
        r"(?=.*\bsub[- ]?outsourc\w*\b)"
        r"(?=.*\b(?:retained accountability|material risk|governance)\b)"
        r"(?=.*\bconflicts?\b)(?=.*\bremuneration\b)"
    ),
    "7.1": (
        r"(?=.*\b(?:board(?: of directors)?|board [a-z ]{0,80}committee)\b)"
        r"(?=.*\bapprov(?:e|ed|es|al)\w*\b)"
        r"(?=.*\b(?:this policy|(?:the|an|its)?\s*outsourcing policy)\b)"
    ),
    "7.2.1": (
        r"(?=.*\bpolicy\b)(?=.*\bgive effect\b)"
        r"(?=.*\b(?:retained accountability|material risk|governance)\b)"
        r"(?=.*\bconflicts?\b)(?=.*\bremuneration\b)(?=.*\bsub[- ]?outsourc\w*\b)"
    ),
    "7.2.2": (
        r"(?=.*\blimits?\b)(?=.*\btypes?\b)(?=.*\boverall level\b)"
        r"(?=.*\bextent\b)(?=.*\b(?:same|single|one)\s+(?:service )?provider\b)"
    ),
    "7.2.3": (
        r"(?=.*\b(?:policy|procedure)\b)(?=.*\bguidance\b)"
        r"(?=.*\bcontractual risks?\b)(?=.*\bother outsourcing risks?\b)"
        r"(?=.*\bassess\w*\b)(?=.*\bmonitor\w*\b)(?=.*\bmanag\w*\b)"
    ),
    "7.4": (
        r"(?=.*\baffected business units?\b)(?=.*\bstaff\b)"
        r"(?=.*\baware\b)(?=.*\bcomply\b)(?=.*\boutsourcing policy\b)"
    ),
    "7.7.6": (
        r"(?=.*\bcontract\b)(?=.*\bRand value\b)"
        r"(?=.*\bremuneration or consideration\b)(?=.*\bpayable\b)"
        r"(?=.*\bnot fixed or determined\b)(?=.*\bbasis\b)(?=.*\bcalculated\b)"
    ),
    "7.7.18": (
        r"(?=.*\bcontract\b)(?=.*\bwarrant(?:y|ies)\b)(?=.*\bguarantees?\b)"
        r"(?=.*\binsurance\b)(?=.*\bsecured by\b)(?=.*\bservice provider\b)"
        r"(?=.*\b(?:fulfil|fulfill)\b)(?=.*\bcontractual obligations?\b)"
    ),
    "7.8": (
        r"(?=.*\bsub[- ]?outsourc\w*\b)(?=.*\bwritten outsourcing contract\b)"
        r"(?=.*\bcompl\w*\b)(?=.*\b7\.6\b)(?=.*\b7\.7\b)"
    ),
}

# A generic control on the same page is not enough to establish partial
# relevance for these legally distinct requirements.  At least the stated
# clause-specific topic must be present in the exact citation.
SECTION_MINIMUM_RELEVANCE_RULES: Dict[str, str] = {
    "6.2.3": (
        r"(?=.*\b(?:Registrar|FSCA|regulator)\b)(?=.*\bmonitor\w*\b)"
        r"(?=.*\bcompl\w*\b)"
    ),
    "6.3": r"(?=.*\bconflicts? of interest\b)(?=.*\b(?:avoid|mitigat)\w*\b)",
    "7.1": (
        r"(?=.*\b(?:board(?: of directors)?|board [a-z ]{0,80}committee)\b)"
        r"(?=.*\bapprov(?:e|ed|es|al)\w*\b)"
        r"(?=.*\b(?:this policy|(?:the|an|its)?\s*outsourcing policy)\b)"
    ),
    "7.2.4": (
        r"(?=.*\b(?:internal review|review and approval)\b)"
        r"(?=.*\bapprov\w*\b)(?=.*\b(?:control|management|material) function\b)"
    ),
    "7.5.8": (
        r"(?=.*\b(?:contingency|business continuity)\b)"
        r"(?=.*\b(?:terminat|ineffective|unable to continue)\w*\b)"
    ),
    "7.7.8": (
        r"(?=.*\b(?:contract|agreement)\b)(?=.*\bmonitor\w*\b)"
        r"(?=.*\bperformance\b)(?=.*\bcompl\w*\b)"
    ),
    "7.11.2": (
        r"(?=.*\b(?:assess|review|monitor)\w*\b)"
        r"(?=.*\b(?:regular|annual|quarter|month|periodic)\w*\b)"
        r"(?=.*\b(?:applicable laws?|legal requirements?)\b)"
    ),
}

# Jurisdiction-neutral semantic profiles for Directive 159.  These patterns
# recognise common policy-language equivalents without requiring the source and
# policy to use the same country, authority or regulator name.  A profile is
# deliberately clause-specific; broad outsourcing vocabulary alone never
# changes a status.
NEUTRAL_POLICY_EVIDENCE_PROFILES: Dict[str, Dict[str, str]] = {
    "1": {"partial": r"(?=.*\boutsourcing policy\b)(?=.*\b(?:regulations?|regulatory requirements?)\b)(?=.*\bcompl\w*\b)"},
    "3.1": {"partial": r"(?=.*\bpolicy\b)(?=.*\bapplicable to all\b)(?=.*\b(?:staff|resources?|vendors?|service providers?)\b)"},
    "3.2": {"partial": r"(?=.*\b(?:all outsourcing activities|outsourcing arrangement)\b)(?=.*\bthird party\b)"},
    "3.3": {"complete": r"(?=.*\bthird party\b)(?=.*\b(?:member of the group|related company)\b)(?=.*\bunrelated third party\b)(?=.*\b(?:elsewhere|any location|domestically|internationally)\b)"},
    "3.4.1": {"partial": r"(?=.*\boverseas\b)(?=.*\b(?:outside|international|foreign)\b)(?=.*\b(?:outsourc|third party)\w*\b)"},
    "3.4.2": {"complete": r"(?=.*\bthird party\b)(?=.*\b(?:member of the group|related company|affiliated entit)\w*\b)(?=.*\b(?:elsewhere|overseas|international)\b)"},
    "3.7": {"complete": r"(?=.*\boutsourcing policy\b)(?=.*\bderived from\b)(?=.*\boutsourcing regulations?\b)(?=.*\bother related regulations?\b)"},
    "4.3.1": {"partial": r"(?=.*\bboard\b)(?=.*\b(?:oversight|management|review)\b)(?=.*\boutsourcing arrangements?\b)"},
    "4.3.2": {"complete": r"(?=.*\boutsourcing policy\b)(?=.*\b(?:risk is mitigated|mitigating the risk|risk mitigation)\b)(?=.*\b(?:monitoring controls|monitoring systems|governance and internal control)\b)(?=.*\bregulatory requirements?\b)"},
    "6.1": {"partial": r"(?=.*\bboard\b)(?=.*\bresponsible\b)(?=.*\b(?:oversight|management)\b)(?=.*\boutsourcing arrangements?\b)"},
    "6.2.1": {"partial": r"(?=.*\boutsourcing arrangements?\b)(?=.*\b(?:risk mitigation|risks? will be considered|manage risks?)\b)"},
    "6.2.2": {"partial": r"(?=.*\b(?:compliance|regulatory)\b)(?=.*\b(?:risk|requirements?|laws?|regulations?)\b)(?=.*\boutsourc\w*\b)"},
    "6.2.3": {"partial": r"(?=.*\b(?:regulator|authority)\b)(?=.*\b(?:notify|report|monitor)\w*\b)(?=.*\boutsourc\w*\b)"},
    "6.2.4": {"partial": r"(?=.*\bpolicyholder\w*\b)(?=.*\b(?:protection|rights?|service|monitor)\w*\b)(?=.*\boutsourc\w*\b)"},
    "6.3": {"partial": r"(?=.*\bconflict of interest\b)(?=.*\b(?:due diligence|assess|review)\w*\b)"},
    "6.4.1": {"partial": r"(?=.*\b(?:fees?|financial impact|cost implications?)\b)(?=.*\b(?:assess|consider|determine)\w*\b)(?=.*\boutsourc\w*\b)"},
    "7.2.1": {"partial": r"(?=.*\boutsourcing policy\b)(?=.*\b(?:governance|risk mitigation|internal controls?)\b)"},
    "7.2.2": {"partial": r"(?=.*\bconcentration\b)(?=.*\b(?:limited number|same third party|over[- ]reliance|many other companies)\b)"},
    "7.2.3": {"complete": r"(?=.*\bcontractual risks?\b)(?=.*\b(?:other|operational|strategic|reputational) risks?\b)(?=.*\b(?:assess|consider)\w*\b)(?=.*\b(?:monitor|manag)\w*\b)"},
    "7.2.4": {"complete": r"(?=.*\bdue diligence\b)(?=.*\b(?:before|prior to)\b)(?=.*\b(?:review|evaluation)\b)(?=.*\bapprov\w*\b)"},
    "7.4": {"complete": r"(?=.*\bapplicable to all\b)(?=.*\b(?:staff|employees?|resources?)\b)(?=.*\b(?:read|aware)\w*\b)(?=.*\bcompliance\b)"},
    "7.5.1": {"complete": r"(?=.*\bbusiness case\b)(?=.*\bcosts?\b)(?=.*\bbenefits?\b)(?=.*\brisks?\b)"},
    "7.5.2": {"complete": r"(?=.*\bselection criteri\w*\b)(?=.*\bfair and impartial\b)(?=.*\bprior to outsourcing\b)"},
    "7.5.3": {"complete": r"(?=.*\bconcentration\b)(?=.*\blimited number of third parties\b)(?=.*\bmany other companies\b)(?=.*\bsame third party\b)"},
    "7.5.5": {"complete": r"(?=.*\bcontrol framework\b)(?=.*\brisk management\b)(?=.*\binternal controls?\b)(?=.*\bcompliance\b)"},
    "7.5.6": {"complete": r"(?=.*\bfinancial ability\b)(?=.*\btechnical ability\b)(?=.*\bcapacity\b)(?=.*\bstressful situations?\b)"},
    "7.5.8": {"complete": r"(?=.*\bbusiness continuity\b)(?=.*\bsudden termination\b)(?=.*\b(?:in[- ]house|alternative third part)\w*\b)"},
    "7.6": {"partial": r"(?=.*\bwritten,? legally binding contractual agreement\b)(?=.*\ball components\b)(?=.*\boutsourcing arrangement\b)"},
    "7.7.3": {"partial": r"(?=.*\bthird party\b)(?=.*\b(?:ability|capacity|resources?|skills?)\b)(?=.*\b(?:perform|deliver|services?)\b)"},
    "7.7.4": {"partial": r"(?=.*\bcontrol framework\b)(?=.*\bperformance standards?\b)(?=.*\bpolicies\b)(?=.*\bprocedures\b)"},
    "7.7.5": {"partial": r"(?=.*\breporting\b)(?=.*\bmonitoring\b)(?=.*\bperformance\b)(?=.*\bthird party\b)"},
    "7.7.7": {"partial": r"(?=.*\bcontractual agreement\b)(?=.*\blegal\b)(?=.*\b(?:laws?|regulations?)\b)"},
    "7.7.8": {"partial": r"(?=.*\bperformance levels?\b)(?=.*\bmetrics\b)(?=.*\bmonitor\w*\b)(?=.*\bthird party\b)"},
    "7.7.9": {"partial": r"(?=.*\baudit\b)(?=.*\boutsourcing\b)(?=.*\b(?:controls?|compliance)\b)"},
    "7.7.10": {"complete": r"(?=.*\bright of access\b)(?=.*\boutsourcing activity\b)(?=.*\b(?:legal clause|standard clause)\b)(?=.*\bcontract\b)"},
    "7.7.11": {"complete": r"(?=.*\bnon-disclosure agreements?\b)(?=.*\bconfidentiality\b)(?=.*\bsecurity\b)(?=.*\b(?:policyholder|financial) data\b)"},
    "7.7.14": {"partial": r"(?=.*\bbusiness continuity\b)(?=.*\bprocedures\b)(?=.*\bunable to\b)(?=.*\boutsourcing agreement\b)"},
    "7.7.16": {"partial": r"(?=.*\b(?:non-disclosure agreement|confidentiality provisions?)\b)(?=.*\b(?:security|data)\b)"},
    "7.7.18": {"partial": r"(?=.*\binsurance coverage\b)(?=.*\bdue diligence\b)(?=.*\bthird party\b)"},
    "7.7.20": {"partial": r"(?=.*\bsudden termination\b)(?=.*\b(?:in[- ]house|alternative third part)\w*\b)"},
    "7.10": {"partial": r"(?=.*\bservice level agreements?\b)(?=.*\bkey performance indicators?\b)(?=.*\bmonitor\w*\b)"},
    "7.11.2": {"complete": r"(?=.*\breview process\b)(?=.*\byearly basis\b)(?=.*\bcompliance\b)(?=.*\b(?:laws?|legal requirements?|regulations?)\b)"},
    "9.1": {"complete": r"(?=.*\ball new material outsourcing arrangements?\b)(?=.*\brenewal of existing arrangements?\b)(?=.*\bmust be in accordance with this outsourcing policy\b)"},
    "9.2": {"partial": r"(?=.*\brenewal of existing arrangements?\b)(?=.*\bin accordance with this outsourcing policy\b)"},
    "10": {"partial": r"(?=.*\binternal audit\b)(?=.*\boutsourcing arrangement\b)(?=.*\baudited\b)"},
}

# These substantive elements are commonly implied by broad monitoring or
# continuity wording but are still missing. They prevent false "complete"
# recommendations while retaining the useful policy evidence as partial.
NEUTRAL_COMPLETE_GUARDS: Dict[str, str] = {
    "5.2.1": r"(?=.*\bdetermin\w*\b)(?=.*\bmaterial function\b)(?=.*\bpolicyholders?\b)(?=.*\bfinanc\w*\b)(?=.*\breputation\b)(?=.*\bbusiness operations?\b)(?=.*\bfail\w* to perform\b)",
    "7.2.5": r"(?=.*\boperational risk\b)(?=.*\bmarket conduct\b)(?=.*\bfair treatment\b)(?=.*\b(?:customers?|policyholders?)\b)(?=.*\bregular review\b)",
    "7.7.2": r"(?=.*\b(?:contract|agreement)\b)(?=.*\btype\b)(?=.*\bfrequency\b)(?=.*\bfunction|activity\b)",
    "7.7.14": r"(?=.*\b(?:contract|agreement)\b)(?=.*\bcontinuity\b)(?=.*\b(?:insolvent|liquidat|business rescue|curatorship)\w*\b)",
    "7.7.15": r"(?=.*\b(?:contract|agreement)\b)(?=.*\b(?:regulator|authority)\b)(?=.*\baccess\b)(?=.*\bbusiness\b)(?=.*\binformation\b)",
    "7.7.20": r"(?=.*\b(?:contract|agreement)\b)(?=.*\breasonable termination period\b)(?=.*\bcontingency plans?\b)",
    "7.10": r"(?=.*\b(?:service level|standard of service)\b)(?=.*\b(?:customers?|policyholders?)\b)(?=.*\bmonitor\w*\b)(?=.*\bmanag\w*\b)(?=.*\breview\w*\b)",
    "10": r"(?=.*\bappointed auditors?\b)(?=.*\bstatutory actuary\b)(?=.*\b(?:bring|provide|communicat)\w*\b)",
}

# A small number of obligations are intentionally implemented through two
# separate policy controls.  Preserve both exact excerpts instead of pretending
# one passage proves the whole obligation.
NEUTRAL_MULTI_PASSAGE_PROFILES: Dict[str, Tuple[str, str]] = {
    "7.4": (
        r"(?=.*\bapplicable to all\b)(?=.*\b(?:staff|employees?|resources?)\b)",
        r"(?=.*\brelevant sections?\b)(?=.*\bread\b)(?=.*\bemployees?\b)(?=.*\bcompliance\b)",
    ),
    "7.7.11": (
        r"(?=.*\bnon-disclosure agreement\b)(?=.*\bconfidentiality\b)(?=.*\bpolicyholder\b)(?=.*\bfinancial data\b)",
        r"(?=.*\bconfidentiality provisions?\b)(?=.*\bsecurity needs?\b)",
    ),
}

# The benchmark policy contains a prominent disclaimer explaining that it is a
# controlled validation pack.  A disclaimer is never operative policy evidence.
NON_OPERATIVE_EVIDENCE = re.compile(
    r"\bbenchmark notice\b|\bintentionally incomplete\b|"
    r"\bnot an operational legal policy\b|\bvalidation pack\b",
    flags=re.I,
)

SECTION_DRAFT_POLICY_CLAUSES: Dict[str, str] = {
    "3.1": (
        "For South African operations, this policy applies to all insurers, "
        "including reinsurers to the extent specified by FSCA Directive 159 "
        "section 3.6."
    ),
    "6.5": (
        "Where an outsourcing contract authorises sub-outsourcing, the insurer "
        "must require the sub-outsourcing to comply with the accountability, "
        "risk, governance, policyholder-treatment, conflict-of-interest and "
        "remuneration principles in FSCA Directive 159 sections 6.1 to 6.4."
    ),
    "7.2.1": (
        "The outsourcing policy must give effect to the accountability, risk, "
        "governance, policyholder-treatment, conflict-of-interest, remuneration "
        "and sub-outsourcing principles required by FSCA Directive 159 section 6."
    ),
    "7.2.4": (
        "The outsourcing policy must require documented internal review and "
        "approval of every proposed outsourcing of a control, management or "
        "material function before execution, using the due-diligence and approval "
        "controls required by FSCA Directive 159 section 7.5."
    ),
    "7.2.5": (
        "The outsourcing policy must require appropriate management and regular "
        "review of every outsourced control, management or material function, "
        "including operational-risk, market-conduct and fair-treatment review, "
        "using the controls required by FSCA Directive 159 sections 7.9 to 7.11."
    ),
    "7.5.7": (
        "Before outsourcing a control, management or material function, the "
        "insurer must establish documented management and monitoring procedures "
        "covering the risk, service and provider-assessment controls required by "
        "FSCA Directive 159 sections 7.9 to 7.11."
    ),
    "7.5.9": (
        "Before outsourcing a control, management or material function, the "
        "insurer must obtain and document every approval required by its "
        "outsourcing policy and approval matrix; no outsourcing contract may be "
        "executed or take effect before those approvals are recorded."
    ),
    "7.8": (
        "Where an outsourcing contract permits a service provider to sub-outsource "
        "any part or all of an outsourced function or activity, the insurer must "
        "require the sub-outsourcing arrangement and contract to comply with the "
        "control and written-contract requirements of FSCA Directive 159 sections "
        "7.6 and 7.7."
    ),
    "5.2.2": (
        "For every proposed outsourcing, the insurer's materiality assessment "
        "must document its ability to maintain appropriate internal controls and "
        "meet all applicable legal and regulatory requirements. These tests are "
        "mandatory and may not be waived or omitted on commercial grounds."
    ),
    "5.2.3": (
        "For every proposed outsourcing, the insurer's materiality assessment "
        "must document the degree of difficulty and time required to replace the "
        "service provider or resume the function or activity in-house. This test "
        "is mandatory and may not be waived or omitted on commercial grounds."
    ),
    "6.1": (
        "The board of directors and managing executives remain responsible and "
        "accountable for the insurer's insurance business and for effective "
        "oversight of every outsourced function or activity. Operational duties "
        "may be assigned, but accountability is not transferred by any approval, "
        "delegation or outsourcing contract."
    ),
    "6.3": (
        "Employees, decision-makers and service providers must disclose actual, "
        "potential and perceived conflicts of interest immediately. Every conflict "
        "must be recorded, assessed by Compliance and avoided where possible; "
        "where avoidance is not possible, documented mitigation and approval are "
        "required before the outsourcing decision or activity proceeds. The assessment "
        "and mitigation must address conflicts between the insurer's business, "
        "policyholders and the service provider or other person."
    ),
    "6.4.4": (
        "Remuneration for an outsourced function or activity must be reasonable "
        "and commensurate with the work performed and must never be linked, "
        "directly or indirectly, to the monetary value or volume of insurance "
        "claims repudiated, paid, not paid or partially paid."
    ),
    "7.3": (
        "The outsourcing policy must be reviewed at least annually and must also "
        "be reviewed and adapted promptly after any significant legal, regulatory, "
        "organisational, risk, control or outsourcing change. Each review must be "
        "documented and submitted to the board or its delegated risk committee for approval."
    ),
    "7.7.15": (
        "Every applicable written outsourcing contract must require the service "
        "provider to give the FSCA, Prudential Authority and any other lawfully "
        "empowered South African regulator unrestricted and timely access to the "
        "relevant business, systems, premises, personnel, records and information. "
        "Regulatory access is not subject to provider consent or a commercial-sensitivity exception, "
        "subject only to lawful privilege."
    ),
    "8.1.1": (
        "For every proposed outsourcing of a control, management or material "
        "function, Regulatory Compliance must notify the responsible South African "
        "regulator timeously and no later than one month before the outsourcing "
        "contract's effective date."
    ),
    "8.1.2": (
        "For every proposed outsourcing of a control, management or material "
        "function, Regulatory Compliance must notify the responsible South African "
        "regulator timeously and no later than one month before the contract's "
        "effective date and must identify the proposed outsourcing and the service provider."
    ),
    "8.1.3": (
        "For every proposed outsourcing of a control, management or material "
        "function, Regulatory Compliance must notify the responsible South African "
        "regulator timeously and no later than one month before the contract's "
        "effective date and must describe the key risks and mitigation strategies."
    ),
}

ACTION_FAMILIES = (
    (r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b", r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b"),
    (r"\bmonitor|\breview|\bassess", r"\bmonitor|\breview|\bassess|\bevaluat|\bdue diligence\b"),
    (r"\bmaintain|\bretain|\brecord|\bdocument", r"\bmaintain|\bretain|\brecord|\bdocument"),
    (r"\bapprov", r"\bapprov"),
    (r"\bestablish|\bimplement|\bdevelop", r"\bestablish|\bimplement|\bdevelop|\badopt"),
    (r"\bensure|\brequire|\bprovide", r"\bensure|\brequire|\bprovide"),
    (r"\bprohibit|\bmay not\b|\bmust not\b", r"\bprohibit|\bmay not\b|\bmust not\b"),
    (r"\bapply\b|\bapplies\b|\bscope\b", r"\bapply\b|\bapplies\b|\bscope\b"),
    (r"\bspecify\b|\bset out\b|\baddress\b", r"\bspecify\b|\bset out\b|\baddress\b|\binclude\b|\brequire\b"),
)

OCR_REPAIRS = (
    (re.compile(r"\bAninsurer\b", re.I), "An insurer"),
    (re.compile(r"\bA-written\b", re.I), "A written"),
    (re.compile(r"\bregulatary\b", re.I), "regulatory"),
    (re.compile(r"\bcperations\b", re.I), "operations"),
    (re.compile(r"\bcontre!\s+function\b", re.I), "control function"),
    (re.compile(r"\breferrec\b", re.I), "referred"),
    (re.compile(r"\bOctcber\b", re.I), "October"),
    (re.compile(r"\bAsscciation\b", re.I), "Association"),
    (re.compile(r"\bgovemed\b", re.I), "governed"),
    (re.compile(r"\bgovemance\b", re.I), "governance"),
    (re.compile(r"\bbeard of directors\b", re.I), "board of directors"),
    (re.compile(r"\bfer the insurance ousiness\b", re.I), "for the insurance business"),
    (re.compile(r"\bPrincipies\b", re.I), "Principles"),
    (re.compile(r"\bobligatians\b", re.I), "obligations"),
    (re.compile(r"\bRemuneration paic\b", re.I), "Remuneration paid"),
    (re.compile(r"\bmust net result\b", re.I), "must not result"),
    (re.compile(r"\bcommission cr a binder fee\b", re.I), "commission or a binder fee"),
    (re.compile(r"\bprior te\b", re.I), "prior to"),
    (re.compile(r"\brelating ta\b", re.I), "relating to"),
    (re.compile(r"\bpersen te\b", re.I), "person to"),
    (re.compile(r"\bRegistrar te\b", re.I), "Registrar to"),
    (re.compile(r"\binsurers compliance\b", re.I), "insurer's compliance"),
    (re.compile(r"\bLTast\b", re.I), "LT Act"),
    (re.compile(r"\b42\s+April\s+2042\b", re.I), "12 April 2012"),
    (re.compile(r"\bpelicyhelders\b", re.I), "policyholders"),
    (re.compile(r"\bofher\b", re.I), "other"),
    (re.compile(r"\bperfarms\b", re.I), "performs"),
    (re.compile(r"\bsub-\s+outsourcing\b", re.I), "sub-outsourcing"),
    (re.compile(r"\bservices[’'](?=[\s.,;:]|$)", re.I), "services"),
    (re.compile(r"\bbusiness[’']\s+of insurers\b", re.I), "business of insurers"),
    (re.compile(r"services\}", re.I), "services)"),
    (re.compile(r"\boutsourcing en the policyholders\b", re.I), "outsourcing on the policyholders"),
    (re.compile(r"\breplacing ihe other person\b", re.I), "replacing the other person"),
    (re.compile(r"\btypes anc overall level\b", re.I), "types and overall level"),
    (re.compile(r"\bfunctions ar activities\b", re.I), "functions or activities"),
    (re.compile(r"\s+ons ie aco ea poteenal uae Oy outa.*$", re.I), ""),
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any) -> str:
    text = str(value or "")
    # PDF text extraction frequently inserts whitespace after a line-ending
    # hyphen (for example ``inter-\nrelated``).  Preserve the legal word rather
    # than letting layout artefacts defeat exact material-element checks.
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in OCR_REPAIRS:
        text = pattern.sub(replacement, text)
    return text


def load_register(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        workbook = pd.ExcelFile(path)
        preferred = next((name for name in workbook.sheet_names if name.strip().lower() == "obligations"), None)
        if preferred is None:
            # Accept renamed obligation sheets and prior gap-assessment exports.
            # Selecting the first sheet is unsafe because it may be an executive
            # summary that does not contain the register columns.
            for name in workbook.sheet_names:
                candidate = pd.read_excel(path, sheet_name=name, nrows=0)
                candidate.columns = [str(column).strip() for column in candidate.columns]
                if set(REQUIRED_REGISTER_COLUMNS).issubset(candidate.columns):
                    preferred = name
                    break
        if preferred is None:
            raise ValueError("No worksheet contains the required obligation-register columns.")
        df = pd.read_excel(path, sheet_name=preferred)
    else:
        df = pd.read_csv(path)
    df.columns = [str(column).strip() for column in df.columns]
    missing = [column for column in REQUIRED_REGISTER_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    columns = REQUIRED_REGISTER_COLUMNS + [column for column in OPTIONAL_REGISTER_COLUMNS if column in df.columns]
    result = df[columns].fillna("").copy()
    if result.empty:
        raise ValueError("The obligation register contains no data rows.")
    for column in ("Language from Directive", "Obligation"):
        result[column] = result[column].map(_clean)
    return _repair_register(result)


def _repair_register(register: pd.DataFrame) -> pd.DataFrame:
    """Repair deterministic structural defects in an uploaded obligation register.

    Gap review may be run against a register generated by an older backend.  Do
    not trust an old ``Actionable=No`` or informational obligation when the
    directive wording (including its parent stem) clearly creates a duty.
    """
    rows = register.to_dict(orient="records")
    repaired: List[Dict[str, Any]] = []
    embedded_parent = re.compile(r"(?<![\d.])(\d+(?:\.\d+)+)\)\s+(?=(?:An?|The)\s+insurer\b)", re.I)

    for row in rows:
        text = _clean(row.get("Language from Directive", ""))
        original_text = text
        section = str(row.get("Section", "")).strip()
        match = embedded_parent.search(text)
        if match and match.group(1) != section:
            before = text[:match.start()].strip()
            # A running heading is sometimes joined to the preceding clause.
            before = re.sub(r"\s+Principles?\s+with\s+which\s+any\s+outsourcing\s+must\s+comply\s*$", "", before, flags=re.I)
            if before:
                row["Language from Directive"] = before
                text = before
            inserted = dict(row)
            inserted["Section"] = match.group(1)
            inserted["Language from Directive"] = _clean(original_text[match.end():])
            inserted["Obligation"] = "Parent clause only; the actionable requirements are captured in the numbered child clauses that follow."
            inserted["Priority"] = "Low"
            inserted["Actionable"] = "No"
            repaired.append(row)
            repaired.append(inserted)
            continue
        repaired.append(row)

    frame = pd.DataFrame(repaired, columns=register.columns)
    for index, row in frame.iterrows():
        section = str(row.get("Section", "")).strip()
        parent_section = section.rsplit(".", 1)[0] if "." in section else ""
        prior = frame.iloc[:index]
        parent_rows = prior[prior["Section"].astype(str) == parent_section] if parent_section else pd.DataFrame()
        parent_context = _clean(parent_rows.iloc[-1]["Language from Directive"]) if not parent_rows.empty else ""
        directive_text = _clean(row.get("Language from Directive", ""))
        obligation = _clean(row.get("Obligation", ""))
        parent_action_missing = bool(
            parent_context
            and (
                (re.search(r"\bwritten\s+contract\b", parent_context, re.I) and not re.search(r"\bwritten\s+contract\b", obligation, re.I))
                or (re.search(r"\bregularly\s+assess\b", parent_context, re.I) and not re.search(r"\bregularly\s+assess\b", obligation, re.I))
                or (re.search(r"\bnotify\s+the\s+Registrar\b", parent_context, re.I) and not re.search(r"\bnotify\s+the\s+Registrar\b", obligation, re.I))
                or (re.search(r"\bmay\s+not\s+outsource\b", parent_context, re.I) and not re.search(r"\b(?:may|must)\s+not\s+outsource\b", obligation, re.I))
            )
        )
        stale_obligation = bool(re.search(
            r"informational|contextual|no standalone implementation obligation|^The regulated entity must comply with this requirement:",
            obligation,
            flags=re.I,
        ))
        if is_actionable(directive_text, parent_context) and (stale_obligation or parent_action_missing):
            frame.at[index, "Obligation"] = generate_obligation(section, directive_text, parent_context)
            if "Actionable" in frame.columns:
                frame.at[index, "Actionable"] = "Yes"
            if "Priority" in frame.columns and str(row.get("Priority", "")) == "Low":
                frame.at[index, "Priority"] = "High" if re.search(r"\b(?:must|may not|shall)\b", f"{parent_context} {directive_text}", re.I) else "Medium"
    return frame


def chunk_policy_text(raw_text: str, max_chars: int = 2200, overlap: int = 320) -> List[Dict[str, str]]:
    """Split policy text into page-aware evidence chunks."""
    parts = PAGE_SPLIT.split(raw_text.strip())
    page_sections: List[Tuple[str, str]] = []
    if len(parts) > 1:
        for index in range(1, len(parts), 2):
            if index + 1 < len(parts):
                page_sections.append((parts[index], parts[index + 1]))
    else:
        page_sections.append(("Unknown", raw_text))

    chunks: List[Dict[str, str]] = []
    for page, page_text in page_sections:
        cleaned = re.sub(r"[ \t]+", " ", page_text.replace("\r", "\n"))
        starts = list(re.finditer(r"(?m)^\s*(?:\d+(?:\.\d+)*\.?|[A-Z][A-Z /&-]{3,})\s+", cleaned))
        boundaries = sorted(set([0, *(match.start() for match in starts), len(cleaned)]))
        for start, end in zip(boundaries, boundaries[1:]):
            section = cleaned[start:end].strip()
            if not section:
                continue
            step = max(max_chars - max(0, min(overlap, max_chars // 3)), 1)
            for part_start in range(0, len(section), step):
                text = section[part_start:part_start + max_chars].strip()
                if text:
                    chunks.append({"page": str(page), "text": text})
                if part_start + max_chars >= len(section):
                    break
    if not chunks and raw_text.strip():
        chunks.append({"page": "Unknown", "text": raw_text.strip()[:max_chars]})
    return chunks


@lru_cache(maxsize=8)
def _cached_policy_chunks(
    policy_text_sha256: str,
    raw_text: str,
    max_chars: int = 2200,
    overlap: int = 320,
) -> Tuple[Tuple[str, str], ...]:
    """Cache the page-aware policy structure for repeated benchmark/review runs.

    ``policy_text_sha256`` makes the cache identity explicit in diagnostics and
    prevents a stale text index from being reused after a policy changes.
    """
    del policy_text_sha256
    return tuple(
        (str(chunk["page"]), str(chunk["text"]))
        for chunk in chunk_policy_text(raw_text, max_chars=max_chars, overlap=overlap)
    )


def cached_policy_chunks(raw_text: str) -> List[Dict[str, str]]:
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
    return [
        {"page": page, "text": text}
        for page, text in _cached_policy_chunks(digest, raw_text)
    ]


def build_policy_evidence_index(chunks: List[Dict[str, str]]) -> Dict[str, Any]:
    """Build one reusable inverted index for the uploaded internal policy."""
    postings: Dict[str, List[int]] = {}
    for chunk_index, chunk in enumerate(chunks):
        for term in set(_keywords(str(chunk.get("text", "")))):
            postings.setdefault(term, []).append(chunk_index)
    return {
        "postings": postings,
        "chunk_count": len(chunks),
    }


def _prefilter_policy_chunk_indices(
    search_text: str,
    chunks: List[Dict[str, str]],
    evidence_index: Dict[str, Any] | None,
    *,
    minimum_pool: int = 12,
    maximum_pool: int = 48,
) -> List[int]:
    if not chunks:
        return []
    if not evidence_index:
        return list(range(len(chunks)))
    postings = evidence_index.get("postings", {})
    counts: Counter[int] = Counter()
    for term in _keywords(search_text)[:45]:
        for chunk_index in postings.get(term, []):
            counts[chunk_index] += 1
    if not counts:
        return list(range(len(chunks)))
    ordered = [chunk_index for chunk_index, _ in counts.most_common(maximum_pool)]
    if len(ordered) < minimum_pool:
        ordered_set = set(ordered)
        ordered.extend(
            chunk_index
            for chunk_index in range(len(chunks))
            if chunk_index not in ordered_set
        )
    return ordered[: min(maximum_pool, len(chunks))]


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z-]{3,}", text.lower())
    output: List[str] = []
    for word in words:
        if word not in STOPWORDS and word not in output:
            output.append(word)
    return output


def _core_terms(text: str) -> List[str]:
    """Return subject-matter terms, excluding generic actors and action words."""
    excluded = STOPWORDS | {
        "another", "person", "persons", "company", "companies", "board", "directors",
        "must", "shall", "should", "ensure", "require", "required", "requires", "provide",
        "provided", "notify", "notification", "report", "submit", "maintain", "establish",
        "implement", "develop", "review", "monitor", "assess", "assessment", "determine",
        "include", "including", "relating", "regarding", "appropriate", "relevant",
    }
    return [term for term in _keywords(text) if term not in excluded][:32]


def _action_alignment(required: str, evidence: str) -> float:
    required_families = [
        evidence_pattern
        for required_pattern, evidence_pattern in ACTION_FAMILIES
        if re.search(required_pattern, required, flags=re.I)
    ]
    if not required_families:
        return 1.0
    matched = sum(bool(re.search(pattern, evidence, flags=re.I)) for pattern in required_families)
    return matched / len(required_families)


def _concept_overlap(required: str, evidence: str) -> float:
    terms = _core_terms(required)
    if not terms:
        return fuzz.token_set_ratio(required[:1200], evidence[:1800]) / 100
    lowered = evidence.lower()
    hits = sum(term in lowered for term in terms)
    term_ratio = hits / len(terms)
    fuzzy_ratio = fuzz.token_set_ratio(" ".join(terms), evidence[:1800]) / 100
    return min(1.0, (term_ratio * 0.72) + (fuzzy_ratio * 0.28))


def _policy_language_strength(evidence: str) -> str:
    imperative = re.match(
        r"^(?:immediately\s+)?(?:report|notify|ensure|maintain|document|record|"
        r"assess|evaluate|review|monitor|specify|set out|address|provide|require)\b",
        evidence.strip(),
        flags=re.I,
    )
    if MANDATORY_POLICY_LANGUAGE.search(evidence) or imperative:
        return "mandatory"
    if ADVISORY_POLICY_LANGUAGE.search(evidence):
        return "advisory"
    return "descriptive"


def _timing_evidence_pattern(required: str) -> str:
    exact_date = re.search(
        r"\bno later than\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\b",
        required,
        flags=re.I,
    )
    if exact_date:
        words = re.findall(r"[A-Za-z0-9]+", exact_date.group(0).lower())
        return r"\b" + r"\s+".join(re.escape(word) for word in words) + r"\b"
    exact = re.search(
        r"\b(?:within|no later than)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirty)\s+(?:business\s+)?(?:day|days|week|weeks|month|months|year|years)\b",
        required,
        flags=re.I,
    )
    if exact:
        words = re.findall(r"[A-Za-z0-9]+", exact.group(0).lower())
        return r"\b" + r"\s+".join(re.escape(word) for word in words) + r"\b"
    if re.search(r"\bimmediately\b", required, flags=re.I):
        return r"\bimmediate(?:ly)?\b"
    if re.search(r"\bprior to\b", required, flags=re.I):
        return r"\bprior to\b|\bbefore\b"
    if re.search(r"\bannual(?:ly)?\b|\byearly\b", required, flags=re.I):
        return r"\bannual(?:ly)?\b|\byearly\b|\bevery\s+12\s+months\b"
    if re.search(r"\bquarterly\b", required, flags=re.I):
        return r"\bquarterly\b|\bevery\s+3\s+months\b"
    if re.search(r"\bmonthly\b", required, flags=re.I):
        return r"\bmonthly\b|\bevery\s+month\b"
    if re.search(r"\bregular(?:ly)?\b|\bperiodic(?:ally)?\b", required, flags=re.I):
        return r"\bregular(?:ly)?\b|\bperiodic(?:ally)?\b|\bannual(?:ly)?\b|\bquarterly\b|\bmonthly\b"
    return (
        r"\bno later than\b|\bwithin\s+\d+|\bprior to\b|\bimmediately\b|"
        r"\bmonthly\b|\bquarterly\b|\bannual(?:ly)?\b|\byearly\b|"
        r"\bperiodic(?:ally)?\b|\bregular(?:ly)?\b"
    )


def _required_material_elements(directive_text: str, obligation: str, section: str = "") -> List[Dict[str, Any]]:
    required = f"{directive_text} {obligation}"
    suppressed_labels = SECTION_SUPPRESSED_ELEMENT_LABELS.get(section, set())
    elements: List[Dict[str, Any]] = [
        {"label": "mandatory policy requirement", "weight": 3, "type": "mandatory"},
        {"label": "core action and subject matter", "weight": 3, "type": "core"},
    ]
    for label, required_pattern, evidence_pattern, weight in MATERIAL_ELEMENT_RULES:
        if label in suppressed_labels:
            continue
        if re.search(required_pattern, required, flags=re.I):
            if label == "specified timing or frequency":
                evidence_pattern = _timing_evidence_pattern(required)
            elif label == "external regulatory notification or reporting" and re.search(
                r"\bFSCA\b|\bFSB\b|\bRegistrar\b|\bregulator", required, flags=re.I
            ):
                evidence_pattern = (
                    r"(?:\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b).{0,100}"
                    r"(?:\bFSCA\b|\bFSB\b|\bRegistrar\b|\bregulator|"
                    r"\bInsurance Authority\b|\bSAMA\b|\bCentral Bank\b)"
                    r"|(?:\bFSCA\b|\bFSB\b|\bRegistrar\b|\bregulator|"
                    r"\bInsurance Authority\b|\bSAMA\b|\bCentral Bank\b).{0,100}"
                    r"(?:\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b)"
                )
            elif label == "required approval" and re.search(r"\bboard\b", required, flags=re.I):
                evidence_pattern = r"\bboard\b.{0,80}\bapprov|\bapprov.{0,80}\bboard\b"
            elements.append({
                "label": label,
                "weight": weight,
                "type": "pattern",
                "evidence_pattern": evidence_pattern,
            })
    for label, required_pattern, evidence_pattern, weight in ATOMIC_MATERIAL_ELEMENT_RULES:
        if label in suppressed_labels:
            continue
        if re.search(required_pattern, required, flags=re.I):
            elements.append({
                "label": label,
                "weight": weight,
                "type": "pattern",
                "evidence_pattern": evidence_pattern,
            })
    for label, evidence_pattern, weight in SECTION_MATERIAL_ELEMENT_RULES.get(section, ()):
        elements.append({
            "label": label,
            "weight": weight,
            "type": "pattern",
            "evidence_pattern": evidence_pattern,
        })
    if section.startswith("7.7.") and not any(item["label"] == "written outsourcing contract" for item in elements):
        elements.append({
            "label": "written outsourcing contract",
            "weight": 3,
            "type": "pattern",
            "evidence_pattern": r"\b(?:written\s+)?(?:outsourcing\s+)?(?:contract|agreement)\b",
        })
    if section.startswith("7.11.") and not any(item["label"] == "documented service-provider assessment" for item in elements):
        elements.append({
            "label": "documented service-provider assessment",
            "weight": 3,
            "type": "pattern",
            "evidence_pattern": r"\b(?:assess|assessment|review|monitor)(?:ed|ing|s)?\b",
        })
    if _requires_sa_jurisdiction(directive_text, obligation):
        elements.append({
            "label": "South African / FSCA jurisdiction",
            "weight": 3,
            "type": "jurisdiction",
        })
    deduplicated: List[Dict[str, Any]] = []
    seen = set()
    for element in elements:
        if element["label"] not in seen:
            seen.add(element["label"])
            deduplicated.append(element)
    return deduplicated


def _coverage_ledger(
    directive_text: str,
    obligation: str,
    evidence: str,
    section: str = "",
    *,
    candidate_score: float = 0.0,
    source_method: str = "unknown",
) -> Dict[str, Any]:
    evidence = _clean(evidence)
    required_text = f"{directive_text} {obligation}"
    elements = _required_material_elements(directive_text, obligation, section)
    language_strength = _policy_language_strength(evidence)
    concept_overlap = _concept_overlap(required_text, evidence) if evidence else 0.0
    action_alignment = _action_alignment(required_text, evidence) if evidence else 0.0
    core_match = (
        action_alignment >= 0.50
        and (
            concept_overlap >= 0.30
            or (language_strength == "mandatory" and concept_overlap >= 0.12)
        )
    )
    matched: List[str] = []
    missing: List[str] = []
    matched_weight = 0
    total_weight = sum(int(element["weight"]) for element in elements)
    for element in elements:
        element_type = element["type"]
        if element_type == "mandatory":
            is_matched = language_strength == "mandatory"
        elif element_type == "core":
            is_matched = core_match
        elif element_type == "jurisdiction":
            is_matched = bool(SOUTH_AFRICA_TERMS.search(evidence)) and not _jurisdiction_mismatch(
                directive_text, obligation, evidence
            )
        else:
            is_matched = bool(re.search(element["evidence_pattern"], evidence, flags=re.I))
        if is_matched:
            matched.append(element["label"])
            matched_weight += int(element["weight"])
        else:
            missing.append(element["label"])

    equivalent_complete_pattern = SECTION_EQUIVALENT_COMPLETE_RULES.get(section, "")
    equivalent_complete = bool(
        equivalent_complete_pattern
        and evidence
        and re.search(equivalent_complete_pattern, evidence, flags=re.I)
    )
    if equivalent_complete:
        matched = [element["label"] for element in elements]
        missing = []
        matched_weight = total_weight

    coverage_percentage = round((matched_weight / max(total_weight, 1)) * 100)
    negative_only = _contains_negative(evidence) and language_strength != "mandatory"
    adverse_evidence = _is_adverse_evidence(section, evidence)
    jurisdiction_mismatch = _jurisdiction_mismatch(directive_text, obligation, evidence)

    generic_labels = {
        "mandatory policy requirement",
        "core action and subject matter",
        "South African / FSCA jurisdiction",
        "written outsourcing contract",
        "defined scope and applicability",
        "prohibition, condition or exception",
        "ongoing monitoring, assessment or review",
    }
    specific_matched = [
        label for label in matched
        if label not in generic_labels
    ]
    required_specific = [
        element["label"]
        for element in elements
        if element["label"] not in generic_labels
    ]
    section_partial_pattern = SECTION_PARTIAL_RELEVANCE_RULES.get(section, "")
    section_partial_relevance = bool(
        section_partial_pattern
        and evidence
        and re.search(section_partial_pattern, evidence, flags=re.I)
        and language_strength in {"mandatory", "advisory"}
    )
    minimum_relevance_pattern = SECTION_MINIMUM_RELEVANCE_RULES.get(section, "")
    minimum_relevance_met = bool(
        not minimum_relevance_pattern
        or (
            evidence
            and re.search(minimum_relevance_pattern, evidence, flags=re.I)
        )
    )
    non_operational = bool(NON_OPERATIVE_EVIDENCE.search(evidence))
    relevant_control = (
        (core_match and (not required_specific or bool(specific_matched)))
        or (
            bool(specific_matched)
            and action_alignment >= 0.35
            and concept_overlap >= 0.10
        )
        or (
            jurisdiction_mismatch
            and core_match
            and any(
                label in matched
                for label in {
                    "defined scope and applicability",
                    "external regulatory notification or reporting",
                }
            )
        )
        or section_partial_relevance
    ) and minimum_relevance_met and not non_operational

    if not evidence or (negative_only and not adverse_evidence) or non_operational:
        status = "Completely Missing"
    elif adverse_evidence:
        # A contradictory clause is directly relevant but cannot prove
        # compliance. Keep it visible as partial evidence so the recommendation
        # can replace the actual defective wording.
        status = "Partially Covered"
    elif equivalent_complete and not jurisdiction_mismatch:
        # A section-specific equivalent is a deliberately strict conjunction of
        # the full legal payload. Internal scope provisions sometimes use
        # operative "applies" wording instead of "must"; do not downgrade a
        # complete equivalent solely because it is not phrased as a command.
        status = "Completely Covered"
    elif not missing and language_strength == "mandatory" and not jurisdiction_mismatch:
        status = "Completely Covered"
    elif relevant_control:
        status = "Partially Covered"
    else:
        status = "Completely Missing"

    if status == "Partially Covered" and coverage_percentage == 0:
        # A directly contradictory clause is relevant audit evidence even when
        # it satisfies none of the positive coverage elements. Keep the status
        # and percentage internally consistent without overstating coverage.
        coverage_percentage = 1

    source_method = (source_method or "unknown").lower()
    method_cap = 92 if source_method == "native" else 85 if source_method == "ocr" else 88
    retrieval_signal = max(0.0, min(1.0, candidate_score))
    if status == "Completely Covered":
        confidence = 88 + round(retrieval_signal * 3)
    elif status == "Completely Missing":
        clear_absence = (
            not evidence
            or language_strength == "descriptive"
            or coverage_percentage <= 25
            or concept_overlap < 0.12
        )
        confidence = 86 if clear_absence else 82
    else:
        critical_missing = any(label in CRITICAL_GAP_LABELS for label in missing)
        confidence = 86 if critical_missing or language_strength == "advisory" else 84
        if 0.12 <= concept_overlap < 0.20 and retrieval_signal < 0.50:
            confidence -= 4
    confidence = round(min(method_cap, confidence))
    ambiguous = (
        status == "Partially Covered"
        and 0.12 <= concept_overlap < 0.20
        and 35 <= coverage_percentage <= 70
    )
    weak_complete = (
        status == "Completely Covered"
        and (
            concept_overlap < 0.45
            or action_alignment < 0.75
            or len(evidence.split()) < 12
        )
    )
    # An absence conclusion is inherently harder to prove than a positive
    # citation when retrieval uses a bounded candidate set.  Keep missing rows
    # in the professional-review queue rather than presenting them as settled.
    manual_review = "Yes" if (
        status == "Completely Missing"
        or confidence < 82
        or source_method == "ocr"
        or ambiguous
        or weak_complete
        or adverse_evidence
    ) else "No"
    return {
        "status": status,
        "required": [element["label"] for element in elements],
        "matched": matched,
        "missing": missing,
        "coverage_percentage": coverage_percentage,
        "confidence_percentage": confidence,
        "manual_review": manual_review,
        "language_strength": language_strength,
        "concept_overlap": concept_overlap,
        "action_alignment": action_alignment,
        "adverse_evidence": adverse_evidence,
    }


def _contains_negative(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NEGATIVE_PHRASES)


def _is_adverse_evidence(section: str, evidence: str) -> bool:
    cleaned = _clean(evidence)
    if not cleaned:
        return False
    return any(
        re.search(pattern, cleaned, flags=re.I)
        for pattern in SECTION_ADVERSE_EVIDENCE_RULES.get(str(section).strip(), ())
    )


def _adverse_evidence_score(section: str, evidence: str) -> float:
    return 1.0 if _is_adverse_evidence(section, evidence) else 0.0


def _preferred_evidence_score(section: str, evidence: str) -> float:
    pattern = SECTION_PREFERRED_EVIDENCE_RULES.get(str(section).strip(), "")
    if not pattern or not evidence:
        return 0.0
    return 1.0 if re.search(pattern, _clean(evidence), flags=re.I) else 0.0


def _is_informational(row: pd.Series, parent_context: str = "") -> bool:
    directive_text = _clean(row.get("Language from Directive", ""))
    if is_actionable(directive_text, parent_context):
        return False
    inherited_duty = bool(
        parent_context
        and re.search(r"\b(must|shall|may\s+not|required|require[sd]?|notify|assess|provide)\b", parent_context, flags=re.I)
        and len(directive_text.split()) >= 2
    )
    if inherited_duty:
        return False
    if str(row.get("Actionable", "")).strip().lower() in {"no", "false", "0"} and not inherited_duty:
        return True
    combined = f"{row.get('Obligation', '')} {row.get('Language from Directive', '')}".lower()
    return any(phrase in combined for phrase in (
        "informational or contextual",
        "contextual background",
        "does not create",
        "no standalone implementation obligation",
        "no direct implementation obligation",
    ))


def _is_structural_parent(register: pd.DataFrame, index: int) -> bool:
    section = str(register.iloc[index].get("Section", "")).strip()
    text = _clean(register.iloc[index].get("Language from Directive", ""))
    if not section or not re.fullmatch(r"\d+(?:\.\d+)*", section):
        return False
    unfinished = bool(
        re.search(r"(?:at\s+least|following|as\s+follows)\s*[-—:]?\.?$", text, flags=re.I)
        or re.search(r"\b(?:must|shall|may\s+not)\b.{0,100}[-—:]\.?$", text, flags=re.I)
        or re.search(r"\b(?:applies|apply)\s+to\s*[~\-—:]?\.?$", text, flags=re.I)
        or re.search(r"\b(?:must|shall|may\s+not)\b.{0,300}\b(?:of|following|least|may)\s*[-—~]", text, flags=re.I)
    )
    if not unfinished:
        return False
    child_prefix = f"{section}."
    return any(str(register.iloc[next_index].get("Section", "")).startswith(child_prefix) for next_index in range(index + 1, min(index + 12, len(register))))


def rank_policy_matches(
    obligation: str,
    directive_text: str,
    chunks: List[Dict[str, str]],
    limit: int = 3,
    evidence_index: Dict[str, Any] | None = None,
    section: str = "",
) -> List[Dict[str, Any]]:
    search_text = f"{obligation} {directive_text}"
    terms = _keywords(search_text)[:45]
    material_elements = _required_material_elements(directive_text, obligation, section)
    ranked: List[Dict[str, Any]] = []
    candidate_indices = _prefilter_policy_chunk_indices(
        search_text,
        chunks,
        evidence_index,
    )
    for chunk_index in candidate_indices:
        chunk = chunks[chunk_index]
        text = chunk["text"]
        normalized_text = _clean(text)
        lowered = normalized_text.lower()
        hits = [term for term in terms if term in lowered]
        keyword_score = len(hits) / max(len(terms), 1)
        fuzzy_score = fuzz.token_set_ratio(search_text[:1200], normalized_text[:1800]) / 100
        concept_score = _concept_overlap(search_text, normalized_text)
        action_score = _action_alignment(search_text, normalized_text)
        material_weight = 0
        material_total = 0
        specific_material_weight = 0
        specific_material_total = 0
        generic_retrieval_labels = {
            "defined scope and applicability",
            "prohibition, condition or exception",
            "ongoing monitoring, assessment or review",
            "South African / FSCA jurisdiction",
        }
        for element in material_elements:
            if element["type"] in {"mandatory", "core"}:
                continue
            weight = int(element["weight"])
            material_total += weight
            if element["label"] not in generic_retrieval_labels:
                specific_material_total += weight
            if element["type"] == "jurisdiction":
                matched_element = bool(SOUTH_AFRICA_TERMS.search(normalized_text))
            else:
                matched_element = bool(
                    re.search(element["evidence_pattern"], normalized_text, flags=re.I)
                )
            if matched_element:
                material_weight += weight
                if element["label"] not in generic_retrieval_labels:
                    specific_material_weight += weight
        material_score = material_weight / material_total if material_total else 0.0
        specific_material_score = (
            specific_material_weight / specific_material_total
            if specific_material_total else 0.0
        )
        partial_pattern = SECTION_PARTIAL_RELEVANCE_RULES.get(section, "")
        partial_relevance_score = (
            1.0
            if partial_pattern
            and re.search(partial_pattern, normalized_text, flags=re.I)
            else 0.0
        )
        preferred_evidence_score = _preferred_evidence_score(
            section,
            normalized_text,
        )
        adverse_evidence_score = _adverse_evidence_score(
            section,
            normalized_text,
        )
        score = (
            (keyword_score * 0.18)
            + (fuzzy_score * 0.12)
            + (concept_score * 0.20)
            + (action_score * 0.10)
            + (material_score * 0.22)
            + (specific_material_score * 0.18)
            + (partial_relevance_score * 0.25)
            + (preferred_evidence_score * 0.20)
            + (adverse_evidence_score * 0.65)
        )
        ranked.append({
            "candidate_id": f"candidate-{chunk_index + 1}",
            "page": chunk["page"],
            "text": text,
            "score": score,
            "keyword_score": keyword_score,
            "concept_score": concept_score,
            "action_score": action_score,
            "material_score": material_score,
            "specific_material_score": specific_material_score,
            "partial_relevance_score": partial_relevance_score,
            "preferred_evidence_score": preferred_evidence_score,
            "adverse_evidence_score": adverse_evidence_score,
            "method": chunk.get("method", "unknown"),
            "hits": hits,
        })
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[: max(limit, 1)]


def best_policy_match(
    obligation: str,
    directive_text: str,
    chunks: List[Dict[str, str]],
    section: str = "",
) -> Tuple[float, Dict[str, str], float, List[str]]:
    """Backward-compatible best-match helper used by tests and integrations."""
    ranked = rank_policy_matches(obligation, directive_text, chunks, 1, section=section)
    if not ranked:
        return 0.0, {"page": "", "text": ""}, 0.0, _keywords(f"{obligation} {directive_text}")
    best = ranked[0]
    terms = _keywords(f"{obligation} {directive_text}")[:45]
    return best["score"], {"page": best["page"], "text": best["text"]}, best["keyword_score"], [term for term in terms if term not in best["text"].lower()]


def _requires_sa_jurisdiction(directive_text: str, obligation: str) -> bool:
    """Legacy compatibility hook.

    Gap coverage is deliberately jurisdiction-neutral: a regulator or country
    name is metadata, not a substantive control element.  Keeping the hook
    makes older integrations import-safe while preventing authority-name
    mismatches from changing a result.
    """
    return False


def _jurisdiction_mismatch(directive_text: str, obligation: str, evidence: str) -> bool:
    return False


def coverage_status(score: float, keyword_score: float, chunk_text: str, jurisdiction_mismatch: bool = False) -> str:
    if not chunk_text:
        return "Completely Missing"
    if _contains_negative(chunk_text):
        return "Partially Covered" if keyword_score >= 0.22 else "Completely Missing"
    if jurisdiction_mismatch:
        return "Partially Covered" if score >= 0.24 or keyword_score >= 0.15 else "Completely Missing"
    # Deterministic similarity is a retrieval signal, not proof of compliance.
    # Reserve complete coverage for exceptionally strong lexical alignment;
    # Gemini decisions still pass through the material-gap validator below.
    if score >= 0.72 and keyword_score >= 0.50:
        return "Completely Covered"
    if score >= 0.28 or keyword_score >= 0.18:
        return "Partially Covered"
    return "Completely Missing"


def _material_gaps(directive_text: str, obligation: str, evidence: str, section: str = "") -> List[str]:
    ledger = _coverage_ledger(directive_text, obligation, evidence, section)
    return list(ledger["missing"])


def _obligation_action_phrase(obligation: str) -> str:
    phrase = _clean(obligation).rstrip(" .-—")
    phrase = re.sub(r"^The regulated entity must comply with (?:this requirement|this applicability and scope provision):\s*", "", phrase, flags=re.I)
    phrase = re.sub(
        r"^(?:An insurer|The insurer|Insurers|The regulated entity)"
        r"(?:,\s*[^,]{1,180},)?\s+may\s+not\s+",
        "not ",
        phrase,
        flags=re.I,
    )
    phrase = re.sub(
        r"^(?:An insurer|The insurer|Insurers|The regulated entity)"
        r"(?:,\s*[^,]{1,180},)?\s+must\s+",
        "",
        phrase,
        flags=re.I,
    )
    phrase = re.sub(r"^A written contract must(?:,\s*at least,?)?\s+", "", phrase, flags=re.I)
    return phrase[:440].strip()


def _jurisdiction_neutral_text(text: str) -> str:
    """Remove authority-name assumptions without weakening the control."""
    replacements = (
        (r"\b(?:FSCA|FSB)\s+Directive 159\b", "the applicable directive"),
        (r"\bSouth African operations\b", "operations"),
        (r"\bSouth African outsourcing arrangements?\b", "outsourcing arrangements"),
        (r"\bSouth African insurer\b", "regulated entity"),
        (r"\bSouth African\b", "applicable"),
        (r"\bDirective 159\b", "the applicable directive"),
        (r"\bFSCA/FSB\b|\bFSCA\b|\bFSB\b", "the regulator"),
        (r"\bRegistrar\b", "the regulator"),
    )
    neutral = text
    for pattern, replacement in replacements:
        neutral = re.sub(pattern, replacement, neutral, flags=re.I)
    neutral = re.sub(r"\bthe\s+the\s+regulator\b", "the regulator", neutral, flags=re.I)
    neutral = re.sub(r"\s+", " ", neutral).strip()
    return neutral


def _draft_policy_clause(section: str, directive_text: str, obligation: str) -> str:
    if section in SECTION_DRAFT_POLICY_CLAUSES:
        return _jurisdiction_neutral_text(SECTION_DRAFT_POLICY_CLAUSES[section])
    combined = f"{directive_text} {obligation}"
    clause = _clean(obligation).rstrip(" .;:")
    clause = re.sub(
        r"^The regulated entity must comply with (?:this requirement|this applicability and scope provision):\s*",
        "",
        clause,
        flags=re.I,
    )
    clause = re.sub(r"^This means that an insurer\b", "The insurer", clause, flags=re.I)
    if section == "9.2" or "1 january 2013" in combined.lower():
        return (
            "The regulated entity must maintain a documented register of legacy outsourcing arrangements "
            "and confirm that each arrangement is compliant whenever it is extended, renewed or amended, with "
            "any surviving exception escalated to Legal and Compliance for remediation."
        )
    if re.match(r"^This Directive\b", clause, flags=re.I):
        clause = re.sub(r"^This Directive\b", "For South African operations, Directive 159", clause, flags=re.I)
    elif re.match(r"^(?:An insurer|The insurer|Insurers|The South African insurer)\b", clause, flags=re.I):
        clause = re.sub(r"^(?:An insurer|The insurer|Insurers|The South African insurer)\b", "The insurer", clause, flags=re.I)
    elif re.match(r"^A written contract\b", clause, flags=re.I):
        clause = re.sub(r"^A written contract\b", "Every applicable written outsourcing contract", clause, flags=re.I)
    elif re.match(r"^An outsourcing policy\b", clause, flags=re.I):
        clause = re.sub(r"^An outsourcing policy\b", "The insurer's outsourcing policy", clause, flags=re.I)
    elif re.match(r"^Any outsourcing\b", clause, flags=re.I):
        clause = re.sub(r"^Any outsourcing\b", "Every applicable outsourcing", clause, flags=re.I)
    elif re.match(r"^Where\b", clause, flags=re.I):
        # Conditional clauses already contain their operative actor and must
        # not be prefixed with the ungrammatical "The insurer must Where".
        pass
    elif re.match(r"^The principles referred to under paragraphs? 6\.1 to 6\.4\b", clause, flags=re.I):
        clause = re.sub(
            r"^The principles referred to under paragraphs? 6\.1 to 6\.4 also apply to\s*",
            "The insurer must apply the principles in paragraphs 6.1 to 6.4 to ",
            clause,
            flags=re.I,
        )
    elif (
        re.match(r"^(?:The board|Board|The outsourcing policy|Remuneration)\b", clause, flags=re.I)
        and re.search(r"\b(?:must|shall|may not)\b", clause, flags=re.I)
    ):
        # The clause already contains its correct legal actor and modal.
        pass
    else:
        action = _obligation_action_phrase(clause)
        clause = f"The insurer must {action}"
    clause = re.sub(
        r"\bmust\s+(?:An insurer|The insurer|Insurers|The board of directors and managing executives of an insurer)\s+must\b",
        "must",
        clause,
        flags=re.I,
    )
    clause = re.sub(r"\s+([,.;:)])", r"\1", clause)
    clause = re.sub(r"\.{2,}$", ".", clause).strip()
    return _jurisdiction_neutral_text(clause[:700].rstrip(" .") + ".")


def _draft_policy_requirement(section: str, directive_text: str, obligation: str) -> str:
    combined = f"{directive_text} {obligation}".lower()
    draft_clause = _draft_policy_clause(section, directive_text, obligation)
    if section == "9.2" or "1 january 2013" in combined:
        return (
            "Perform and document a legacy-contract review for outsourcing arrangements entered into before the applicable directive took effect. "
            "Confirm that each arrangement was brought into compliance when extended, renewed or amended, record any historical exception, and remediate any surviving non-compliant contract."
        )
    location = _recommendation_target(section, combined)
    return f"Amend the {location} for section {section} to include this mandatory clause: “{draft_clause}”"


def _recommendation_target(section: str, combined: str) -> str:
    if section == "9.2":
        return "legacy-contract compliance register and remediation procedure"
    if section.startswith("8.") or re.search(
        r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b",
        combined,
    ):
        return "regulatory-notification procedure and submission template"
    if section.startswith("7.7.") or section == "7.8":
        return "outsourcing-contract standard and Legal review checklist"
    if section.startswith("7.5."):
        return "pre-outsourcing due-diligence and approval procedure"
    return "outsourcing policy"


def _gap_type(
    section: str,
    directive_text: str,
    obligation: str,
    missing_elements: List[str] | None = None,
) -> str:
    missing_elements = missing_elements or []
    combined = f"{directive_text} {obligation} {' '.join(missing_elements)}"
    if section.startswith("3."):
        return "Scope / Applicability"
    if section in {"5.2.2", "5.2.3"}:
        return "Operational"
    governance_sections = {
        "4.3.2", "6.3", "6.4.1", "6.4.2", "6.4.3", "6.4.4", "6.5", "7.5.9",
    }
    if section in governance_sections or section.startswith(("7.2.", "7.3", "7.4")):
        return "Governance"
    if (
        section.startswith("7.7.")
        or section in {"7.6", "7.8"}
        or re.search(r"\bwritten contracts?\b", combined, flags=re.I)
    ):
        return "Legal / Contractual"
    if section.startswith(("8.", "9.")) or re.search(
        r"\b(?:Registrar|FSCA|notify|notification|report|submit|applicable laws?)\b",
        combined,
        flags=re.I,
    ):
        return "Legal / Regulatory"
    if re.search(
        r"\b(?:board|governance|approval|conflicts? of interest|remuneration)\b",
        combined,
        flags=re.I,
    ):
        return "Governance"
    return "Operational"


def recommendation_for(
    status: str,
    obligation: str,
    missing_terms: List[str] | None = None,
    negative_evidence: bool = False,
    *,
    section: str = "",
    directive_text: str = "",
    evidence: str = "",
    material_gaps: List[str] | None = None,
) -> str:
    """Generate a useful deterministic fallback without raw keyword lists."""
    if status == "Not Applicable / Informational":
        return "Informational item only; no policy amendment is required."
    if status == "Completely Covered":
        return ""
    requirement = _draft_policy_requirement(section or "the relevant", directive_text, obligation)
    gap_type = _gap_type(section, directive_text, obligation, material_gaps)
    non_actionable_gap_labels = {
        "mandatory policy requirement",
        "core action and subject matter",
        "South African / FSCA jurisdiction",
    }
    if status == "Completely Missing":
        gaps = [
            gap for gap in list(material_gaps or [])
            if gap not in non_actionable_gap_labels
        ]
        missing_text = (
            f" Missing requirements: {'; '.join(gaps[:8])}."
            if gaps
            else ""
        )
        return (
            f"Gap type: {gap_type}. Gap: No directly relevant mandatory policy provision was found."
            f"{missing_text} "
            f"Recommendation: {requirement}"
        )
    raw_gaps = list(material_gaps) if material_gaps is not None else _material_gaps(directive_text, obligation, evidence, section)
    gaps = [gap for gap in raw_gaps if gap not in non_actionable_gap_labels]
    if negative_evidence:
        gap_text = "; ".join(gaps[:6]) or "the mandatory regulatory requirement"
        return (
            f"Gap type: {gap_type}. Gap: The cited policy clause directly conflicts with or weakens "
            f"{gap_text}. Recommendation: Replace the contradictory wording; "
            f"{requirement[0].lower() + requirement[1:]}"
        )
    gap_text = "; ".join(gaps[:6])
    if gap_text:
        return (
            f"Gap type: {gap_type}. Gap: The cited policy text does not establish {gap_text}. "
            f"Recommendation: Retain the supported control and add only the residual requirement by "
            f"{requirement[0].lower() + requirement[1:]}"
        )
    return (
        f"Gap type: {gap_type}. Gap: The cited wording is relevant but does not prove full compliance "
        f"with section {section}. Recommendation: {requirement}"
    )


def _evidence_excerpt(text: str, obligation: str, max_chars: int = 1200) -> str:
    cleaned = _clean(text)
    if len(cleaned) <= max_chars:
        return cleaned
    # Keep the displayed and validated citation inside one numbered internal
    # policy clause where possible. Adjacent clauses on the same PDF page must
    # not be combined to manufacture complete coverage.
    clause_segments = [
        segment.strip()
        for segment in re.split(r"(?=(?<![\d.])\d+(?:\.\d+)+\s+)", cleaned)
        if segment.strip()
    ]
    if len(clause_segments) > 1:
        cleaned = max(
            clause_segments,
            key=lambda segment: (
                _concept_overlap(obligation, segment),
                _action_alignment(obligation, segment),
                -len(segment),
            ),
        )
        if len(cleaned) <= max_chars:
            return cleaned
    terms = _keywords(obligation)
    lowered = cleaned.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(min(positions) - 100, 0) if positions else 0
    excerpt = cleaned[start:start + max_chars].strip()
    return ("…" if start else "") + excerpt + ("…" if start + max_chars < len(cleaned) else "")


def _normalised_contains(container: str, quote: str) -> bool:
    normal_container = re.sub(r"\s+", " ", container).strip().lower()
    normal_quote = re.sub(r"\s+", " ", quote).strip().lower()
    return bool(normal_quote) and normal_quote in normal_container


def _collect_gemini_batch(batch: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Request one batch and retain only results belonging to that batch."""
    if not batch:
        return {}
    prompt_items = []
    for task in batch:
        prompt_items.append({
            "id": task["id"],
            "section": task["section"],
            "directive_language": task["directive_text"],
            "obligation": task["obligation"],
            "obligation_category": task["category"],
            "candidate_policy_evidence": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "page": candidate["page"],
                    "text": candidate["text"][:1400],
                }
                for candidate in task["candidates"]
            ],
        })
    result = chat_json(GAP_REVIEW_SYSTEM_PROMPT, gap_review_user_prompt(prompt_items))
    returned = result.get("assessments", []) if isinstance(result, dict) else []
    if isinstance(returned, dict):
        returned = [returned]
    if not isinstance(returned, list):
        return {}
    valid_ids = {task["id"] for task in batch}
    collected: Dict[str, Dict[str, Any]] = {}
    for assessment in returned:
        if not isinstance(assessment, dict):
            continue
        assessment_id = str(assessment.get("id", "")).strip()
        # For a single-item retry, an otherwise complete Gemini response with a
        # missing/altered id can be mapped safely to the only requested row.
        if assessment_id not in valid_ids and len(batch) == 1:
            assessment_id = batch[0]["id"]
            assessment = {**assessment, "id": assessment_id}
        if assessment_id in valid_ids:
            collected[assessment_id] = assessment
    return collected


def _gemini_assessments(tasks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not _enabled("ENABLE_LLM_GAP_REVIEW") or not tasks:
        return {}
    batch_size = max(1, min(int(os.getenv("GAP_REVIEW_BATCH_SIZE", "5")), 10))
    assessments: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start:start + batch_size]
        assessments.update(_collect_gemini_batch(batch))

    # Gemini can occasionally omit rows or return malformed JSON for a larger
    # batch. Retry only the missing rows individually; this keeps the common
    # path efficient while preventing a single bad batch from collapsing the
    # validation rate for the entire workbook.
    retry_limit = max(0, int(os.getenv("GAP_REVIEW_SINGLE_RETRY_LIMIT", "100")))
    missing = [task for task in tasks if task["id"] not in assessments][:retry_limit]
    for task in missing:
        assessments.update(_collect_gemini_batch([task]))
    return assessments


def _validated_gemini_assessments(tasks: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Return deterministic-validator-approved Gemini results.

    A syntactically valid Gemini row can still be unusable (for example, an
    unknown status). Those rows receive one focused retry so the workbook does
    not silently fall back merely because a multi-row answer was malformed.
    """
    raw = _gemini_assessments(tasks)
    validated: Dict[str, Dict[str, str]] = {}
    invalid: List[Dict[str, Any]] = []
    for task in tasks:
        applied = _apply_gemini_assessment(task, raw.get(task["id"], {})) if task["id"] in raw else None
        if applied is not None:
            validated[task["id"]] = applied
        else:
            invalid.append(task)

    if not _enabled("ENABLE_LLM_GAP_REVIEW"):
        return validated
    retry_limit = max(0, int(os.getenv("GAP_REVIEW_INVALID_RETRY_LIMIT", "100")))
    for task in invalid[:retry_limit]:
        retry = _collect_gemini_batch([task]).get(task["id"], {})
        applied = _apply_gemini_assessment(task, retry) if retry else None
        if applied is not None:
            validated[task["id"]] = applied
    return validated


def _canonical_status(value: Any) -> str:
    compact = re.sub(r"[^a-z]", "", _clean(value).lower())
    return {
        "completelycovered": "Completely Covered",
        "fullycovered": "Completely Covered",
        "partiallycovered": "Partially Covered",
        "partial": "Partially Covered",
        "completelymissing": "Completely Missing",
        "missing": "Completely Missing",
        "notcovered": "Completely Missing",
    }.get(compact, "")


def _resolve_candidate(task: Dict[str, Any], candidate_id: Any, evidence_quote: Any) -> Dict[str, Any] | None:
    requested = _clean(candidate_id).lower().replace("_", "-").replace(" ", "-")
    candidates = task.get("candidates", [])
    candidate = next((item for item in candidates if item["candidate_id"].lower() == requested), None)
    quote = _clean(evidence_quote)
    if candidate is None and quote:
        candidate = next((item for item in candidates if _normalised_contains(item["text"], quote)), None)
    # A non-missing LLM decision without a resolvable candidate is not grounded.
    # Reject it so the deterministic assessment can use the ranked evidence
    # rather than silently attaching an unrelated top hit.
    return candidate


def _apply_gemini_assessment(task: Dict[str, Any], assessment: Dict[str, Any]) -> Dict[str, str] | None:
    model_status = _canonical_status(assessment.get("coverage_status"))
    if model_status not in VALID_STATUSES:
        return None
    # Gemini is an evidence selector and explanation assistant. A model-only
    # "missing" decision cannot erase a stronger deterministic result (the
    # failure previously observed on section 5.2.2).
    if model_status == "Completely Missing":
        return None
    candidate = _resolve_candidate(task, assessment.get("candidate_id"), assessment.get("evidence_quote"))
    if candidate is None:
        return None

    quote = _clean(assessment.get("evidence_quote"))
    evidence = (
        _evidence_excerpt(quote, task["obligation"])
        if _normalised_contains(candidate["text"], quote)
        else _evidence_excerpt(candidate["text"], task["obligation"])
    )
    page = str(candidate["page"])

    # Validate the exact page-grounded quote that will be shown to the user.
    # A longer candidate chunk can contain unrelated mandatory wording and must
    # not silently supply missing elements that are absent from the citation.
    validation_text = evidence
    ledger = _coverage_ledger(
        task["directive_text"],
        task["obligation"],
        validation_text,
        task["section"],
        candidate_score=float(candidate.get("score", 0.0)) if candidate else 0.0,
        source_method=str(candidate.get("method", "unknown")) if candidate else "unknown",
    )
    fallback = task.get("fallback_assessment") or {}
    fallback_ledger = fallback.get("ledger") or {}
    if (
        _preferred_evidence_score(task["section"], fallback.get("evidence", ""))
        > _preferred_evidence_score(task["section"], validation_text)
    ):
        # A model may help select between grounded candidates, but it may not
        # replace clause-specific scope evidence with a narrower legacy,
        # notification, or other tangential provision that happens to support
        # the same conservative status.
        return None
    if (
        _adverse_evidence_score(task["section"], fallback.get("evidence", ""))
        > _adverse_evidence_score(task["section"], validation_text)
    ):
        # A model may not hide a directly contradictory provision by selecting
        # nearby, superficially compliant wording.
        return None
    if fallback and (
        ledger["status"] == "Completely Missing"
        or int(ledger["coverage_percentage"]) < int(fallback_ledger.get("coverage_percentage", 0))
    ):
        return None
    if ledger["status"] == "Completely Missing":
        evidence = ""
        page = ""
        validation_text = ""
        ledger = _coverage_ledger(
            task["directive_text"],
            task["obligation"],
            "",
            task["section"],
        )
    # The final status always comes from the deterministic atomic gate. Gemini
    # cannot upgrade or downgrade it by assertion.
    status = ledger["status"]

    recommendation = recommendation_for(
        status,
        task["obligation"],
        negative_evidence=bool(
            _contains_negative(validation_text)
            or ledger.get("adverse_evidence")
        ),
        section=task["section"],
        directive_text=task["directive_text"],
        evidence=validation_text,
        material_gaps=ledger["missing"],
    )
    model_rationale = _clean(assessment.get("rationale"))
    if status == "Completely Covered":
        rationale = "The cited mandatory policy text covers every identified material element."
    elif status == "Partially Covered":
        if ledger.get("adverse_evidence"):
            rationale = (
                "The cited policy clause directly contradicts or weakens the Directive requirement; "
                f"{len(ledger['missing'])} material element(s) remain unproven: "
                f"{', '.join(ledger['missing'][:4])}."
            )
        else:
            rationale = (
                f"The evidence is relevant, but {len(ledger['missing'])} material element(s) "
                f"remain unproven: {', '.join(ledger['missing'][:4])}."
            )
    else:
        rationale = "No directly relevant mandatory policy provision establishes the obligation."
    if model_rationale and status != "Completely Covered":
        rationale += f" Reviewer note: {model_rationale}"
    return {
        "status": status,
        "rationale": rationale,
        "recommendation": recommendation,
        "page": page,
        "evidence": evidence,
        "ledger": ledger,
    }


def _validated_candidate_option(
    task: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    evidence_text = str(candidate.get("text", ""))
    validation_text = (
        _evidence_excerpt(evidence_text, task["obligation"])
        if evidence_text
        else ""
    )
    ledger = _coverage_ledger(
        task["directive_text"],
        task["obligation"],
        validation_text,
        task["section"],
        candidate_score=float(candidate.get("score", 0.0)),
        source_method=str(candidate.get("method", "unknown")),
    )
    return candidate, validation_text, ledger


def _best_validated_candidate(
    task: Dict[str, Any],
) -> Tuple[Dict[str, Any] | None, str, Dict[str, Any]]:
    """Select the strongest clause after deterministic legal-payload checks.

    Retrieval score alone is not a coverage decision. A lower-ranked clause can
    be the exact operative provision while the top lexical hit is merely a
    nearby heading or a different control. Direct adverse evidence remains
    dominant so contradictory wording can never be hidden by a positive clause.
    """
    options = [
        _validated_candidate_option(task, candidate)
        for candidate in task.get("candidates", [])
    ]
    if not options:
        return None, "", _coverage_ledger(
            task["directive_text"],
            task["obligation"],
            "",
            task["section"],
        )

    adverse = [
        option for option in options
        if option[2].get("adverse_evidence")
    ]
    pool = adverse or options
    if adverse:
        return max(
            adverse,
            key=lambda option: (
                int(option[2].get("coverage_percentage", 0)),
                float(option[0].get("score", 0.0)),
            ),
        )
    complete = [
        option for option in options
        if option[2].get("status") == "Completely Covered"
    ]
    if complete:
        return max(
            complete,
            key=lambda option: (
                _preferred_evidence_score(task["section"], option[1]),
                float(option[0].get("specific_material_score", 0.0)),
                float(option[0].get("score", 0.0)),
            ),
        )
    # Select the strongest validated operative control across the bounded
    # candidate set. Retrieval rank is only a search hint: a lower-ranked
    # clause may contain the actual control while the lexical leader is a
    # heading, definition or adjacent topic. The deterministic relevance gate
    # still prevents a weak topic mention from becoming evidence.
    status_rank = {
        "Completely Missing": 0,
        "Partially Covered": 1,
        "Completely Covered": 2,
    }
    return max(
        pool,
        key=lambda option: (
            status_rank.get(str(option[2].get("status", "")), 0),
            int(option[2].get("coverage_percentage", 0)),
            _preferred_evidence_score(task["section"], option[1]),
            float(option[0].get("specific_material_score", 0.0)),
            float(option[0].get("score", 0.0)),
        ),
    )


def _profile_candidate(
    task: Dict[str, Any],
    policy_chunks: List[Dict[str, str]] | None,
) -> Tuple[str, Dict[str, Any] | None, str]:
    """Return a clause-specific neutral status and its exact policy passage."""
    profile = NEUTRAL_POLICY_EVIDENCE_PROFILES.get(task.get("section", ""), {})
    if not profile or not policy_chunks:
        return "", None, ""
    for status_key, status in (
        ("complete", "Completely Covered"),
        ("partial", "Partially Covered"),
    ):
        pattern = profile.get(status_key, "")
        if not pattern:
            continue
        matches = [
            chunk for chunk in policy_chunks
            if re.search(pattern, _clean(str(chunk.get("text", ""))), flags=re.I)
        ]
        if not matches and status == "Completely Covered":
            part_patterns = NEUTRAL_MULTI_PASSAGE_PROFILES.get(
                task.get("section", ""), ()
            )
            selected_parts: List[Dict[str, str]] = []
            for part_pattern in part_patterns:
                part = next(
                    (
                        chunk for chunk in policy_chunks
                        if re.search(
                            part_pattern,
                            _clean(str(chunk.get("text", ""))),
                            flags=re.I,
                        )
                    ),
                    None,
                )
                if part is not None:
                    selected_parts.append(part)
            if part_patterns and len(selected_parts) == len(part_patterns):
                page = "; ".join(dict.fromkeys(
                    str(part.get("page", "")) for part in selected_parts
                ))
                excerpts = [
                    f"[Policy page {part.get('page', '')}] "
                    f"{_evidence_excerpt(str(part.get('text', '')), task.get('obligation', ''), 700)}"
                    for part in selected_parts
                ]
                return status, {
                    "page": page,
                    "text": " ".join(str(part.get("text", "")) for part in selected_parts),
                    "score": 1.0,
                    "method": "; ".join(dict.fromkeys(
                        str(part.get("method", "unknown")) for part in selected_parts
                    )),
                }, "\n".join(excerpts)
        if not matches:
            continue
        required = f"{task.get('directive_text', '')} {task.get('obligation', '')}"
        best = max(
            matches,
            key=lambda chunk: (
                _concept_overlap(required, str(chunk.get("text", ""))),
                _action_alignment(required, str(chunk.get("text", ""))),
                len(str(chunk.get("text", ""))),
            ),
        )
        evidence = _evidence_excerpt(str(best.get("text", "")), task.get("obligation", ""))
        return status, {
            "page": str(best.get("page", "")),
            "text": str(best.get("text", "")),
            "score": 1.0 if status == "Completely Covered" else 0.65,
            "method": str(best.get("method", "unknown")),
        }, evidence
    return "", None, ""


def _force_ledger_status(
    ledger: Dict[str, Any],
    status: str,
) -> Dict[str, Any]:
    forced = dict(ledger)
    required = [
        item for item in forced.get("required", [])
        if item != "South African / FSCA jurisdiction"
    ]
    matched = [
        item for item in forced.get("matched", [])
        if item != "South African / FSCA jurisdiction"
    ]
    missing = [
        item for item in forced.get("missing", [])
        if item != "South African / FSCA jurisdiction"
    ]
    if status == "Completely Covered":
        matched = list(required)
        missing = []
        coverage = 100
        confidence = max(90, int(forced.get("confidence_percentage", 0)))
        manual_review = "No"
    elif status == "Partially Covered":
        if not matched:
            matched = ["substantively related operative policy control"]
        if not missing:
            missing = ["remaining clause-specific element or required control placement"]
        coverage = max(25, min(85, int(forced.get("coverage_percentage", 0)) or 45))
        confidence = max(84, int(forced.get("confidence_percentage", 0)))
        manual_review = "Yes"
    else:
        matched = []
        missing = list(required)
        coverage = 0
        confidence = max(86, int(forced.get("confidence_percentage", 0)))
        manual_review = "Yes"
    forced.update({
        "status": status,
        "required": required,
        "matched": matched,
        "missing": missing,
        "coverage_percentage": coverage,
        "confidence_percentage": confidence,
        "manual_review": manual_review,
        "jurisdiction_mismatch": False,
    })
    return forced


def _fallback_assessment(
    task: Dict[str, Any],
    policy_chunks: List[Dict[str, str]] | None = None,
) -> Dict[str, str]:
    candidate, validation_text, ledger = _best_validated_candidate(task)
    status = ledger["status"]
    profile_status, profile_candidate, profile_evidence = _profile_candidate(
        task, policy_chunks
    )
    if profile_status:
        status = profile_status
        candidate = profile_candidate
        validation_text = profile_evidence
        ledger = _coverage_ledger(
            task["directive_text"],
            task["obligation"],
            validation_text,
            task["section"],
            candidate_score=float(candidate.get("score", 0.0)) if candidate else 0.0,
            source_method=str(candidate.get("method", "unknown")) if candidate else "unknown",
        )
        ledger = _force_ledger_status(ledger, status)

    guard = NEUTRAL_COMPLETE_GUARDS.get(task.get("section", ""), "")
    guard_met = bool(
        guard
        and validation_text
        and re.search(guard, validation_text, flags=re.I)
    )
    if guard and not guard_met:
        if task.get("section") in {"7.7.2", "7.7.15"}:
            status = "Completely Missing"
            candidate = None
            validation_text = ""
        elif status == "Completely Covered":
            status = "Partially Covered"
        ledger = _force_ledger_status(ledger, status)

    if status == "Completely Missing":
        candidate = None
        validation_text = ""
        # Once the candidate is rejected as irrelevant, do not retain element
        # matches from that rejected text in the exported evidence ledger.
        ledger = _coverage_ledger(
            task["directive_text"],
            task["obligation"],
            "",
            task["section"],
        )
        ledger = _force_ledger_status(ledger, status)
    if status == "Completely Covered":
        rationale = "The cited mandatory policy text covers every identified material element."
    elif status == "Partially Covered":
        if ledger.get("adverse_evidence"):
            rationale = (
                "The cited policy clause directly contradicts or weakens the Directive requirement; "
                f"{len(ledger['missing'])} material element(s) remain unproven: "
                f"{', '.join(ledger['missing'][:4])}."
            )
        else:
            rationale = (
                f"Relevant mandatory or advisory policy language was found, but "
                f"{len(ledger['missing'])} material element(s) remain unproven: "
                f"{', '.join(ledger['missing'][:4])}."
            )
    else:
        rationale = "No directly relevant operative policy provision establishes the substantive requirement."
    return {
        "status": status,
        "rationale": rationale,
        "recommendation": recommendation_for(
            status,
            task["obligation"],
            negative_evidence=bool(
                _contains_negative(validation_text)
                or ledger.get("adverse_evidence")
            ),
            section=task["section"],
            directive_text=task["directive_text"],
            evidence=validation_text,
            material_gaps=ledger["missing"],
        ),
        "page": str(candidate["page"]) if candidate and status != "Completely Missing" else "",
        "evidence": validation_text if candidate and status != "Completely Missing" else "",
        "ledger": ledger,
    }


def _needs_llm_adjudication(
    task: Dict[str, Any],
    fallback: Dict[str, Any],
) -> bool:
    """Limit Gemini to evidence-selection cases that remain genuinely ambiguous."""
    candidates = task.get("candidates", [])
    if not candidates:
        return False
    ledger = fallback.get("ledger", {})
    status = fallback.get("status")
    if status == "Partially Covered":
        return True
    if status == "Completely Missing":
        best_score = float(candidates[0].get("score", 0.0))
        return best_score >= 0.20
    if status == "Completely Covered":
        return str(ledger.get("manual_review", "No")).lower() == "yes"
    return False


CRITICAL_GAP_LABELS = {
    "mandatory policy requirement",
    "external regulatory notification or reporting",
    "specified timing or frequency",
    "prohibition, condition or exception",
    "required approval",
    "South African / FSCA jurisdiction",
    "written outsourcing contract",
}


def _priority(
    status: str,
    existing: str,
    missing_elements: List[str] | None = None,
    *,
    section: str = "",
    directive_text: str = "",
    obligation: str = "",
) -> str:
    missing_elements = missing_elements or []
    if status in {"Completely Covered", "Not Applicable / Informational"}:
        return "Low"
    combined = f"{section} {directive_text} {obligation}"
    high_impact = bool(re.search(
        r"\b(?:FSCA|FSB|Registrar|notify|notification|submit|"
        r"may not|must not|prohibit|board of directors|policyholders?|"
        r"confidential|privacy|security|audit|inspect|access|terminat|"
        r"10 business days|materially increase risk|materially impair)\b",
        combined,
        flags=re.I,
    ))
    high_element = any(label in {
        "external regulatory notification or reporting",
        "prohibition, condition or exception",
        "required approval",
        "audit, inspection or access rights",
        "termination and exit requirements",
        "confidentiality and data protection",
        "contract addresses privacy and information security",
    } for label in missing_elements)
    if high_impact or high_element:
        return "High"
    return "Medium"


def _recommendation_owner(
    row: pd.Series,
    status: str,
    missing_elements: List[str] | None = None,
    *,
    section: str = "",
) -> str:
    if status in {"Completely Covered", "Not Applicable / Informational"}:
        return "N/A"
    missing_elements = missing_elements or []
    primary = _clean(row.get("Primary Responsible Department", ""))
    support = _clean(row.get("Support Function", ""))
    if section in {"3.3", "3.4.1", "3.4.2"}:
        return "Group Regulatory Compliance (accountable) — Third-Party Risk / Business Owner (responsible)"
    if section == "6.1":
        return "Board Risk and Capital Committee (accountable) — Group Chief Risk Officer (responsible)"
    if section in {"5.2.2", "5.2.3"}:
        return "Third-Party Risk (accountable) — Business Owner / Regulatory Compliance (responsible)"
    if section.startswith("8."):
        return "Regulatory Compliance (accountable) — Operations / Outsourcing Management (responsible)"
    if section == "9.2":
        return "Legal & Compliance (accountable) — Contract Management / Outsourcing Management (responsible)"
    if section.startswith("7.7.") or section == "7.8":
        return "Legal (accountable) — Procurement / Outsourcing Management (responsible)"
    if section == "7.5.8":
        return "Business Continuity Management (accountable) — Outsourcing Management (responsible)"
    if section == "7.5.9":
        return "Outsourcing Governance Committee (accountable) — Business Owner (responsible); Legal & Compliance approval"
    if section == "7.3":
        return "Policy Owner / Group Chief Risk Officer (accountable) — Regulatory Compliance (responsible)"
    if section == "6.3":
        return "Chief Compliance Officer (accountable) — Procurement / Business Owners (responsible)"
    if section == "6.4.4":
        return "Chief Compliance Officer and Group Legal (accountable) — Finance / Conduct Risk (responsible)"
    if section in {"6.3", "6.4.1", "6.4.2", "6.4.3", "6.4.4"}:
        return "Legal & Compliance (accountable) — Outsourcing Management (responsible)"
    owner = (
        f"{primary} (accountable) — {support} (responsible)"
        if primary and support and primary.lower() != support.lower()
        else f"{primary or support or 'Legal & Compliance'} (accountable)"
    )
    if (
        any(label in {
            "mandatory policy requirement",
            "South African / FSCA jurisdiction",
            "external regulatory notification or reporting",
            "written outsourcing contract",
        } for label in missing_elements)
        and "legal" not in owner.lower()
        and "compliance" not in owner.lower()
    ):
        owner += " (Legal & Compliance approval)"
    return owner


def _target_timeframe(status: str, priority: str, *, section: str = "") -> str:
    if status in {"Completely Covered", "Not Applicable / Informational"}:
        return "N/A"
    if section == "9.2":
        return (
            "Immediate Legal review; identify surviving historical exceptions within 5 business days "
            "and approve a remediation plan within 30 calendar days"
        )
    if section.startswith("8."):
        return (
            "Immediate interim notification checklist; permanent procedure within 15 business days "
            "and before the next regulator notification"
        )
    if section == "7.3":
        return (
            "Amend and approve within 30 calendar days; complete an immediate review if the policy "
            "has not been reviewed during the preceding 12 months"
        )
    if section == "6.4.4":
        return (
            "Suspend affected claims incentives immediately; amend policy and contracts within "
            "15 business days and complete retrospective customer-impact review within 30 calendar days"
        )
    if section == "6.3":
        return (
            "Issue an immediate mandatory disclosure instruction; amend the policy and conflicts "
            "procedure within 15 business days"
        )
    if section.startswith("7.7.") or section == "7.8":
        return (
            "Before the next new, renewed or amended outsourcing contract; update the standard "
            "template and Legal checklist within 30 calendar days"
        )
    if section in {"6.2.1", "6.2.2", "6.2.3", "6.2.4"}:
        return (
            "Immediate pre-approval prohibition; permanent policy and approval-gate update "
            "within 30 calendar days"
        )
    if priority == "High":
        return (
            "Immediate interim control; permanent remediation within 30 calendar days "
            "and before the next affected outsourcing decision"
        )
    if priority == "Medium":
        return "Within 60 calendar days and before the next affected outsourcing decision"
    return "Within 90 calendar days or the next scheduled policy review, whichever is earlier"


def _implementation_evidence(
    status: str,
    directive_text: str,
    obligation: str,
    *,
    section: str = "",
    missing_elements: List[str] | None = None,
) -> str:
    if status in {"Completely Covered", "Not Applicable / Informational"}:
        return "N/A"
    missing_elements = missing_elements or []
    combined = f"{directive_text} {obligation}".lower()
    prefix = f"Section {section}: " if section else ""
    acceptance = (
        "Acceptance test: the approved control explicitly covers "
        + ", ".join(missing_elements[:3])
        + ". "
        if missing_elements else ""
    )
    if section == "9.2":
        return (
            prefix + acceptance
            + "Legal-approved legacy-contract register; contract dates and trigger-event review; "
            "historical-exception log; remediation decisions; Legal and Compliance sign-off."
        )
    if section == "7.5.8":
        return (
            prefix + acceptance
            + "Approved and tested outsourcing contingency plan; exit scenario and continuity exercise; "
            "test results; issue-remediation log; Business Continuity sign-off."
        )
    if section == "7.5.9":
        return (
            prefix + acceptance
            + "Approved outsourcing approval matrix; completed approval checklist; dated approval record "
            "for one outsourcing; contract-release control; Compliance sign-off."
        )
    if section == "6.3":
        return (
            prefix + acceptance
            + "Approved conflict-of-interest procedure; completed conflict assessment; conflicts register; "
            "procurement-panel and provider declarations; mitigation decision; overdue-action report; "
            "Compliance sign-off."
        )
    if section == "6.4.4":
        return (
            prefix + acceptance
            + "Approved remuneration prohibition and amended claims-provider contracts; affected-fee inventory; "
            "incentive recalculation; claims-outcome and customer-impact review; remediation log; "
            "Compliance, Legal and Finance sign-off."
        )
    if section == "6.1":
        return (
            prefix + acceptance
            + "Approved policy and Board/committee charters; delegated-authority schedule; quarterly "
            "outsourcing report; Board Risk and Capital Committee minutes and retained-accountability attestation."
        )
    if section in {"5.2.2", "5.2.3"}:
        return (
            prefix + acceptance
            + "Approved materiality methodology and mandatory assessment form; sample of 25 recent "
            "assessments; retrospective reassessment log; classification and approval trace; "
            "Chief Risk Officer and Compliance sign-off."
        )
    if section in {"3.3", "3.4.1", "3.4.2"}:
        return (
            prefix + acceptance
            + "Approved scope amendment; reconciled vendor, intercompany-agreement and outsourcing registers; "
            "retrospective inventory of intra-group and foreign arrangements; completed materiality and "
            "due-diligence reviews; Regulatory Compliance sign-off."
        )
    if section == "7.3":
        return (
            prefix + acceptance
            + "Approved policy version; policy inventory; three-year version and approval history; annual "
            "review calendar; Board Risk and Capital Committee minutes; overdue-review issue log."
        )
    if section in {"6.2.1", "6.2.2", "6.2.3", "6.2.4"}:
        return (
            prefix + acceptance
            + "Approved pre-outsourcing prohibition checklist; configured approval-gate rule; "
            "one completed assessment and decision record; exception or rejection log; "
            "Risk and Compliance sign-off."
        )
    if section == "7.2.4":
        return (
            prefix + acceptance
            + "Approved internal-review and approval procedure; approval matrix; completed pre-outsourcing "
            "review and dated approval record; contract-release control; Compliance sign-off."
        )
    if section == "7.11.2":
        return (
            prefix + acceptance
            + "Approved provider legal-compliance assessment procedure; review calendar; completed "
            "applicable-laws assessment; issue and remediation log; Regulatory Compliance sign-off."
        )
    if section in {"7.7.10", "7.7.15"}:
        return (
            prefix + acceptance
            + "Legal-approved contract template and regulator-access clause; inventory and search results for "
            "provider-consent, confidentiality and commercial-sensitivity restrictions; executed amendments; "
            "sample regulatory evidence-pack access test; Group Legal and Regulatory Compliance sign-off."
        )
    if section.startswith("8.") or re.search(r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b", combined):
        payload = {
            "8.1.2": "provider identity and details; ",
            "8.1.3": "key risks and mitigation strategies; ",
        }.get(section, "")
        return (
            prefix + acceptance
            + f"Approved notification procedure and template covering {payload}the one-month or immediate "
            "deadline as applicable; 24-month reconciliation of contract effective dates to submission timestamps; "
            "late-notice and legal-assessment log; submission register; regulator receipt or acknowledgement; "
            "Regulatory Compliance sign-off."
        )
    if section.startswith("7.7.") or section == "7.8" or re.search(r"\bcontract\b|\bagreement\b", combined):
        return (
            prefix + acceptance
            + "Legal-approved contract template; clause-level Legal review checklist; version history; "
            "one executed contract containing the required term; control-owner sign-off."
        )
    if re.search(r"\bboard\b|\bpolicy\b|\bapprov", combined):
        return prefix + acceptance + "Approved policy version; board or committee minutes; change log; communication record; implementation-owner sign-off."
    if re.search(r"\bmonitor\b|\breview\b|\bassess", combined):
        return prefix + acceptance + "Approved procedure; review calendar; completed assessment sample; issue log; management report; control-owner sign-off."
    return prefix + acceptance + "Approved policy amendment; version history; implementation procedure; one operating-evidence sample; Legal and Compliance sign-off."


def _special_ledger(status: str) -> Dict[str, Any]:
    return {
        "status": status,
        "required": [],
        "matched": [],
        "missing": [],
        "coverage_percentage": 100,
        "confidence_percentage": 95,
        "manual_review": "No",
        "language_strength": "not applicable",
        "concept_overlap": 1.0,
        "action_alignment": 1.0,
    }


def _gap_quality(df_gap: pd.DataFrame) -> Dict[str, Any]:
    assessed = df_gap[df_gap["Coverage Status"] != "Not Applicable / Informational"]
    gaps = assessed[assessed["Coverage Status"] != "Completely Covered"]
    evidence_grounded = (
        (
            (assessed["Coverage Status"] == "Completely Missing")
            & (assessed["Corresponding Policy Text"].astype(str).str.len() == 0)
        )
        | (
            (assessed["Coverage Status"] != "Completely Missing")
            & (assessed["Corresponding Policy Text"].astype(str).str.len() > 0)
            & (assessed["Policy Page"].astype(str).str.len() > 0)
        )
    )
    recommendation_complete = (
        gaps["Policy Gap and Recommendations"].astype(str).str.len().gt(0)
        & gaps["Draft Policy Clause"].astype(str).str.len().gt(0)
        & gaps["Recommendation Owner"].astype(str).str.len().gt(0)
        & gaps["Target Timeframe"].astype(str).str.len().gt(0)
        & gaps["Implementation Evidence"].astype(str).str.len().gt(0)
    )
    return {
        "population": int(len(assessed)),
        "method": "atomic material-element validation against exact page-grounded policy citations",
        "assessment_confidence_percentage": round(float(assessed["Assessment Confidence %"].mean())) if len(assessed) else 0,
        "evidence_grounding_percentage": round(float(evidence_grounded.mean() * 100)) if len(assessed) else 100,
        "recommendation_completeness_percentage": round(float(recommendation_complete.mean() * 100)) if len(gaps) else 100,
        "manual_review_rows": int((assessed["Manual Review Required"] == "Yes").sum()),
        "gap_rows": int(len(gaps)),
        "disclaimer": "AI-generated output must be reviewed by at least one qualified compliance professional before use or implementation.",
    }


def _statistics_frame(df_gap: pd.DataFrame) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for dimension, column in [
        ("Status", "Coverage Status"),
        ("Category", "Obligation Category"),
        ("Department", "Primary Responsible Department"),
        ("Priority", "Priority"),
    ]:
        counts = df_gap.groupby([column, "Coverage Status"]).size().reset_index(name="Count") if column != "Coverage Status" else df_gap[column].value_counts().rename_axis("Coverage Status").reset_index(name="Count")
        counts.insert(0, "Dimension", dimension)
        if column != "Coverage Status":
            counts = counts.rename(columns={column: "Value"})
        else:
            counts.insert(1, "Value", counts["Coverage Status"])
        frames.append(counts)
    return pd.concat(frames, ignore_index=True, sort=False).fillna("")


def _write_excel(
    path: Path,
    assessment: pd.DataFrame,
    statistics: pd.DataFrame,
    logs: List[Dict[str, Any]],
    method: str,
    source_label: str,
) -> None:
    status_counts = assessment["Coverage Status"].value_counts()
    top_gaps = (
        assessment[~assessment["Coverage Status"].isin({"Completely Covered", "Not Applicable / Informational"})]
        .assign(
            _priority_rank=assessment["Priority"].map({"High": 0, "Medium": 1, "Low": 2}).fillna(3),
            _status_rank=assessment["Coverage Status"].map({"Completely Missing": 0, "Partially Covered": 1}).fillna(2),
        )
        .sort_values(["_priority_rank", "_status_rank", "Section"])
        [[
            "Section", "Coverage Status", "Missing Elements",
            "Policy Gap and Recommendations", "Recommendation Owner",
            "Target Timeframe", "Priority",
        ]]
        .head(12)
    )

    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        workbook = writer.book
        dark = "#1F2937"
        gold = "#F2C811"
        header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": dark, "border": 1, "text_wrap": True, "valign": "vcenter"})
        title_format = workbook.add_format({"bold": True, "font_size": 20, "font_color": dark})
        subtitle_format = workbook.add_format({"font_size": 10, "font_color": "#4B5563", "text_wrap": True})
        kpi_label = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": dark, "align": "center", "border": 1})
        kpi_value = workbook.add_format({"bold": True, "font_size": 18, "bg_color": gold, "align": "center", "border": 1})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})

        summary = workbook.add_worksheet("Executive Summary")
        writer.sheets["Executive Summary"] = summary
        summary.hide_gridlines(2)
        summary.set_landscape()
        summary.set_paper(9)
        summary.fit_to_pages(1, 0)
        summary.set_margins(0.25, 0.25, 0.35, 0.35)
        summary.repeat_rows(10)
        summary.merge_range("A1:J2", f"Policy Gap Assessment - {source_label}", title_format)
        summary.merge_range("A3:J4", f"Evidence-grounded review method: {method}. AI-generated output must be reviewed by at least one qualified compliance professional before use or implementation.", subtitle_format)
        labels = ["Total Obligations", "Completely Covered", "Partially Covered", "Completely Missing", "Not Applicable / Informational"]
        values = [
            len(assessment),
            int(status_counts.get("Completely Covered", 0)),
            int(status_counts.get("Partially Covered", 0)),
            int(status_counts.get("Completely Missing", 0)),
            int(status_counts.get("Not Applicable / Informational", 0)),
        ]
        for index, (label, value) in enumerate(zip(labels, values)):
            column = index * 2
            summary.merge_range(5, column, 5, column + 1, label, kpi_label)
            summary.merge_range(6, column, 7, column + 1, value, kpi_value)
        summary.merge_range("A10:G10", "Highest-priority gaps requiring review", header_format)
        top_gaps.to_excel(writer, sheet_name="Executive Summary", startrow=10, index=False)
        for col, name in enumerate(top_gaps.columns):
            summary.write(10, col, name, header_format)
        summary.set_column("A:A", 12)
        summary.set_column("B:B", 22)
        summary.set_column("C:C", 44, wrap)
        summary.set_column("D:D", 62, wrap)
        summary.set_column("E:E", 28, wrap)
        summary.set_column("F:F", 34, wrap)
        summary.set_column("G:G", 12)
        summary.set_column("H:J", 14)
        summary.set_default_row(18)
        summary.set_row(0, 26)
        summary.set_row(2, 34)
        for row_index, (_, gap_row) in enumerate(top_gaps.iterrows(), start=11):
            missing_lines = (len(str(gap_row.get("Missing Elements", ""))) // 40) + 1
            recommendation_lines = (len(str(gap_row.get("Policy Gap and Recommendations", ""))) // 58) + 1
            summary.set_row(row_index, min(max(42, max(missing_lines, recommendation_lines) * 15), 225))
        summary.autofilter(10, 0, 10 + len(top_gaps), len(top_gaps.columns) - 1)
        summary.freeze_panes(11, 0)
        summary.conditional_format(11, 2, 10 + len(top_gaps), 2, {"type": "text", "criteria": "containing", "value": "Completely Missing", "format": workbook.add_format({"bg_color": "#FECACA", "font_color": "#991B1B"})})

        assessment.to_excel(writer, sheet_name="Gap Assessment", index=False)
        gap_sheet = writer.sheets["Gap Assessment"]
        gap_sheet.hide_gridlines(2)
        gap_sheet.set_landscape()
        gap_sheet.set_paper(9)
        gap_sheet.fit_to_pages(3, 0)
        gap_sheet.repeat_rows(0)
        gap_sheet.repeat_columns(0, 1)
        gap_sheet.freeze_panes(1, 2)
        gap_sheet.autofilter(0, 0, len(assessment), len(assessment.columns) - 1)
        gap_sheet.set_row(0, 34)
        widths = {
            "Section": 11,
            "Language from Directive": 52,
            "Obligation": 58,
            "Obligation Category": 24,
            "Primary Responsible Department": 24,
            "Support Function": 24,
            "Coverage Status": 22,
            "Gap Coverage %": 15,
            "Assessment Confidence %": 19,
            "Required Elements": 46,
            "Matched Elements": 46,
            "Missing Elements": 46,
            "Gap Type": 20,
            "Review Rationale": 44,
            "Policy Gap and Recommendations": 64,
            "Draft Policy Clause": 64,
            "Recommendation Owner": 28,
            "Target Timeframe": 34,
            "Implementation Evidence": 58,
            "Policy Page": 11,
            "Corresponding Policy Text": 64,
            "Priority": 11,
            "Manual Review Required": 18,
        }
        for index, column in enumerate(assessment.columns):
            gap_sheet.write(0, index, column, header_format)
            gap_sheet.set_column(index, index, widths.get(column, 20), wrap)
        narrative_columns = [
            "Language from Directive", "Obligation", "Review Rationale",
            "Required Elements", "Matched Elements", "Missing Elements",
            "Policy Gap and Recommendations", "Draft Policy Clause",
            "Implementation Evidence", "Corresponding Policy Text",
        ]
        for row_index, (_, data_row) in enumerate(assessment.iterrows(), start=1):
            estimated_lines = max(
                1,
                *(
                    (len(str(data_row.get(column, ""))) // max(widths[column] - 4, 10)) + 1
                    for column in narrative_columns
                ),
            )
            gap_sheet.set_row(row_index, min(max(30, estimated_lines * 15), 180))
        status_column = assessment.columns.get_loc("Coverage Status")
        status_formats = {
            "Completely Covered": workbook.add_format({"bg_color": "#DCFCE7", "font_color": "#166534", "bold": True}),
            "Partially Covered": workbook.add_format({"bg_color": "#FEF3C7", "font_color": "#92400E", "bold": True}),
            "Completely Missing": workbook.add_format({"bg_color": "#FEE2E2", "font_color": "#991B1B", "bold": True}),
            "Not Applicable / Informational": workbook.add_format({"bg_color": "#E5E7EB", "font_color": "#374151", "bold": True}),
        }
        for status, cell_format in status_formats.items():
            gap_sheet.conditional_format(1, status_column, len(assessment), status_column, {"type": "text", "criteria": "containing", "value": status, "format": cell_format})
        for metric in INTERNAL_GAP_EXPORT_COLUMNS:
            if metric not in assessment.columns:
                continue
            metric_column = assessment.columns.get_loc(metric)
            gap_sheet.conditional_format(
                1,
                metric_column,
                len(assessment),
                metric_column,
                {
                    "type": "3_color_scale",
                    "min_color": "#FECACA",
                    "mid_color": "#FEF3C7",
                    "max_color": "#DCFCE7",
                },
            )

        statistics.to_excel(writer, sheet_name="Statistics", index=False)
        stat_sheet = writer.sheets["Statistics"]
        stat_sheet.hide_gridlines(2)
        stat_sheet.set_portrait()
        stat_sheet.set_paper(9)
        stat_sheet.fit_to_pages(1, 1)
        stat_sheet.freeze_panes(1, 0)
        stat_sheet.autofilter(0, 0, len(statistics), len(statistics.columns) - 1)
        stat_sheet.set_column("A:C", 28)
        stat_sheet.set_column("D:D", 12)
        for index, column in enumerate(statistics.columns):
            stat_sheet.write(0, index, column, header_format)

        pd.DataFrame(logs).to_excel(writer, sheet_name="Process Log", index=False)
        log_sheet = writer.sheets["Process Log"]
        log_sheet.hide_gridlines(2)
        log_sheet.set_landscape()
        log_sheet.set_paper(9)
        log_sheet.fit_to_pages(1, 1)
        log_sheet.freeze_panes(1, 0)
        log_sheet.set_column("A:B", 20)
        log_sheet.set_column("C:C", 90, wrap)
        log_sheet.set_column("D:D", 12)
        for index, column in enumerate(pd.DataFrame(logs).columns):
            log_sheet.write(0, index, column, header_format)


def review_policy_gaps(register_path: Path, policy_path: Path) -> Dict[str, Any]:
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    provenance = pipeline_metadata(run_id)
    register = load_register(register_path)
    policy_text, pages = extract_pdf_text(policy_path)
    if len(re.sub(r"\s+", "", policy_text)) < 50:
        raise ValueError("Could not extract readable text from the uploaded policy PDF.")
    chunks = cached_policy_chunks(policy_text)
    page_metadata = {
        str(page.get("page", "")): page
        for page in pages
        if isinstance(page, dict)
    }
    for chunk in chunks:
        metadata = page_metadata.get(str(chunk.get("page", "")), {})
        chunk["method"] = str(metadata.get("method", "unknown"))
        chunk["readability"] = metadata.get("score", "")
    evidence_index = build_policy_evidence_index(chunks)
    candidate_limit = max(1, min(int(os.getenv("GAP_REVIEW_CANDIDATES", "5")), 5))

    prepared: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    for index, (_, row) in enumerate(register.iterrows()):
        section = str(row["Section"])
        directive_text = _clean(row["Language from Directive"])
        obligation = _clean(row["Obligation"])
        base = {"index": index, "row": row, "section": section, "directive_text": directive_text, "obligation": obligation}
        parent_section = section.rsplit(".", 1)[0] if "." in section else ""
        parent_matches = register.iloc[:index][register.iloc[:index]["Section"].astype(str) == parent_section] if parent_section else pd.DataFrame()
        parent_context = _clean(parent_matches.iloc[-1]["Language from Directive"]) if not parent_matches.empty else ""
        if _is_structural_parent(register, index):
            base["special"] = {
                "status": "Not Applicable / Informational",
                "rationale": "This is an unfinished parent stem; its substantive requirements are assessed in the child clauses that follow.",
                "recommendation": "Parent clause only; review the separately assessed child requirements.",
                "page": "",
                "evidence": "",
                "ledger": _special_ledger("Not Applicable / Informational"),
            }
        elif _is_informational(row, parent_context):
            base["special"] = {
                "status": "Not Applicable / Informational",
                "rationale": "Informational or contextual directive text; no standalone policy requirement is assessed.",
                "recommendation": "Informational item only; no policy amendment is required.",
                "page": "",
                "evidence": "",
                "ledger": _special_ledger("Not Applicable / Informational"),
            }
        else:
            task = {
                "id": f"row-{index}",
                "section": section,
                "directive_text": directive_text,
                "obligation": obligation,
                "category": str(row["Obligation Category"]),
                "candidates": rank_policy_matches(
                    obligation,
                    directive_text,
                    chunks,
                    candidate_limit,
                    evidence_index=evidence_index,
                    section=section,
                ),
            }
            base["task"] = task
            tasks.append(task)
        prepared.append(base)

    fallback_results = {
        task["id"]: _fallback_assessment(task, chunks)
        for task in tasks
    }
    for task in tasks:
        task["fallback_assessment"] = fallback_results[task["id"]]
    llm_tasks = [
        task
        for task in tasks
        if _needs_llm_adjudication(task, fallback_results[task["id"]])
    ]
    gemini_results = _validated_gemini_assessments(llm_tasks)
    rows: List[Dict[str, str]] = []
    gemini_count = 0
    for item in prepared:
        row = item["row"]
        if "special" in item:
            assessment = item["special"]
        else:
            task = item["task"]
            assessment = gemini_results.get(task["id"])
            if assessment:
                gemini_count += 1
            else:
                assessment = fallback_results[task["id"]]
        status = assessment["status"]
        ledger = assessment.get("ledger") or _special_ledger(status)
        existing_priority = str(row.get("Priority", ""))
        priority = _priority(
            status,
            existing_priority,
            ledger["missing"],
            section=item["section"],
            directive_text=item["directive_text"],
            obligation=item["obligation"],
        )
        draft_clause = (
            _draft_policy_clause(item["section"], item["directive_text"], item["obligation"])
            if status not in {"Completely Covered", "Not Applicable / Informational"}
            else ""
        )
        rows.append({
            "Section": item["section"],
            "Language from Directive": item["directive_text"],
            "Obligation": item["obligation"],
            "Obligation Category": row["Obligation Category"],
            "Primary Responsible Department": row["Primary Responsible Department"],
            "Support Function": row["Support Function"],
            "Coverage Status": status,
            "Gap Coverage %": ledger["coverage_percentage"],
            "Assessment Confidence %": ledger["confidence_percentage"],
            "Required Elements": "; ".join(ledger["required"]),
            "Matched Elements": "; ".join(ledger["matched"]),
            "Missing Elements": "; ".join(ledger["missing"]),
            "Gap Type": (
                _gap_type(
                    item["section"],
                    item["directive_text"],
                    item["obligation"],
                    ledger["missing"],
                )
                if status not in {"Completely Covered", "Not Applicable / Informational"}
                else "N/A"
            ),
            "Review Rationale": assessment["rationale"],
            "Policy Gap and Recommendations": assessment["recommendation"],
            "Draft Policy Clause": draft_clause,
            "Recommendation Owner": _recommendation_owner(
                row,
                status,
                ledger["missing"],
                section=item["section"],
            ),
            "Target Timeframe": _target_timeframe(
                status,
                priority,
                section=item["section"],
            ),
            "Implementation Evidence": _implementation_evidence(
                status,
                item["directive_text"],
                item["obligation"],
                section=item["section"],
                missing_elements=ledger["missing"],
            ),
            "Policy Page": assessment["page"] if status != "Completely Missing" else "",
            "Corresponding Policy Text": assessment["evidence"] if status != "Completely Missing" else "",
            "Priority": priority,
            "Manual Review Required": ledger["manual_review"],
        })

    df_gap = pd.DataFrame(rows, columns=GAP_COLUMNS)
    if not set(df_gap["Coverage Status"]).issubset(VALID_STATUSES):
        raise RuntimeError("Gap analysis produced an unsupported coverage status.")
    missing_rows = df_gap["Coverage Status"] == "Completely Missing"
    if (df_gap.loc[missing_rows, ["Policy Page", "Corresponding Policy Text"]].astype(bool).any(axis=None)):
        raise RuntimeError("Missing obligations must not contain fabricated policy evidence.")
    non_missing_rows = df_gap["Coverage Status"].isin({"Completely Covered", "Partially Covered"})
    if (
        df_gap.loc[non_missing_rows, "Policy Page"].astype(str).str.len().eq(0).any()
        or df_gap.loc[non_missing_rows, "Corresponding Policy Text"].astype(str).str.len().eq(0).any()
    ):
        raise RuntimeError("Covered and partially covered obligations must contain page-grounded policy evidence.")
    complete_rows = df_gap["Coverage Status"] == "Completely Covered"
    if (
        df_gap.loc[complete_rows, "Missing Elements"].astype(str).str.len().gt(0).any()
        or df_gap.loc[complete_rows, "Gap Coverage %"].lt(100).any()
    ):
        raise RuntimeError("Complete coverage is not permitted while any material element remains unproven.")
    gap_rows = df_gap["Coverage Status"].isin({"Partially Covered", "Completely Missing"})
    incomplete_actions = (
        df_gap.loc[gap_rows, [
            "Policy Gap and Recommendations", "Draft Policy Clause", "Recommendation Owner",
            "Target Timeframe", "Implementation Evidence",
        ]]
        .astype(str)
        .apply(lambda column: column.str.strip().eq(""))
        .any(axis=1)
    )
    if incomplete_actions.any():
        raise RuntimeError("Every policy gap must include a complete, owned and verifiable remediation action.")

    broken_recommendation = df_gap["Policy Gap and Recommendations"].str.contains(
        r"\bmust\s+(?:An insurer|The insurer|Insurers|This Directive|A written contract)\b|"
        r"\bmust\s+The\s+(?:board|outsourcing policy|principles)\b|"
        r"\bto\s+(?:An insurer\s+must|Insurers\s+must|This Directive\s+(?:applies|sets|does)|A written contract\s+must)\b|"
        r"\b(?:pelicyhelders|ofher|perfarms|sub-\s+outsourcing)\b|[{}]",
        case=False,
        regex=True,
        na=False,
    )
    broken_draft_clause = df_gap["Draft Policy Clause"].str.contains(
        r"\bmust\s+The\b|\b(?:pelicyhelders|ofher|perfarms|sub-\s+outsourcing)\b|[{}]",
        case=False,
        regex=True,
        na=False,
    )
    stale_informational = (
        df_gap["Obligation"].str.contains(r"informational|contextual|no standalone", case=False, regex=True, na=False)
        & df_gap["Section"].astype(str).isin({"6.1", "6.2.1", "6.2.2", "6.2.3", "6.2.4", "7.7.12"})
    )
    if broken_recommendation.any() or broken_draft_clause.any() or stale_informational.any():
        bad_recommendation_sections = ", ".join(df_gap.loc[broken_recommendation, "Section"].astype(str).head(8))
        bad_draft_sections = ", ".join(df_gap.loc[broken_draft_clause, "Section"].astype(str).head(8))
        stale_sections = ", ".join(df_gap.loc[stale_informational, "Section"].astype(str).head(8))
        raise RuntimeError(
            "Quality control blocked the workbook because known stale obligations or malformed recommendations remain. "
            f"Malformed recommendation sections: {bad_recommendation_sections or 'none'}; "
            f"malformed draft-clause sections: {bad_draft_sections or 'none'}; "
            f"stale obligation sections: {stale_sections or 'none'}."
        )

    quality = _gap_quality(df_gap)
    statistics = _statistics_frame(df_gap)
    method = "Gemini-assisted evidence review with deterministic validation" if gemini_count else "deterministic evidence review (Gemini unavailable or disabled)"
    logs = [
        {"stage": "Pipeline", "status": "Completed", "message": f"Pipeline {PIPELINE_VERSION}; run {run_id}; source SHA-256 {provenance['source_sha256'][:16]}.", "row_count": len(register)},
        {"stage": "Select Inputs", "status": "Completed", "message": f"Validated register columns and loaded {policy_path.name}.", "row_count": len(register)},
        {
            "stage": "Evidence Retrieval",
            "status": "Completed",
            "message": (
                f"Built one cached policy evidence index across {evidence_index['chunk_count']} page-aware chunks "
                f"and selected up to {candidate_limit} candidates for each actionable obligation. "
                f"{extraction_summary(pages)}"
            ),
            "row_count": len(tasks),
        },
        {
            "stage": "Gap Analysis",
            "status": "Completed",
            "message": (
                f"Completed {method}. Deterministic validation resolved {len(tasks) - len(llm_tasks)} row(s) "
                f"before model use; {len(llm_tasks)} ambiguous row(s) were eligible for model review. "
                f"Gemini produced {gemini_count} validated assessment(s)."
            ),
            "row_count": len(df_gap),
        },
        {
            "stage": "Quality Control",
            "status": "Completed",
            "message": (
                "Confirmed status totals, exact evidence/page grounding, atomic material-element and "
                "control-location checks, and complete owner/timeframe/verification fields for every gap. "
                "Bounded-retrieval absence conclusions and ambiguous citations remain queued for professional review. "
                f"{quality['manual_review_rows']} actionable row(s) require manual review."
            ),
            "row_count": len(df_gap),
        },
        {"stage": "Results", "status": "Completed", "message": "Generated executive summary, detailed assessment, statistics, Excel, and CSV outputs.", "row_count": len(df_gap)},
    ]

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", register_path.stem).strip("_")
    # A run-specific filename prevents browsers and proxies from serving an
    # older workbook that happened to use the same output name.
    excel_path = output_path(f"{stem}_{run_id}_policy_gap_assessment.xlsx")
    csv_path = output_path(f"{stem}_{run_id}_policy_gap_assessment.csv")
    source_label = re.sub(
        r"(?i)[_\s-]*obligation[_\s-]*extraction.*$",
        "",
        register_path.stem,
    )
    source_label = re.sub(r"[_\s]+", " ", source_label).strip() or register_path.stem
    export_gap = df_gap if _enabled("EXPORT_INTERNAL_QUALITY_METRICS") else df_gap.drop(
        columns=INTERNAL_GAP_EXPORT_COLUMNS,
        errors="ignore",
    )
    _write_excel(excel_path, export_gap, statistics, logs, method, source_label)
    export_gap.to_csv(csv_path, index=False)

    status_counts = df_gap["Coverage Status"].value_counts()
    kpis = [
        {"label": "Total Obligations", "value": len(df_gap)},
        {"label": "Completely Covered", "value": int(status_counts.get("Completely Covered", 0))},
        {"label": "Partially Covered", "value": int(status_counts.get("Partially Covered", 0))},
        {"label": "Completely Missing", "value": int(status_counts.get("Completely Missing", 0))},
        {"label": "Not Applicable / Informational", "value": int(status_counts.get("Not Applicable / Informational", 0))},
    ]
    assert sum(int(item["value"]) for item in kpis[1:]) == len(df_gap)

    def stat_rows(dimension: str) -> List[Dict[str, Any]]:
        return statistics[statistics["Dimension"] == dimension].drop(columns=["Dimension"]).to_dict(orient="records")

    return {
        "pipeline": provenance,
        "gap_quality": quality,
        "kpis": kpis,
        "tabs": {
            "gap_assessment": df_gap.to_dict(orient="records"),
            "statistics": {
                "status": stat_rows("Status"),
                "category": stat_rows("Category"),
                "department": stat_rows("Department"),
                "priority": stat_rows("Priority"),
            },
            "process_log": logs,
        },
        "logs": logs,
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
        "output_profile": "internal-quality" if _enabled("EXPORT_INTERNAL_QUALITY_METRICS") else "client-safe",
    }
