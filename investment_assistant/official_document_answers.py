"""官方文档检索式回答：只摘录有 SHA/页码身份的原文，不验证字段数值。"""
from __future__ import annotations

import re
from typing import Any

from .company_qa import catalog, references, unverified_company_subject
from .official_document_ingestion import OfficialDocumentStore, search_official_documents
from .question_intents import classify_question

_YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")
_LATEST = re.compile(r"最新|当前|现在|截至目前|今年")


def _document_date_key(value: Any) -> tuple[int, int, int]:
    text = str(value or "")
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        return tuple(map(int, match.groups()))
    match = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](20\d{2})", text)
    if match:
        day, month, year = map(int, match.groups())
        return year, month, day
    match = re.search(r"20\d{2}", text)
    return (int(match.group(0)), 0, 0) if match else (0, 0, 0)


class OfficialDocumentAnswerService:
    def __init__(self, store: OfficialDocumentStore | None = None) -> None:
        self.store = store or OfficialDocumentStore()

    def inventory(self, ticker: str, question: str = "", *, requested_by: str = "") -> dict[str, Any]:
        """返回经当前 SHA 复核的文档元数据，不把目录查询误送到字段检索。"""
        code = str(ticker).strip().upper()
        records = catalog()
        targets, unknown = references(question, records)
        if unknown or (targets and targets != {code}) or unverified_company_subject(question, records, include_non_fact=True):
            return {"status": "refused", "error_code": "CROSS_TICKER_QUERY", "answer": "请一次查询一家公司的官方资料目录。", "sources": []}
        documents = sorted(self.store.list_documents(ticker=code),
            key=lambda d: (_document_date_key(d.get("filing_date")), _document_date_key(d.get("report_date"))), reverse=True)
        years = set(_YEAR.findall(question))
        if len(years) > 1:
            return {"status": "needs_clarification", "answer": "请先选择一个报告期间，我再列出对应官方资料。", "sources": []}
        if years:
            documents = [d for d in documents if str(d.get("report_date") or "").startswith(next(iter(years)))]
        if "公告" in question:
            documents = [d for d in documents if str(d.get("filing_form")) not in {"annual_report", "10-K", "20-F"}]
        elif "年报" in question or "年度报告" in question:
            documents = [d for d in documents if str(d.get("filing_form")) in {"annual_report", "10-K", "20-F"}]
        listed = [{k: d.get(k) for k in ("document_id", "ticker", "title", "source", "source_url", "report_date", "filing_date", "filing_form", "indexed_at", "source_sha256", "page_authority")} for d in documents[:8]]
        if not listed:
            return {"status": "needs_clarification", "ticker": code, "documents": [], "sources": [],
                    "answer": f"当前本地还没有 {code} 符合条件的官方文档目录。请指定公司和资料类型；这不代表官网没有发布。", "error_code": "OFFICIAL_DOCUMENT_NOT_ONBOARDED"}
        lines = [f"我找到 {code} 已入库的官方资料（按披露日期排列）："]
        refs = []
        for i, doc in enumerate(listed, 1):
            lines.append(f"{i}. {doc['title'] or doc['filing_form']} · 资料期间 {doc['report_date'] or '未标注'} · 披露 {doc['filing_date'] or '未标注'} · {doc['source']} [D{i}]")
            refs.append({"citation": f"D{i}", "label": doc["source"], "identity": {**doc, "published_at": doc["filing_date"], "retrieved_at": doc["indexed_at"]}})
        lines.append("这是当前本地已入库的一手资料目录，未执行本轮官网刷新；不能保证是官网此刻最新的全部披露，也不代表其中每个财务字段已核验。")
        return {"status": "documents_listed", "ticker": code, "answer": "\n\n".join(lines), "documents": listed,
                "sources": refs, "intent_label": "官方资料目录", "freshness": {"mode": "local_snapshot", "checked_online": False,
                "latest_filing_date": listed[0]["filing_date"], "indexed_at": listed[0]["indexed_at"]}, "requested_by": requested_by}

    def answer(self, ticker: str, question: str, *, requested_by: str = "") -> dict[str, Any]:
        code = str(ticker or "").upper().strip()
        normalized = str(question or "").strip()
        if not normalized or len(normalized) > 500 or not re.fullmatch(r"[A-Z0-9.]{2,16}", code):
            raise ValueError("official_document_input_invalid")
        if classify_question(normalized, has_explicit_company=True).kind == "document_inventory":
            return self.inventory(code, normalized, requested_by=requested_by)
        # 与已登记别名和显式代码比对；不允许用请求参数偷换问句中的公司。
        targets, unknown_codes = references(normalized, catalog())
        if unknown_codes or (targets and targets != {code}):
            return {"status": "refused", "error_code": "CROSS_TICKER_QUERY",
                    "ticker": code, "question": normalized,
                    "answer": "问题涉及其他公司或未知代码；请一次只查询一家公司的官方资料。", "sources": []}
        years = set(_YEAR.findall(normalized))
        documents = sorted(self.store.list_documents(ticker=code),
                           key=lambda item: (_document_date_key(item.get("filing_date")),
                                             _document_date_key(item.get("report_date"))), reverse=True)
        indexed_periods = sorted({str(item.get("report_date") or "")[:4] for item in documents
                                  if re.match(r"20\d{2}", str(item.get("report_date") or ""))}, reverse=True)
        if len(years) > 1:
            return {"status": "refused", "error_code": "OFFICIAL_DOCUMENT_YEAR_REQUIRED",
                    "ticker": code, "question": normalized,
                    "answer": "一次只能查询一个报告或公告期间，避免混用不同期间的资料。", "sources": [],
                    "coverage": {"indexed_periods": indexed_periods}}
        if years:
            year = next(iter(years))
        elif _LATEST.search(normalized) and indexed_periods:
            year = indexed_periods[0]
        else:
            return {"status": "refused", "error_code": "OFFICIAL_DOCUMENT_YEAR_REQUIRED",
                    "ticker": code, "question": normalized,
                    "answer": "请明确报告或公告年度；如果要查最新一手资料，请说“最新官方资料”。", "sources": [],
                    "coverage": {"indexed_periods": indexed_periods}}
        matches = search_official_documents(normalized, ticker=code, year=year, limit=5, store=self.store)
        # 空结果不能把其他来源或其他年度资料拼接进来。
        if not matches:
            return {"status": "refused", "error_code": "OFFICIAL_DOCUMENT_NOT_FOUND",
                    "ticker": code, "question": normalized,
                    "answer": f"已检查 {code} {year} 年的一手官方资料索引，但没有找到与该问题匹配的原文字段；不会用其他年度替代。",
                    "sources": [], "retrieval": {"top_k": 5, "match_count": 0},
                    "coverage": {"indexed_periods": indexed_periods,
                                 "latest_indexed_document": documents[0] if documents else None}}
        refs = []
        for hit in matches:
            refs.append({"citation": hit["citation"], "label": hit["source"],
                         "excerpt": hit["content"],
                         "identity": {key: hit.get(key) for key in (
                             "document_id", "ticker", "source", "source_url", "source_sha256",
                             "title", "report_date", "filing_date", "filing_form", "page_authority", "page",
                             "level", "ordinal", "record_id", "adjacent_record_ids")}})
        primary = matches[0]
        excerpt = " ".join(str(primary["content"]).split())[:650]
        page_note = f"本地转换页 {primary['page']}" if primary["page_authority"] == "generated" else f"PDF 页 {primary['page']}"
        return {"status": "evidence_retrieved", "error_code": None, "ticker": code,
                "question": normalized, "evidence_level": "indexed_official_unverified",
                "source_label": primary["source"], "evidence_label": "官方原文索引·字段待核验",
                "answer": f"在 {primary['source']} 官方资料找到相关原文（{page_note}，[O1]）：{excerpt}\n\n这只是可追溯的检索摘录；具体数值、单位及表格列仍需核验，不作为已验证财务结论。",
                "sources": refs,
                "retrieval": {"top_k": 5, "match_count": len(refs), "index_type": "page_paragraph_table"},
                "limitations": ["索引结果未自动升级为已核验财务字段。", "扫描/OCR 与复杂表格可能有识别错误；请核对官方原件。"],
                "requested_by": requested_by}
