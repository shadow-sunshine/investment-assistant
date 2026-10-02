"""隔离财报检索实验：字段与原始块内数值证据优先，hash 不视为语义信号。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any

from .rag import LocalResearchRAG

# 仅含通用财务术语；中英共用概念，最长标签优先，不能加入公司、题目或答案。
FINANCIAL_GLOSSARY = (
    ("total_revenue", ("营业总收入", "总营业收入", "total revenue", "total revenues", "total net sales")),
    ("revenue", ("营业收入", "销售收入", "revenue", "revenues", "net sales", "sales revenue")),
    ("total_cost", ("总销售成本", "总营业成本", "total cost of sales", "total cost of revenue")),
    ("cost", ("营业成本", "销售成本", "cost of sales", "cost of revenue", "cost of goods sold")),
    ("gross_profit", ("毛利", "gross profit", "gross margin")),
    ("gross_margin_ratio", ("毛利率", "gross profit margin", "gross margin percentage", "gross margin rate")),
    ("operating_income", ("营业利润", "经营利润", "经营盈利", "operating income", "operating profit")),
    ("pretax_income", ("利润总额", "税前利润", "除税前盈利", "income before income taxes", "income before taxes", "profit before income tax", "profit before tax")),
    ("income_tax", ("所得税费用", "所得税开支", "income tax expense", "income tax expenses", "provision for income taxes", "income tax provision")),
    ("parent_profit", ("归属于母公司股东的净利润", "归属于上市公司股东的净利润", "归母净利润", "net income attributable to shareholders", "profit attributable to owners of the parent")),
    ("net_income", ("净利润", "净盈利", "年度盈利", "net income", "net profit", "profit for the year", "profit for the period")),
    ("basic_eps", ("基本每股收益", "基本每股盈利", "basic earnings per share", "basic eps")),
    ("diluted_eps", ("稀释每股收益", "摊薄每股收益", "diluted earnings per share", "diluted eps")),
    ("eps", ("每股收益", "每股盈利", "earnings per share", "eps")),
    ("weighted_roe", ("加权平均净资产收益率", "weighted average return on equity")),
    ("roe", ("净资产收益率", "return on equity", "roe")),
    ("total_opex", ("总运营费用", "营业费用合计", "total operating expenses")),
    ("selling", ("销售费用", "销售开支", "销售及市场推广开支", "销售与市场营销费用", "销售和营销费用", "selling expenses", "selling and marketing expenses", "sales and marketing", "selling and distribution expenses")),
    ("admin", ("管理费用", "一般及行政费用", "行政开支", "general and administrative", "administrative expenses")),
    ("rnd_expense", ("研发费用", "研究开发费用", "研究及开发费用", "research and development", "research and development expenses", "r&d expenses")),
    ("rnd_investment", ("研发投入合计", "研发投入", "研发投资", "total research and development investment", "research and development investment")),
    ("finance_expense", ("财务费用", "融资成本", "finance expense", "finance expenses", "finance costs", "financial expenses")),
    ("total_assets", ("总资产", "资产总额", "资产总计", "total assets")),
    ("total_liabilities", ("负债总额", "总负债", "负债合计", "total liabilities")),
    ("parent_equity", ("归属于上市公司股东的净资产", "归属于母公司股东权益合计", "归属于母公司所有者权益合计", "equity attributable to shareholders", "equity attributable to owners of the parent")),
    ("total_equity", ("股东权益合计", "所有者权益合计", "净资产", "total stockholders equity", "total shareholders equity", "total equity")),
    ("cash", ("货币资金", "现金及现金等价物", "cash on hand", "cash and cash equivalents", "cash equivalents")),
    ("receivables", ("应收账款", "accounts receivable", "trade receivables")),
    ("other_receivables", ("其他应收款", "other receivables")),
    ("prepayments", ("预付款项", "预付款", "prepayments", "prepaid expenses")),
    ("inventory", ("存货", "inventories", "inventory")),
    ("trading_assets", ("交易性金融资产", "trading financial assets", "financial assets held for trading")),
    ("equity_investments", ("长期股权投资", "long term equity investments", "long term equity investment")),
    ("fixed_assets", ("固定资产", "fixed assets", "property plant and equipment")),
    ("rou_assets", ("使用权资产", "right of use assets", "right of use asset")),
    ("intangible_assets", ("无形资产", "intangible assets")),
    ("goodwill", ("商誉", "goodwill")),
    ("contract_liabilities", ("合同负债", "contract liability", "contract liabilities", "deferred revenue")),
    ("payables", ("应付账款", "accounts payable", "trade payables")),
    ("capital_reserve", ("资本公积", "capital reserve", "capital reserves", "additional paid in capital")),
    ("surplus_reserve", ("盈余公积", "surplus reserve", "surplus reserves", "statutory reserves")),
    ("retained_earnings", ("未分配利润", "留存收益", "retained earnings", "undistributed profit")),
    ("operating_cash", ("经营活动产生的现金流量净额", "经营活动产生的现金净额", "经营活动现金流量净额", "net cash from operations", "net cash provided by operating activities", "net cash generated from operating activities", "net cash flow from operating activities")),
    ("investing_cash", ("投资活动产生的现金流量净额", "投资活动现金流量净额", "net cash flow from investing activities", "net cash used in investing activities", "net cash provided by investing activities")),
    ("financing_cash", ("筹资活动产生的现金流量净额", "net cash flow from financing activities", "net cash used in financing activities", "net cash provided by financing activities")),
    ("dividend", ("现金红利", "现金股利", "现金分红", "cash dividend", "cash dividends", "dividend per share", "每股股利")),
    ("current_assets", ("流动资产合计", "total current assets")),
    ("current_liabilities", ("流动负债合计", "total current liabilities")),
    ("noncurrent_assets", ("非流动资产合计", "total non current assets", "total noncurrent assets")),
    ("noncurrent_liabilities", ("非流动负债合计", "total non current liabilities", "total noncurrent liabilities")),
    ("other_current_assets", ("其他流动资产", "other current assets")),
    ("current_noncurrent_assets", ("一年内到期的非流动资产", "current portion of non current assets")),
    ("share_capital", ("实收资本", "股本", "share capital", "paid in capital", "common stock")),
    ("treasury_stock", ("库存股", "treasury stock")),
    ("other_comprehensive_income", ("其他综合收益", "other comprehensive income")),
    ("depreciation", ("折旧及摊销", "depreciation and amortization")),
    ("impairment", ("资产减值损失", "impairment losses")),
    ("interest_income", ("利息收入", "interest income")),
    ("interest_expense", ("利息费用", "interest expense")),
)
# 总额不能与子科目等同；父概念只作为弱匹配，不享有完整标签等级。
FIELD_PARENTS = {"total_revenue": "revenue", "total_cost": "cost", "basic_eps": "eps", "diluted_eps": "eps", "weighted_roe": "roe"}
QUALIFIERS = (
    ("overseas", ("国外", "境外", "海外", "overseas", "international", "foreign")),
    ("domestic", ("国内", "境内", "domestic")),
    ("consolidated", ("合并", "consolidated")),
    ("parent", ("母公司", "parent company")),
)
STOPWORDS = frozenset("a an and are as at by for from how in is of on or the to was were what which with year fiscal annual report does did do much hold spend balance ended end december company please total 请 问 是 的 多少 公司 年度 年 财年 截至 年末 余额".split())
CANDIDATES_PER_BRANCH = 48
NUMBER_WINDOW = 120
TABLE_WINDOW = 300
RULE_VERSION = "financial-block-evidence-2"
INCOME_FIELDS = frozenset({"total_revenue", "revenue", "cost", "total_cost", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income", "rnd_expense", "selling", "admin", "total_opex", "parent_profit"})
BALANCE_FIELDS = frozenset({"total_assets", "total_liabilities", "total_equity", "parent_equity", "cash", "receivables", "other_receivables", "prepayments", "inventory", "trading_assets", "equity_investments", "fixed_assets", "rou_assets", "intangible_assets", "goodwill", "contract_liabilities", "payables", "capital_reserve", "surplus_reserve", "retained_earnings", "current_assets", "current_liabilities", "noncurrent_assets", "noncurrent_liabilities", "other_current_assets", "current_noncurrent_assets", "share_capital", "treasury_stock", "other_comprehensive_income"})
FLOW_FIELDS = frozenset({"operating_cash", "investing_cash", "financing_cash"})


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    text = re.sub(r"(?<=[a-z])'s\b", "", text)
    text = re.sub(r"(?<=[a-z])[-‐‑–—](?=[a-z])", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _alias_pattern(alias: str, *, literal_detail: bool = False) -> re.Pattern[str]:
    words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", _normalize(alias))
    separator = r"(?:[\s,&/()\']|\band\b)+" if literal_detail else r"[\s,&/()\']+"
    body = separator.join(re.escape(word) for word in words)
    left = r"(?<![a-z0-9])" if words and words[0][0].isascii() else ""
    right = r"(?![a-z0-9])" if words and words[-1][-1].isascii() else ""
    return re.compile(left + body + right)


FIELD_PATTERNS = tuple((key, _alias_pattern(alias)) for key, aliases in FINANCIAL_GLOSSARY for alias in aliases)
QUALIFIER_PATTERNS = tuple((key, _alias_pattern(alias)) for key, aliases in QUALIFIERS for alias in aliases)
# 只容许原始块内“标签前半 + 数值单元格 + 标签后半”的有限乱序，不能跨块修复。
BROKEN_LABEL_PATTERNS = tuple(
    (key, re.compile(re.escape(alias[:split]) + r"(?:[\s\d,.()%¥$+−-]|不适用){3,100}" + re.escape(alias[split:])))
    for key, aliases in FINANCIAL_GLOSSARY for alias in aliases
    if re.fullmatch(r"[\u4e00-\u9fff]{4,}", alias)
    for split in range(2, len(alias) - 1)
)
NUMBER_PATTERN = re.compile(r"(?<![a-z0-9])\(?[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?%?(?![a-z0-9])")
STATEMENT_PATTERN = re.compile(r"合并利润表|合并资产负债表|合并现金流量表|主要会计数据|主要财务指标|financial highlights|consolidated (?:statements? of|balance sheets?)|income statements?")
UNIT_PATTERN = re.compile(r"\([^)]{0,24}\)|\b(?:note|notes|in|millions?|billions?|thousands?|rmb|usd|hkd|eur|cny)\b|人民币|万元|亿元|元|股|附注|%|[\d\s.,:;$¥€£/\[\]一二三四五六七八九十、]+")
RULES_SHA256 = hashlib.sha256(json.dumps({
    "version": RULE_VERSION, "glossary": FINANCIAL_GLOSSARY, "parents": FIELD_PARENTS,
    "qualifiers": QUALIFIERS, "stopwords": sorted(STOPWORDS),
    "number_window": NUMBER_WINDOW, "table_window": TABLE_WINDOW,
    "broken_patterns": [(key, pattern.pattern) for key, pattern in BROKEN_LABEL_PATTERNS],
    "schema": [sorted(INCOME_FIELDS), sorted(BALANCE_FIELDS), sorted(FLOW_FIELDS)],
    "number_pattern": NUMBER_PATTERN.pattern, "statement_pattern": STATEMENT_PATTERN.pattern,
    "unit_pattern": UNIT_PATTERN.pattern, "candidate_limit": CANDIDATES_PER_BRANCH,
    "order": "evidence_tier,qualifier,detail_row,field_coverage,structured,comparative,family_rows,row_numbers,literal,hash",
    "display": "same_original_block_no_stitching", "mode": "hash+disabled",
    "score": "descending_ordinal_not_probability", "bm25": {"k1": 1.2, "b": .75},
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _terms(text: str) -> Counter[str]:
    """中文 2/3-gram 与英文词；完整标签由独立概念匹配保留。"""
    terms: list[str] = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z]+|\d+(?:[.,]\d+)*", text.lower()):
        if '\u4e00' <= part[0] <= '\u9fff':
            terms.extend(part[i:i + n] for n in (2, 3) for i in range(len(part) - n + 1))
        elif part not in STOPWORDS and (len(part) > 1 or part.isdigit()):
            terms.append(part)
    return Counter(terms)


def _field_spans(text: str) -> list[tuple[int, int, str]]:
    matches = [(m.start(), m.end(), key) for key, pattern in FIELD_PATTERNS for m in pattern.finditer(text)]
    matches.extend((m.start(), m.end(), key) for key, pattern in BROKEN_LABEL_PATTERNS
                   for m in pattern.finditer(text) if _amounts(m.group()))
    # 同一起点/重叠范围只取最长财务标签，避免其他应收款被当成应收账款。
    selected: list[tuple[int, int, str]] = []
    for match in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0], item[2])):
        if not any(match[0] < old[1] and old[0] < match[1] for old in selected):
            selected.append(match)
    return sorted(selected)


def _clean_query(query: str) -> str:
    # 通用所有格、日期、问句外壳剥离，不依赖标的名单；年份不充当答案数字。
    query = unicodedata.normalize("NFKC", query).lower().replace("’", "'")
    query = re.sub(r"\b[a-z]+(?:\s+[a-z]+)?'s\b", " ", query)
    query = re.sub(r"\b(?:19|20)\d{2}\b", " ", query)
    query = re.sub(r"请问|截至|是多少|有多少|多少|年末|年度|财年|余额|年|的|是|[?？]", " ", query)
    return _normalize(query)


def _intent(query: str) -> dict[str, Any]:
    cleaned = _clean_query(query)
    spans = _field_spans(_normalize(query))
    fields = frozenset(key for _, _, key in spans)
    ambiguities = []
    # 英文 bare gross margin 可指金额也可指比率；显式保留歧义，不能伪称两个概念等价。
    if "gross_profit" in fields and re.search(r"\bgross margin\b", _normalize(query)):
        fields |= {"gross_margin_ratio"}
        ambiguities.append("gross_margin_absolute_or_ratio")
    cleaned_spans = _field_spans(cleaned)
    residual = cleaned
    for start, end, _ in reversed(cleaned_spans):
        residual = residual[:start] + " " + residual[end:]
    # 任意产品/业务标签只能来自当前查询原文，绝不加入公司或产品翻译特判。
    detail_words = [word for word in re.findall(r"[a-z]+", residual) if word not in STOPWORDS]
    detail = " ".join(detail_words) if 2 <= len(detail_words) <= 6 else ""
    qualifiers = frozenset(key for key, pattern in QUALIFIER_PATTERNS if pattern.search(_normalize(query)))
    # 查询原词与通用别名同时参加字面召回，不读取冻结映射或任何评测答案。
    aliases = [alias for key, variants in FINANCIAL_GLOSSARY if key in fields for alias in variants]
    terms = _terms(cleaned + " " + " ".join(aliases))
    terms = Counter({term: 1 for term in terms if not any(ch.isdigit() for ch in term)})
    return {"fields": fields, "qualifiers": qualifiers, "terms": terms, "detail": detail, "ambiguities": ambiguities,
            "expanded": cleaned + " " + " ".join(aliases)}


def _amounts(text: str) -> list[re.Match[str]]:
    matches = []
    for match in NUMBER_PATTERN.finditer(text):
        raw = match.group().strip("()+−-%")
        # 排除表头年份与小附注编号；小数、千分位、百分数及货币符号仍可作数值证据。
        plain = raw.isdigit()
        currency = bool(re.search(r"[$¥€£]\s*$", text[max(0, match.start() - 3):match.start()]))
        if plain and not currency and (int(raw) < 100 or 1900 <= int(raw) <= 2100) and not match.group().endswith("%"):
            continue
        matches.append(match)
    return matches


def _number_window(text: str, span: tuple[int, int, str], spans: list[tuple[int, int, str]]) -> str:
    start, end, _ = span
    if _amounts(text[start:end]):
        return text[start:end]  # 同块标签被数值打断，数字仍在该标签的有限范围内。
    next_start = min((other_start for other_start, _, _ in spans if other_start >= end), default=len(text))
    return text[end:min(end + NUMBER_WINDOW, next_start)]


def _local_numbers(text: str, span: tuple[int, int, str], spans: list[tuple[int, int, str]]) -> tuple[int, bool]:
    start, end, _ = span
    if _amounts(text[start:end]):
        return len(_amounts(text[start:end])), True
    tail = _number_window(text, span, spans)
    amounts = _amounts(tail)
    if not amounts:
        return 0, False
    bridge = tail[:amounts[0].start()]
    return len(amounts), not UNIT_PATTERN.sub("", bridge).strip(" ()-、")


def _structure(record: dict[str, Any]) -> dict[str, Any]:
    text, spans = record["normalized"], record["fields"]
    numeric_fields = set()
    comparative = False
    for span in spans:
        count, row = _local_numbers(text, span, spans)
        if not row:
            continue
        numeric_fields.add(span[2])
        # 金额 + 小数比例 + 上期金额 + 比例为常见资产变化表，不依赖标题所在页/相邻块。
        values = _amounts(_number_window(text, span, spans))
        small_decimals = sum("." in m.group() and abs(float(m.group().strip("()+−-%").replace(",", ""))) < 100 for m in values)
        comparative |= count >= 4 and small_decimals >= 2
    counts = [len(numeric_fields & family) for family in (INCOME_FIELDS, BALANCE_FIELDS, FLOW_FIELDS)]
    return {"family_rows": counts, "statement": int(bool(STATEMENT_PATTERN.search(text))),
            "comparative": int(comparative)}


def _evidence(record: dict[str, Any], intent: dict[str, Any], literal: float) -> dict[str, Any]:
    text, spans = record["normalized"], record["fields"]
    wanted = intent["fields"]
    if any(span[2] == "eps" for span in spans):
        subrows = []
        for key, label in (("basic_eps", "basic|基本"), ("diluted_eps", "diluted|稀释|摊薄")):
            for match in re.finditer(r"(?<![a-z])(?:" + label + r")(?![a-z])", text):
                if not any(start <= match.start() < end for start, end, _ in spans):
                    subrows.append((match.start(), match.end(), key))
        spans = sorted(spans + subrows)
    exact = [span for span in spans if span[2] in wanted]
    weak = [span for span in spans if span[2] in {FIELD_PARENTS.get(key) for key in wanted}]
    rows = [_local_numbers(text, span, spans) for span in exact]
    row_numbers = max((count for count, row in rows if row), default=0)
    nearby = max((count for count, _ in rows), default=0)
    number_evidence = [{"field": span[2], "window": _number_window(text, span, spans)}
                       for span, (count, row) in zip(exact, rows) if count and row]
    qualifiers = {key for key, pattern in QUALIFIER_PATTERNS if pattern.search(text)}
    coverage = len({span[2] for span in exact}) / len(wanted) if wanted else 0.0
    qualifier = len(intent["qualifiers"] & qualifiers) / len(intent["qualifiers"]) if intent["qualifiers"] else 0.0
    table_row = False
    detail_row = False
    patterns = [(key, pattern) for key, pattern in QUALIFIER_PATTERNS if key in intent["qualifiers"]]
    if intent["detail"]:
        patterns.append(("query_literal_label", _alias_pattern(intent["detail"], literal_detail=True)))
    for key, pattern in patterns:
        for match in pattern.finditer(text):
            # 表头可以在数据行前后，但必须同块且有限邻域；这里不解析或补全任何列值。
            if not any(min(abs(match.start() - end), abs(start - match.end())) <= TABLE_WINDOW for start, end, _ in exact):
                continue
            span = (match.start(), match.end(), key)
            count, row = _local_numbers(text, span, spans)
            if count >= 2 and row:
                table_row = True
                detail_row |= key == "query_literal_label"
                row_numbers = max(row_numbers, count)
                number_evidence.extend({"field": field, "window": _number_window(text, span, spans)} for field in sorted(wanted))
    tier = 4 if row_numbers else 2 if exact else 1 if weak else 0
    structure = record["structure"]
    family_rows = max((count for family, count in zip((INCOME_FIELDS, BALANCE_FIELDS, FLOW_FIELDS), structure["family_rows"]) if wanted & family), default=0)
    structured = int(bool(structure["statement"] or family_rows >= 4))
    rank = (tier, qualifier, int(detail_row), coverage, structured, structure["comparative"], min(family_rows, 6), min(row_numbers, 3), literal)
    return {"rank": rank, "tier": tier, "matched_fields": sorted({span[2] for span in exact}),
            "nearby_numbers": nearby, "row_numbers": row_numbers, "qualifier_coverage": qualifier,
            "statement": structure["statement"], "field_coverage": coverage, "family_rows": family_rows,
            "structured": structured, "comparative": structure["comparative"], "table_header_row": table_row,
            "number_evidence": number_evidence}


def _page_key(metadata: dict[str, Any], record_id: str) -> tuple[str, str, str]:
    if metadata.get("source_type") == "pdf":
        return str(metadata.get("file_name", "")), str(metadata.get("source_id", "")), str(metadata.get("page", ""))
    return str(metadata.get("file_name", "")), str(metadata.get("source_id", "")), str(metadata.get("chunk_index", record_id))


class ExperimentalHybridRetrieval:
    """明确支持 hash + disabled：字面与字段证据主排，hash 仅候选/末级辅助。"""

    def __init__(self, rag: LocalResearchRAG) -> None:
        if rag.provider.mode != "hash" or rag.reranker.mode != "disabled":
            raise ValueError("实验仅支持 hash + disabled reranker；semantic 未测")
        self.rag = rag
        self._corpus: dict[str, list[dict[str, Any]]] = {}

    def _records(self, ticker: str) -> list[dict[str, Any]]:
        if ticker not in self._corpus:
            data = self.rag.collection.get(where={"ticker": ticker}, include=["documents", "metadatas"])
            ids, documents, metadatas = data["ids"], data["documents"], data["metadatas"]
            if len(ids) != len(documents) or len(ids) != len(metadatas) or any(
                not isinstance(meta, dict) or meta.get("ticker") != ticker for meta in metadatas
            ):
                raise ValueError("ticker_scope_violation:get")
            if len(set(ids)) != len(ids) or any(not isinstance(doc, str) for doc in documents):
                raise ValueError("record_binding_violation:get")
            records = []
            for record_id, content, metadata in zip(ids, documents, metadatas):
                normalized = _normalize(content)
                records.append({"id": record_id, "content": content, "metadata": dict(metadata),
                                "normalized": normalized, "terms": _terms(normalized),
                                "fields": _field_spans(normalized),
                                "tie": hashlib.sha256(content.encode("utf-8")).hexdigest()})
            for record in records:
                record["structure"] = _structure(record)
            self._corpus[ticker] = records
        return self._corpus[ticker]

    def search_with_candidates(self, query: str, ticker: str, limit: int = 4) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not ticker or not ticker.strip():
            raise ValueError("实验检索必须显式指定 ticker")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须为正整数")
        ticker = ticker.upper().strip()
        records = self._records(ticker)
        if not records:
            return [], []
        intent = _intent(query)
        pool = min(CANDIDATES_PER_BRANCH, len(records))
        response = self.rag.collection.query(
            query_embeddings=self.rag.provider.embed([intent["expanded"]]), n_results=pool,
            where={"ticker": ticker}, include=["documents", "metadatas", "distances"],
        )
        vector_ids, vector_docs = response["ids"][0], response["documents"][0]
        vector_meta, distances = response["metadatas"][0], response["distances"][0]
        if (len(vector_ids) != len(vector_docs) or len(vector_ids) != len(vector_meta)
            or len(vector_ids) != len(distances) or any(
                not isinstance(meta, dict) or meta.get("ticker") != ticker for meta in vector_meta
            )):
            raise ValueError("ticker_scope_violation:query")
        bound = {record["id"]: record for record in records}
        vector = {}
        for record_id, document, metadata, distance in zip(vector_ids, vector_docs, vector_meta, distances):
            if record_id not in bound or document != bound[record_id]["content"] or metadata != bound[record_id]["metadata"]:
                raise ValueError("record_binding_violation:query")
            if not math.isfinite(float(distance)):
                raise ValueError("invalid_distance:query")
            vector[record_id] = float(distance)
        n = len(records)
        query_terms = intent["terms"]
        df = Counter(term for record in records for term in record["terms"] if term in query_terms)
        avg_len = sum(sum(record["terms"].values()) for record in records) / n or 1.0
        pages: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in records:
            length = sum(record["terms"].values())
            literal = 0.0
            for term in query_terms:
                tf = record["terms"].get(term, 0)
                if tf:
                    idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                    literal += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg_len))
            evidence = _evidence(record, intent, literal)
            distance = vector.get(record["id"])
            vector_score = 0.0 if distance is None else 1.0 / (1.0 + max(distance, 0.0))
            rank = evidence["rank"] + (vector_score,)
            item = {"record": record, "evidence": evidence, "literal": literal, "rank": rank,
                    "distance": distance, "vector_score": vector_score}
            key = _page_key(record["metadata"], record["id"])
            old = pages.get(key)
            if old is None or rank > old["rank"] or (rank == old["rank"] and (record["tie"], record["id"]) < (old["record"]["tie"], old["record"]["id"])):
                pages[key] = item
        ordered = sorted(pages.values(), key=lambda item: (tuple(-part for part in item["rank"]), item["record"]["tie"], item["record"]["id"]))
        literal_pages = [item for item in ordered if item["literal"] > 0 or item["evidence"]["tier"] > 0][:pool]
        lrank = {_page_key(item["record"]["metadata"], item["record"]["id"]): i + 1 for i, item in enumerate(literal_pages)}
        vector_pages: dict[tuple[str, str, str], float] = {}
        for record_id, distance in vector.items():
            key = _page_key(bound[record_id]["metadata"], record_id)
            vector_pages[key] = min(distance, vector_pages.get(key, math.inf))
        vrank = {key: i + 1 for i, (key, _) in enumerate(sorted(vector_pages.items(), key=lambda item: (item[1], item[0][1])))}
        candidates = []
        for order_index, item in enumerate(ordered):
            record, evidence = item["record"], item["evidence"]
            key = _page_key(record["metadata"], record["id"])
            if key not in lrank and key not in vrank:
                continue
            # 分数只作可审计展示；真实顺序为上述证据元组，不再伪称 RRF/语义分数。
            score = float(len(ordered) - order_index)
            candidates.append({
                "content": record["content"], "metadata": dict(record["metadata"]),
                "distance": item["distance"], "vector_score": item["vector_score"],
                "lexical_score": item["literal"], "rerank_score": score, "anchor_score": evidence["field_coverage"],
                "fusion_score": score, "vector_rank": vrank.get(key), "literal_rank": lrank.get(key),
                "matching_mode": "financial_fields" if intent["fields"] else "literal_only_unknown_field",
                "query_ambiguities": intent["ambiguities"],
                "ranking_evidence": {key: value for key, value in evidence.items() if key != "rank"},
                "rules_sha256": RULES_SHA256,
            })
        return ([{**item, "citation": f"S{i + 1}"} for i, item in enumerate(candidates[:limit])], candidates)

    def search(self, query: str, ticker: str, limit: int = 4) -> list[dict[str, Any]]:
        return self.search_with_candidates(query, ticker, limit)[0]
