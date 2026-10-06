"""Research Answer v1：在现有核验服务之上编排，不扩大事实证据范围。"""
from __future__ import annotations

import re
from typing import Any

from .bounded_general_qa import IndustryObservationService
from .company_qa import CompanyAnswerService, catalog, references, YEAR, QUARTER, LIVE

_CONCEPTS = {
    "市盈率": "市盈率是股票价格与每股收益之比；收益为负或口径不同的公司不宜直接按该比率比较。",
    "同比": "同比是本期与上年同期的比较；计算前应确认期间、币种和会计口径一致。",
    "毛利率": "毛利率通常为（营业收入－营业成本）除以营业收入；不同披露口径不能直接混算。",
    "现金流": "现金流反映现金流入和流出；经营、投资、筹资现金流应分别查看，不能等同利润。",
}
_COMPARE = re.compile(r"对比|比较|相比|孰高|哪家|两家|差异")
_SUMMARY = re.compile(r"财报摘要|年报摘要|年度报告摘要|总结.{0,8}(?:财报|年报)")
_OVERVIEW = re.compile(r"业绩概览|业绩表现|经营概览|公司概览|业绩怎么样")
_INDUSTRY = re.compile(r"行业观察|行业.{0,10}(?:前景|趋势|关注|研究)|(?:关注|研究|观察).{0,10}(?:行业|赛道|板块)")
_CONCEPT = re.compile(r"^(?:什么是|解释(?:一下)?|如何理解|介绍(?:一下)?)(?:财务)?(?P<term>市盈率|同比|毛利率|现金流)[？?。]?$|^(?P<term2>市盈率|同比|毛利率|现金流)(?:是什么|是什么意思|什么意思)[？?。]?$|^(?:市盈率|同比|毛利率|现金流)[？?。]?$")


def classify_research(question: str, targets: set[str]) -> str | None:
    """只识别明确意图；普通指标问答留在既有路径。"""
    if _COMPARE.search(question):
        return "company_comparison"
    if _SUMMARY.search(question):
        return "financial_summary"
    if _OVERVIEW.search(question):
        return "company_overview"
    if _INDUSTRY.search(question) and not targets:
        return "industry_observation"
    if _CONCEPT.fullmatch(question.strip()):
        return "concept"
    return None


class ResearchAnswerService:
    def __init__(self, company: CompanyAnswerService | None = None,
                 industry: IndustryObservationService | None = None):
        self.company = company or CompanyAnswerService()
        self.industry = industry or IndustryObservationService()

    def answer(self, question: str, *, requested_by: str, ticker: str | None = None) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError("research_question_invalid")
        question = question.strip()
        records = catalog()
        targets, unknown = references(question, records)
        if ticker:
            ticker = ticker.strip().upper()
            if ticker not in records or (targets and targets != {ticker}):
                return self._packet("unresolved", question, [], None, reason="标的与问题不一致或尚未接入官方资料。")
            targets.add(ticker)
        intent = classify_research(question, targets)
        if unknown or intent is None:
            return self._packet(intent or "unresolved", question, sorted(targets), None,
                                reason="无法核验意图或公司范围；请写明已收录的标的和研究类型。")
        if intent == "concept":
            if targets:
                return self._packet(intent, question, sorted(targets), None, reason="请将概念问题与公司事实分开提问。")
            match = _CONCEPT.fullmatch(question)
            term = match.group("term") or match.group("term2") if match else None
            if not term:
                term = re.sub(r"[？?。]$", "", question)
            explanation = _CONCEPTS[term]
            return self._packet(intent, question, [], None, conclusion=explanation,
                                limitations=["通用概念解释，不是公司事实、实时数据或投资建议。"], tier="educational")
        if intent == "industry_observation":
            if targets:
                return self._packet(intent, question, sorted(targets), None, reason="行业和公司问题须分开。")
            result = self.industry.answer(question, requested_by=requested_by)
            return self._from_result(intent, question, [], None, result, tier="official_statistics",
                                     period=result.get("data_period"))
        if intent == "company_comparison":
            if len(targets) != 2:
                return self._packet(intent, question, sorted(targets), None, reason="比较需明确两家已收录公司；候选标的不能用于事实比较。")
        elif len(targets) != 1:
            return self._packet(intent, question, sorted(targets), None, reason="请明确一家已收录公司。")
        years = set(YEAR.findall(question))
        if len(years) != 1 or QUARTER.search(question) or LIVE.search(question):
            return self._packet(intent, question, sorted(targets), None,
                                reason="请明确单一年度；季度、实时数据和投资建议不使用全年年报替代。")
        year = next(iter(years))
        tickers = sorted(targets)
        if any(records[t]["year"] != year for t in tickers):
            return self._packet(intent, question, tickers, year, reason="公司年报年度不一致，不能跨期拼接。")
        # 仅请求已有全年收入锚点；不从摘要文字猜测利润、亮点或风险事实。
        results = [self.company.answer(t, f"{t} {year}年全年营业收入是多少？", requested_by=requested_by) for t in tickers]
        if any(r.get("status") != "answered" or not r.get("sources") for r in results):
            return self._packet(intent, question, tickers, year, reason="至少一个标的的全年收入尚无可核验锚点；不输出不完整的对比或概览。")
        metrics, sources = [], []
        for t, result in zip(tickers, results):
            citation = f"R{len(sources) + 1}"
            source = result["sources"][0]
            if (source.get("identity") or {}).get("ticker") != t:
                return self._packet(intent, question, tickers, year, reason="来源与标的不一致。")
            sources.append({**source, "citation": citation, "tier": "official_verified"})
            metrics.append({"ticker": t, "field": result.get("field") or "全年收入", "statement": result["answer"], "citation": citation})
        conclusion = "；".join(f"{m['ticker']}：{m['statement']}" for m in metrics)
        if intent == "company_comparison":
            conclusion = "同年度全年收入分别为：" + conclusion + "；币种和单位未做跨公司换算，不能据此推断投资价值。"
        limitations = ["仅覆盖已核验的全年收入，不代表完整财报分析；其他指标与经营风险尚无足够证据。", "不是实时行情或投资建议。"]
        return self._packet(intent, question, tickers, year, conclusion=conclusion,
                            metrics=metrics, sources=sources, limitations=limitations, tier="official_verified")

    def _from_result(self, intent: str, question: str, tickers: list[str], year: str | None,
                     result: dict[str, Any], *, tier: str, period: str | None = None) -> dict[str, Any]:
        if result.get("status") != "answered" or not result.get("sources"):
            return self._packet(intent, question, tickers, year, reason=str(result.get("answer") or "证据不足。"))
        sources = [{**s, "tier": tier} for s in result["sources"]]
        return self._packet(intent, question, tickers, year, conclusion=result["answer"], sources=sources,
                            limitations=result.get("limitations") or [], tier=tier, period=period)

    @staticmethod
    def _packet(intent: str, question: str, tickers: list[str], year: str | None, *,
                conclusion: str | None = None, reason: str | None = None,
                metrics: list[dict[str, Any]] | None = None, sources: list[dict[str, Any]] | None = None,
                limitations: list[str] | None = None, tier: str = "none", period: str | None = None) -> dict[str, Any]:
        sections = {"conclusion": conclusion or reason, "core_metrics": metrics or [],
                    "highlights": [], "risks": [], "scope": {"tickers": tickers, "year": year, "data_period": period},
                    "sources": sources or []}
        if conclusion and not sections["highlights"]:
            sections["risks"] = ["亮点及公司风险未建立单独证据锚点，不作推断。"] if tickers else []
        return {"status": "answered" if conclusion else "refused", "intent": intent,
                "task_context": {"intent": intent, "tickers": tickers, "year": year, "question": question},
                "answer": conclusion or reason, "sections": sections, "evidence_tier": tier,
                "sources": sources or [], "limitations": limitations or ([reason] if reason else []),
                "error_code": None if conclusion else "RESEARCH_EVIDENCE_INSUFFICIENT"}
