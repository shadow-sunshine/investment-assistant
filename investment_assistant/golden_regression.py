"""Offline regression checks for the approved golden report baseline."""

from __future__ import annotations

import json
import re
from typing import Any

from .config import DATA_DIR
from .llm_generation import validate_narrative_source_attribution, validate_rag_claim_attribution
from .safety import validate_report

GOLDEN_DIR = DATA_DIR / "golden"
MANIFEST_PATH = GOLDEN_DIR / "manifest.json"
FINANCIAL_SECTION_PREFIX = "## 2."
NARRATIVE_SECTION_PREFIX = "## 4."
MISSING_LABEL = "\u5feb\u7167\u5b57\u6bb5\u7f3a\u5931\uff1a"
SNAPSHOT_LABEL = "\u3010\u6765\u6e90\uff1a\u7ed3\u6784\u5316\u5feb\u7167\u3011"


def _section_by_prefix(report: str, heading_prefix: str) -> str:
    pattern = rf"^{re.escape(heading_prefix)}[^\n]*$([\s\S]*?)(?=^##\s|\Z)"
    match = re.search(pattern, report, flags=re.MULTILINE)
    if not match:
        raise AssertionError(f"Golden report is missing section prefix: {heading_prefix}")
    return match.group(1).strip()


def check_golden_sample() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sample_id = manifest["id"]
    report_path = GOLDEN_DIR / f"{sample_id}.md"
    audit_path = GOLDEN_DIR / f"{sample_id}.json"
    report = report_path.read_text(encoding="utf-8")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    expectations = manifest["expectations"]

    safety = validate_report(report, audit.get("sources", []), audit.get("financial_snapshot"))
    assert safety["passed"], safety["findings"]
    assert audit.get("evaluation", {}).get("passed") is expectations["safety_passed"]
    assert audit.get("llm_result", {}).get("used") is expectations["controlled_llm_used"]

    financial_section = _section_by_prefix(report, FINANCIAL_SECTION_PREFIX)
    narrative = _section_by_prefix(report, NARRATIVE_SECTION_PREFIX)
    assert MISSING_LABEL in financial_section
    assert SNAPSHOT_LABEL in narrative
    for field in expectations["required_financial_fields"]:
        assert audit.get("financial_snapshot", {}).get(field) is not None, f"Golden snapshot missing {field}"

    snapshot_attribution = validate_narrative_source_attribution(narrative)
    rag_attribution = validate_rag_claim_attribution(narrative)
    assert snapshot_attribution["passed"], snapshot_attribution["findings"]
    assert rag_attribution["passed"], rag_attribution["findings"]
    return {
        "passed": True,
        "sample_id": sample_id,
        "safety": safety,
        "snapshot_attribution": snapshot_attribution,
        "rag_attribution": rag_attribution,
    }


def main() -> int:
    print(json.dumps(check_golden_sample(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
