"""对已实际入库的四份官方披露文件执行固定的 10 题证据检索验收。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from investment_assistant.config import DATA_DIR
from investment_assistant.official_document_answers import OfficialDocumentAnswerService
from investment_assistant.official_document_ingestion import OfficialDocumentStore, _sha256

logging.getLogger("pypdf").setLevel(logging.ERROR)
OUTPUT = DATA_DIR / "evaluations" / "official_document_mvp_10qa.json"


@dataclass(frozen=True)
class Case:
    ticker: str
    question: str
    document_id: str
    expected_sha256: str
    page: int
    expected_excerpt: str
    expected_pdf_marker: str


CASES = (
    Case("300750.SZ", "宁德时代2026年关联方担保金额上限是多少美元？", "94bdd08f036815cc67005703ab09ed43",
         "f78a1bc68440f594918e05e5d4d20ffddddb6702521d7d9dab22e9368fbff108", 5, "1.3 亿美元", "1.3"),
    Case("300750.SZ", "宁德时代2026年关联方担保的担保方是谁？", "94bdd08f036815cc67005703ab09ed43",
         "f78a1bc68440f594918e05e5d4d20ffddddb6702521d7d9dab22e9368fbff108", 1, "厦门瑞庭", "厦门瑞庭"),
    Case("300750.SZ", "宁德时代2026年关联交易审议时关联董事如何表决？", "94bdd08f036815cc67005703ab09ed43",
         "f78a1bc68440f594918e05e5d4d20ffddddb6702521d7d9dab22e9368fbff108", 2, "回避表决", "8 票"),
    Case("0700.HK", "腾讯2026年公告提交日期是什么？", "3436bc50c4217a0f552fecf2336f9521",
         "b0c12c04b0a9daa99459cb97255bfe70696a769e47b1ac8f9b7f8b0bae12cf4a", 1, "05 October 2026", "05 October 2026"),
    Case("0700.HK", "腾讯2026年回购股份交易日期和股数是多少？", "3436bc50c4217a0f552fecf2336f9521",
         "b0c12c04b0a9daa99459cb97255bfe70696a769e47b1ac8f9b7f8b0bae12cf4a", 5, "238,000", "238,000"),
    Case("0700.HK", "腾讯2025年年报总收入是多少？", "8f83ad593dc5ba5ba3ac95eaa68b61e8",
         "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98", 8, "751,766", "751,766"),
    Case("0700.HK", "腾讯2025年年报研发费用是多少？", "8f83ad593dc5ba5ba3ac95eaa68b61e8",
         "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98", 199, "85,747", "85,747"),
    Case("MSFT", "MSFT 2026年总收入是多少？", "d52abfea1fc7be71a599ac7bb3898d4e",
         "803350171b5cbf667f0915a7de46b17d2e2c6351d398bf36e22016671997d109", 62, "331,839", "331,839"),
    Case("MSFT", "MSFT 2026年现金及现金等价物多少？", "d52abfea1fc7be71a599ac7bb3898d4e",
         "803350171b5cbf667f0915a7de46b17d2e2c6351d398bf36e22016671997d109", 64, "20,935", "20,935"),
    Case("MSFT", "MSFT 2026年净利润是多少？", "d52abfea1fc7be71a599ac7bb3898d4e",
         "803350171b5cbf667f0915a7de46b17d2e2c6351d398bf36e22016671997d109", 70, "133,749", "133,749"),
)


def evaluate(store: OfficialDocumentStore | None = None) -> dict[str, Any]:
    store = store or OfficialDocumentStore()
    documents = {item["document_id"]: item for item in store.list_documents()}
    service = OfficialDocumentAnswerService(store)
    results = []
    for index, case in enumerate(CASES, 1):
        row: dict[str, Any] = {"number": index, "ticker": case.ticker, "question": case.question,
                               "expected": {"document_id": case.document_id, "page": case.page,
                                            "excerpt_marker": case.expected_excerpt, "pdf_marker": case.expected_pdf_marker,
                                            "source_sha256": case.expected_sha256}}
        try:
            doc = documents[case.document_id]
            raw = store.root / case.document_id / "raw.pdf"
            raw_text = PdfReader(str(raw)).pages[case.page - 1].extract_text() or ""
            answer = service.answer(case.ticker, case.question, requested_by="mvp-acceptance")
            source = (answer.get("sources") or [])[0]
            identity = source["identity"]
            checks = {
                "original_pdf_sha256": _sha256(raw) == case.expected_sha256 == doc["source_sha256"],
                "original_pdf_page": case.expected_pdf_marker in raw_text,
                "retrieval_status": answer.get("status") == "evidence_retrieved" and answer.get("evidence_level") == "indexed_official_unverified",
                "top1_document_page": identity["document_id"] == case.document_id and identity["page"] == case.page,
                "top1_source_identity": identity["source_sha256"] == case.expected_sha256 and
                                        identity["source_url"] == doc["source_url"] and identity["ticker"] == case.ticker,
                "top1_expected_excerpt": case.expected_excerpt in source["excerpt"] and case.expected_excerpt in answer["answer"],
            }
            row.update({"passed": all(checks.values()), "checks": checks, "actual_answer": answer["answer"],
                        "top1_source": source, "parser_version": doc["parser_version"],
                        "page_authority": doc["page_authority"]})
        except (OSError, KeyError, IndexError, ValueError, RuntimeError) as exc:
            row.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
        results.append(row)
    return {"evaluated_at": datetime.now(UTC).isoformat(), "scope": "official_document_retrieval_not_verified_financial_answers",
            "case_count": len(results), "passed": sum(row["passed"] for row in results), "results": results}


def main() -> int:
    outcome = evaluate()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": outcome["passed"], "total": outcome["case_count"], "artifact": str(OUTPUT)}, ensure_ascii=False))
    return 0 if outcome["passed"] == outcome["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
