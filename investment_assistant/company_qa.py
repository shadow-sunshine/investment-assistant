"""公司目录与受控事实问答；公司身份从资料清单派生，数值只取已审定锚点。"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from pypdf import PdfReader

from . import chinese_qa_service as chinese
from . import bounded_general_qa as bounded
from .config import DATA_DIR, KNOWLEDGE_DIR
from .question_intents import requested_metrics

MANIFEST_PATH = DATA_DIR / "materials_manifest.json"
ANCHORS_PATH = DATA_DIR / "financial_fact_anchors.json"
ONBOARDED_PATH = DATA_DIR / "onboarded_materials.json"
# 别名只负责公司身份，不是按公司分支的业务规则；覆盖没有中文名称的英文年报。
EXTRA_ALIASES = {
    "0700.HK": ("腾讯", "腾讯控股", "Tencent"),
    "MSFT": ("微软", "Microsoft"),
    "AAPL": ("苹果公司", "Apple Inc"),
}
MARKET_CODE = re.compile(r"(?<![A-Za-z0-9])(?:\d{6}\.(?:SZ|SS)|\d{4,5}\.HK|[A-Z]{2,5})(?![A-Za-z0-9])", re.I)
YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")
UNKNOWN_COMPANY_PERIOD = re.compile(r"[\u4e00-\u9fff]{2,12}.{0,8}20\d{2}年?.{0,10}(?:收入|营收|净利润|股数)")
REVENUE = re.compile(r"收入|营收|revenue|营业额", re.I)
LIVE = re.compile(r"实时|现价|股价|最新价格|买入|卖出|购买|值得买|推荐买")
LATEST = re.compile(r"最新|当前|现在|截至目前|今年")
QUARTER = re.compile(r"(?:第?[一二三四1-4]季度|Q[1-4]|季报|上半年|下半年|半年报)", re.I)


class CompanySourceUnavailable(Exception):
    """目录或证据版本无效，不得交付事实。"""


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompanySourceUnavailable("manifest_unavailable") from exc
    if not isinstance(value, dict):
        raise CompanySourceUnavailable("manifest_invalid")
    return value


def _report_year(entry: dict[str, Any]) -> str | None:
    date = str(entry.get("report_date") or "")
    match = re.fullmatch(r"(20\d{2})(?:-\d{2}-\d{2})?", date)
    return match.group(1) if match else None


def catalog() -> dict[str, dict[str, Any]]:
    """每次读取当前资料清单；别名不改变资料身份、年度或验证值。"""
    materials = _json(MANIFEST_PATH)
    onboarded = _json(ONBOARDED_PATH)
    if materials.keys() & onboarded.keys():
        raise CompanySourceUnavailable("catalog_material_collision")
    materials.update(onboarded)
    issuer = _json(bounded.ISSUER_MANIFEST)
    result = {}
    for key, entry in materials.items():
        if not isinstance(entry, dict) or entry.get("ticker") != key:
            raise CompanySourceUnavailable("catalog_entry_invalid")
        validation = entry.get("validation") or {}
        url = entry.get("source_url")
        try:
            host = urlparse(url).hostname if isinstance(url, str) else None
        except ValueError as exc:
            raise CompanySourceUnavailable("catalog_source_url_invalid") from exc
        if (host not in {"static.cninfo.com.cn", "www1.hkexnews.hk", "www.sec.gov"}
                or urlparse(url).scheme not in {"https", "http"}
                or (host != "static.cninfo.com.cn" and not url.startswith("https://"))):
            raise CompanySourceUnavailable("catalog_source_url_invalid")
        if (not isinstance(validation, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(validation.get("sha256")))
                or type(validation.get("page_count")) is not int or validation["page_count"] < 1
                or not isinstance(entry.get("file_name"), str)
                or entry["file_name"] in {".", ".."}
                or Path(entry["file_name"]).name != entry["file_name"]):
            raise CompanySourceUnavailable("catalog_identity_invalid")
        company = entry.get("company")
        if not company and str(entry.get("announcement_title") or "").endswith("2025年年度报告"):
            company = str(entry["announcement_title"]).removesuffix("2025年年度报告")
        # 已有公司的中文名称不能由清单任意重命名成另一家公司。
        trusted_names = chinese.COMPANY_ALIASES.get(key) or EXTRA_ALIASES.get(key)
        if trusted_names and company and company not in trusted_names:
            raise CompanySourceUnavailable("catalog_company_name_drift")
        aliases = {key, *EXTRA_ALIASES.get(key, ())}
        if company:
            aliases.add(str(company))
        if key in chinese.COMPANY_ALIASES:
            aliases.update(chinese.COMPANY_ALIASES[key])
        result[key] = {"ticker": key, "aliases": tuple(sorted(aliases, key=len, reverse=True)),
                       "display_name": str(company or next((alias for alias in EXTRA_ALIASES.get(key, ()) if any("\u4e00" <= c <= "\u9fff" for c in alias)), key)),
                       "year": _report_year(entry), "entry": entry,
                       "engine": "chinese" if key in chinese.CHINESE_TICKERS else "anchored"}
    # 仅接入已由原服务核验的 SEC 网易快照；不能让任意 issuer 清单声明新公司身份。
    if set(issuer) != {"NETEASE_2025"}:
        raise CompanySourceUnavailable("issuer_catalog_unapproved")
    entry = issuer["NETEASE_2025"]
    if (not isinstance(entry, dict) or entry.get("ticker") != "9999.HK"
            or entry.get("source_url") != bounded.NETEASE_SOURCE_URL
            or entry.get("report_date") != "2025-12-31"
            or not isinstance(entry.get("validation"), dict)
            or entry["validation"].get("sha256") != bounded.ISSUER_SHA256
            or not isinstance(entry.get("aliases"), list)
            or not all(isinstance(alias, str) for alias in entry["aliases"])
            or set(entry["aliases"]) != {"NTES", "9999.HK", "网易", "NetEase"}
            or "9999.HK" in result):
        raise CompanySourceUnavailable("issuer_identity_invalid")
    result["9999.HK"] = {"ticker": "9999.HK", "aliases": tuple(entry["aliases"]),
                          "year": _report_year(entry), "entry": entry,
                          "engine": "issuer", "display_name": "网易"}
    alias_owners: dict[str, str] = {}
    for code, record in result.items():
        for alias in record["aliases"]:
            if not isinstance(alias, str) or not alias.strip():
                raise CompanySourceUnavailable("catalog_alias_invalid")
            folded = alias.casefold()
            if folded in alias_owners and alias_owners[folded] != code:
                raise CompanySourceUnavailable("catalog_alias_collision")
            alias_owners[folded] = code
    return result


def references(question: str, records: dict[str, dict[str, Any]] | None = None) -> tuple[set[str], set[str]]:
    records = records if records is not None else catalog()
    normalized = unicodedata.normalize("NFKC", question)
    targets: set[str] = set()
    for ticker, record in records.items():
        for alias in record["aliases"]:
            if not isinstance(alias, str) or not alias:
                continue
            if re.fullmatch(r"[A-Za-z0-9.]+", alias):
                matched = re.search(r"(?<![A-Za-z0-9.])" + re.escape(alias) + r"(?![A-Za-z0-9.])", normalized, re.I)
            else:
                matched = alias in normalized
            if matched:
                targets.add(ticker)
    # 一个交易标的不能带进另一个公司；NTES 是网易美股别名，不是另一公司。
    codes = {code.upper() for code in MARKET_CODE.findall(normalized)}
    codes -= {"RMB", "HKEX", "SEC", "USD", "Q4"}
    aliases_as_codes = {alias.upper() for row in records.values() for alias in row["aliases"]
                        if re.fullmatch(r"[A-Za-z0-9.]+", alias)}
    return targets, codes - aliases_as_codes


# 这不是公司 NER：只接受已知别名及有限的无主体追问措辞；剩余实体样文字一律拒绝。
# 指标词复用中文问答已有字段，避免新目录与字段名漂移。
_NEUTRAL_WORDS = (
    "请告诉我", "请帮我", "帮我查", "我想知道", "我要查", "想知道", "查一下", "查询", "请问", "请", "问",
    "这家", "那家", "该公司", "这家公司", "这份报告", "这个报告", "该报告", "本报告", "这份年报", "公司", "企业", "报告", "财报", "年报", "年度报告", "摘要", "概览", "给我", "一份", "一版", "提供", "查看", "总结", "整理",
    "它", "那", "这条", "上面", "刚才", "关于", "的", "和", "与", "及", "还有", "对比",
    "今年", "去年", "全年", "财年", "年度", "年", "截至", "期末", "现在", "最新", "多少", "是多少",
    "有没有", "是什么", "来自哪里", "来自哪一页", "来源", "在哪页", "哪一页", "一页", "页",
    "数据", "数字", "情况", "如何", "怎么样", "为什么", "下降", "上升", "增长", "减少", "同比", "环比", "呢", "啊", "看看", "看", "了解", "查", "要", "我",
    "股份", "股票", "证券", "代码", "营业额", "总股本", "股数", "已发行股", "流通股", "净收入", "收入", "营收",
    "利息收入", "投资收入", "服务收入", "经营现金流", "现金流", "风险", "利润", "股价", "市值", "投资", "建议",
    "官方资料", "官方文件", "披露清单", "官方披露", "披露", "清单", "目录", "有哪些", "有哪", "列表", "已收录", "已接入", "覆盖范围", "资料", "文件", "最近", "季度", "半年度", "本地", "全部", "都要", "总收入", "回购", "关联方", "担保", "金额上限", "不超过", "美元", "人民币", "提交日期", "公告", "日期", "研发费用", "研发开支", "股",
)
_FACT_WORDS = re.compile(r"收入|营收|营业额|revenue|利润|现金流|股价|股数|股份|股本|证券|代码|市值|风险|财报|年报|年度报告|数据|货币资金|资产|负债|研发|费用|存款|净息差", re.I)


def unverified_company_subject(question: str, records: dict[str, dict[str, Any]], *, include_non_fact: bool = False) -> bool:
    """只用于公司事实与报告作用域；不识别任意公司，残留实体样文字就停下澄清。"""
    if not include_non_fact and not _FACT_WORDS.search(question):
        return False
    residual = unicodedata.normalize("NFKC", question)
    for row in records.values():
        for alias in row["aliases"]:
            if re.fullmatch(r"[A-Za-z0-9.]+", alias):
                residual = re.sub(r"(?<![A-Za-z0-9.])" + re.escape(alias) + r"(?![A-Za-z0-9.])", "", residual, flags=re.I)
            else:
                residual = residual.replace(alias, "")
    residual = re.sub(r"(?<!\d)20\d{2}\s*年?(?!\d)", "", residual)
    words = {word for _, aliases in chinese.FIELD_ALIASES for word in aliases}
    words.update(_NEUTRAL_WORDS)
    words.update(("revenue", "net revenues", "RMB", "USD", "2025", "2024"))
    # 从左至右一次匹配最长已知语法词，不依赖 set 的进程随机顺序。
    # 逐词删除会把“官方披露清单”拆出“官方”残片，误判为另一家公司。
    grammar = "|".join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word)))
    residual = re.sub(grammar, "", residual, flags=re.I)
    residual = re.sub(r"[\s\d，,。！？!?：:、；;（）()·—-]+", "", residual)
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", residual))


def _document_date_key(value: Any) -> tuple[int, int, int, int]:
    """把 SEC/CNINFO/HKEX 常见日期格式归一为可排序键。"""
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        return tuple(map(int, match.groups())) + (0,)
    match = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](20\d{2})", text)
    if match:
        day, month, year = map(int, match.groups())
        return year, month, day, 0
    match = re.search(r"20\d{2}", text)
    return (int(match.group(0)), 0, 0, 0) if match else (0, 0, 0, 0)


def _official_coverage(ticker: str) -> list[dict[str, Any]]:
    """读取已入库的一手官方文档覆盖，不把文档索引误当成已核验字段。"""
    try:
        from .official_document_ingestion import OfficialDocumentStore
        documents = OfficialDocumentStore().list_documents(ticker=ticker)
    except Exception:  # noqa: BLE001 - 覆盖状态不可用时仍要返回受控拒答
        return []
    result = []
    for document in documents:
        report_date = str(document.get("report_date") or "")
        filing_date = str(document.get("filing_date") or "")
        year = report_date[:4] if re.match(r"20\d{2}", report_date) else None
        if not year:
            year_match = re.search(r"20\d{2}", filing_date)
            year = year_match.group(0) if year_match else None
        result.append({"year": year, "report_date": report_date, "filing_date": filing_date,
                      "title": document.get("title"), "filing_form": document.get("filing_form"),
                      "source": document.get("source"), "source_url": document.get("source_url"),
                      "document_id": document.get("document_id")})
    return sorted(result, key=lambda item: (_document_date_key(item.get("filing_date")),
                                             _document_date_key(item.get("report_date"))), reverse=True)


def _coverage_message(ticker: str, record: dict[str, Any], requested_year: str | None = None) -> tuple[str, str, dict[str, Any]]:
    coverage = _official_coverage(ticker)
    indexed_years = sorted({str(item["year"]) for item in coverage if item.get("year")}, reverse=True)
    annual_year = str(record.get("year") or "")
    latest = coverage[0] if coverage else None
    if requested_year and requested_year in indexed_years:
        detail = f"已发现 {requested_year} 年官方资料（{latest.get('title') if latest and latest.get('year') == requested_year else '公告/披露文件'}），但该字段尚未建立数值证据锚点。"
        reason = "OFFICIAL_DOCUMENT_FIELD_UNVERIFIED"
    elif requested_year:
        detail = f"当前已核验的年度年报是 {annual_year or '已有资料'} 年；未发现已入库的 {requested_year} 年同公司官方资料。"
        reason = "OFFICIAL_PERIOD_NOT_ONBOARDED"
    else:
        detail = f"当前已核验的年度年报是 {annual_year or '已有资料'} 年。"
        reason = "LATEST_OFFICIAL_DOCUMENT_REQUIRED"
    if indexed_years:
        detail += " 已入库官方资料期间：" + "、".join(indexed_years) + "。"
    detail += "不会用旧年度替代当前期间；可先走官方资料发现/入库，再回答该期间。"
    return detail, reason, {"annual_report_year": annual_year, "indexed_years": indexed_years,
                            "documents": coverage[:8]}


def _refused(ticker: str, question: str, answer: str, reason: str, requested_by: str) -> dict[str, Any]:
    return {"status": "refused", "ticker": ticker, "question": question, "answer": answer,
            "error_code": reason, "sources": [], "limitations": ["未取得可核验的同公司、同期间证据；不使用其他公司或季度数据补足。"],
            "requested_by": requested_by}


def _annual_revenue_intent(question: str, record: dict[str, Any]) -> bool:
    """只接受单一年度总收入问句；利息收入、净收入等不得改写成营业收入。"""
    remainder = unicodedata.normalize("NFKC", question)
    for alias in record["aliases"]:
        if re.fullmatch(r"[A-Za-z0-9.]+", alias):
            remainder = re.sub(r"(?<![A-Za-z0-9.])" + re.escape(alias) + r"(?![A-Za-z0-9.])", "", remainder, flags=re.I)
        else:
            remainder = remainder.replace(alias, "")
    remainder = re.sub(r"(?<!\d)20\d{2}\s*年?(?!\d)", "", remainder)
    remainder = re.sub(r"\s+", "", remainder)
    return bool(re.fullmatch(r"(?:请问|查询|查一下)?的?(?:全年|年度)?(?:营业收入|营收|总收入|收入|营业额)(?:怎么样|如何|是多少|多少|情况|呢)?[?？。]?", remainder))


def _verified_chinese_revenue(ticker: str, question: str, record: dict[str, Any], spec: dict[str, Any], requested_by: str) -> dict[str, Any]:
    """锁定当前官方 PDF 字节、页码、表头、单位与同一收入行。"""
    entry, validation = record["entry"], record["entry"]["validation"]
    name, sha, page = spec.get("file_name"), spec.get("source_sha256"), spec.get("page")
    if (spec.get("year") != record["year"] or name != entry.get("file_name")
            or sha != validation.get("sha256") or spec.get("source_url") != entry.get("source_url")
            or spec.get("page_authority") != entry.get("page_authority")
            or type(page) is not int or not isinstance(name, str) or Path(name).name != name
            or not re.fullmatch(r"[0-9a-f]{64}", str(sha))):
        raise CompanySourceUnavailable("chinese_revenue_anchor_identity_drift")
    if (spec.get("currency") != "RMB" or spec.get("unit") != "百万元"
            or spec.get("table") != "2.1 关键指标"
            or any(not re.fullmatch(r"[1-9][\d,]*", str(spec.get(key))) for key in ("value", "comparative"))
            or not re.fullmatch(r"\(\d{1,2}\.\d%\)", str(spec.get("yoy_change")))):
        raise CompanySourceUnavailable("chinese_revenue_anchor_invalid")
    try:
        data = (KNOWLEDGE_DIR / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise CompanySourceUnavailable("material_sha256_drift")
        reader = PdfReader(BytesIO(data))
        if len(reader.pages) != validation.get("page_count") or not 1 <= page <= len(reader.pages):
            raise CompanySourceUnavailable("anchor_page_invalid")
        text = unicodedata.normalize("NFKC", reader.pages[page - 1].extract_text() or "")
    except (OSError, ValueError, TypeError, IndexError) as exc:
        raise CompanySourceUnavailable("material_unavailable") from exc
    value, comparative, change = spec["value"], spec["comparative"], spec["yoy_change"]
    header = f"项 目 {record['year']} 年 {int(record['year']) - 1} 年 本年同比增减"
    row = f"营业收入 {value} {comparative} {change}"
    section = text.split(spec["table"], 1)[-1].split("2.2 主要会计数据", 1)[0]
    if (f"{record['year']} 年年度报告" not in text or spec["table"] not in text
            or "(货币单位:人民币百万元)" not in section or header not in section
            or len(re.findall(r"(?m)^" + re.escape(row) + r"$", section)) != 1
            or round((int(value.replace(",", "")) / int(comparative.replace(",", "")) - 1) * 100, 1)
            != -float(change.strip("()%"))):
        raise CompanySourceUnavailable("chinese_revenue_anchor_mismatch")
    identity = {"ticker": ticker, "file_name": name, "source_sha256": sha,
                "source_url": entry["source_url"], "page": page,
                "page_authority": entry["page_authority"], "report_year": record["year"]}
    excerpt = section[section.index("(货币单位:人民币百万元)"):section.index(row) + len(row)]
    yi = int(value.replace(",", "")) / 100
    return {"status": "answered", "ticker": ticker, "question": question, "field": "营业收入",
            "answer": f"{record['display_name']} {record['year']} 年营业收入为人民币 {value} 百万元（{yi:,.2f} 亿元），较 {int(record['year']) - 1} 年的 {comparative} 百万元同比下降 {change.strip('()')}。[C1] 仅能说明收入变化，不能据此判断投资价值。",
            "sources": [{"citation": "C1", "identity": identity, "excerpt": excerpt}],
            "limitations": ["依据官方年报关键指标表的全年收入列；不是季度或实时数据。", "不构成投资建议。"],
            "requested_by": requested_by}


class CompanyAnswerService:
    def __init__(self, chinese_service: Any | None = None, issuer_service: Any | None = None):
        self.chinese = chinese_service or chinese.ChineseKnowledgeAnswerService()
        self.issuer = issuer_service or bounded.IssuerAnswerService()

    def answer(self, ticker: str, question: str, *, requested_by: str) -> dict[str, Any]:
        if not isinstance(ticker, str) or not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError("company_input_invalid")
        ticker = ticker.strip().upper()
        question = unicodedata.normalize("NFKC", question).strip()
        records = catalog()
        if ticker not in records:
            return _refused(ticker, question, "尚未收录该公司已核验的官方资料，不能从其他公司或公开网页拼接答案。", "MATERIAL_NOT_ONBOARDED", requested_by)
        targets, unknown = references(question, records)
        if unknown or (targets and targets != {ticker}) or unverified_company_subject(question, records, include_non_fact=True):
            return _refused(ticker, question, "无法确认问题只涉及请求的公司；请写明单一已收录公司与期间。", "COMPANY_SCOPE_UNVERIFIED", requested_by)
        record = records[ticker]
        metrics = requested_metrics(question)
        # 整句通过主体门禁后再拆字段；每个字段单独核验，不能用收入代替利润。
        if len(metrics) > 1 and not LIVE.search(question):
            years = set(YEAR.findall(question))
            if len(years) != 1 or QUARTER.search(question):
                return _refused(ticker, question, "多指标查询请明确同一报告期间；季度与全年不能互相替代。", "PERIOD_SCOPE_MISMATCH", requested_by)
            year = next(iter(years))
            results, sources, lines = [], [], []
            from .official_document_answers import OfficialDocumentAnswerService
            document_service = None
            for number, metric in enumerate(metrics, 1):
                effective = f"{ticker} {year}年{metric}是多少？"
                answer = self.answer(ticker, effective, requested_by=requested_by)
                if answer.get("error_code") in {"FIELD_NOT_VERIFIED", "UNIT_NOT_VERIFIED", "OFFICIAL_DOCUMENT_FIELD_UNVERIFIED", "OFFICIAL_PERIOD_NOT_ONBOARDED"}:
                    if document_service is None:
                        document_service = OfficialDocumentAnswerService()
                    excerpt = document_service.answer(ticker, effective, requested_by=requested_by)
                    if excerpt.get("status") == "evidence_retrieved":
                        answer = excerpt
                rewritten = str(answer.get("answer") or "没有取得该字段证据。")
                for source in answer.get("sources") or []:
                    old = source.get("citation") or f"S{len(sources)+1}"
                    new = f"M{number}-{old}"
                    rewritten = rewritten.replace(f"[{old}]", f"[{new}]")
                    sources.append({**source, "citation": new})
                status = answer.get("status", "refused")
                label = "已核验" if status == "answered" else "官方原文待核验" if status == "evidence_retrieved" else "证据缺口"
                lines.append(f"**{metric}（{label}）**\n{rewritten}")
                results.append({"metric": metric, "status": status, "answer": rewritten, "error_code": answer.get("error_code")})
            complete = all(r["status"] == "answered" for r in results)
            return {"status": "answered" if complete else "partial", "ticker": ticker, "question": question,
                    "answer": "\n\n".join(lines), "metric_results": results, "sources": sources,
                    "limitations": ["每项字段分别核验；待核验摘录与证据缺口不能当作完整财务结论。"], "requested_by": requested_by}
        years = set(YEAR.findall(question))
        if QUARTER.search(question):
            return _refused(ticker, question, "季度或半年数据不能由全年年报收入替代。", "PERIOD_SCOPE_MISMATCH", requested_by)
        if LIVE.search(question):
            return _refused(ticker, question, "年报不是实时行情，也不能据此给出买卖建议。", "OUT_OF_SCOPE", requested_by)
        requested_year = next(iter(years)) if len(years) == 1 else None
        if len(years) > 1:
            return _refused(ticker, question, "一次只能查询一个报告期间；不会拼接不同年度的数字。", "REPORT_PERIOD_UNSUPPORTED", requested_by)
        if (requested_year and requested_year != str(record.get("year") or "")) or (not requested_year and LATEST.search(question)):
            message, reason, coverage = _coverage_message(ticker, record, requested_year)
            result = _refused(ticker, question, message, reason, requested_by)
            result["official_coverage"] = coverage
            return result
        if not requested_year and not LATEST.search(question):
            return _refused(ticker, question, f"请明确报告年度；当前已核验的年度年报是 {record['year'] or '已有资料'} 年。若要查最新期间，请直接说“最新官方资料”。", "REPORT_PERIOD_UNSUPPORTED", requested_by)
        if record["engine"] == "issuer":
                return self.issuer.answer(ticker, question, requested_by=requested_by)
        if record["engine"] == "chinese":
            if _annual_revenue_intent(question, record):
                spec = (_json(ANCHORS_PATH).get(ticker) or {}).get("revenue")
                if isinstance(spec, dict) and "comparative" in spec:
                    return _verified_chinese_revenue(ticker, question, record, spec, requested_by)
            normalized_question = re.sub("营收", "营业收入", question)
            result = self.chinese.answer(ticker, normalized_question, requested_by=requested_by)
            result["question"] = question
            # 服务内部核验 SHA/页码；目录同时附上经清单绑定的官方 URL 和年度。
            for source in result.get("sources") or []:
                identity = source.get("identity") or {}
                if identity.get("source_sha256") != record["entry"]["validation"]["sha256"]:
                    raise CompanySourceUnavailable("chinese_source_identity_drift")
                identity.update(source_url=record["entry"].get("source_url"), page_authority=record["entry"].get("page_authority"), report_year=record["year"])
            if result.get("evidence"):
                identity = result["evidence"].get("identity") or {}
                if identity.get("source_sha256") != record["entry"]["validation"]["sha256"]:
                    raise CompanySourceUnavailable("chinese_evidence_identity_drift")
                identity.update(source_url=record["entry"].get("source_url"), page_authority=record["entry"].get("page_authority"), report_year=record["year"])
            if result.get("status") == "answered" and REVENUE.search(question):
                spec = (_json(ANCHORS_PATH).get(ticker) or {}).get("revenue")
                evidence = result.get("evidence") or {}
                identity = evidence.get("identity") or {}
                value = (result.get("answer") or "").split("为 ")[-1].rstrip("。")
                if spec is None:
                    result["limitations"].append("数值的币种/单位未单独标注；请核对引用页，不自动换算。")
                    return result
                if (not isinstance(spec, dict) or spec.get("year") != record["year"]
                        or spec.get("file_name") != identity.get("file_name")
                        or spec.get("source_sha256") != identity.get("source_sha256")
                        or spec.get("source_url") != record["entry"].get("source_url")
                        or spec.get("page_authority") != record["entry"].get("page_authority")
                        or spec.get("page") != identity.get("page") or spec.get("value") != value
                        or spec.get("currency") != "RMB" or spec.get("unit") != "千元"):
                    return _refused(ticker, question, "收入数值或币种、单位尚未绑定已审定的年度证据页。", "UNIT_NOT_VERIFIED", requested_by)
                matched = next((source for source in result.get("sources") or []
                                if source.get("identity") == identity), None)
                if not matched:
                    raise CompanySourceUnavailable("citation_identity_mismatch")
                result["answer"] = f"{record['display_name']} {record['year']} 年营业收入为人民币 {value} 千元。[{matched['citation']}] 这是年报全年列，不是季度收入。"
                result["limitations"].append("单位依据已人工核对的固定官方 PDF 页；该页中文字符的程序化提取不完整。")
            return result
        return self._anchored(ticker, question, record, requested_by)

    def _anchored(self, ticker: str, question: str, record: dict[str, Any], requested_by: str) -> dict[str, Any]:
        if not _annual_revenue_intent(question, record):
            return _refused(ticker, question, "该年报问题暂无经过核验的字段锚点。当前总收入锚点不能回答利息收入、服务收入或净利润等其他指标。", "FIELD_NOT_VERIFIED", requested_by)
        anchors = _json(ANCHORS_PATH)
        spec = (anchors.get(ticker) or {}).get("revenue")
        if not isinstance(spec, dict) or spec.get("year") != record["year"]:
            return _refused(ticker, question, "该公司该年度收入尚未完成证据锚点核验。", "FIELD_NOT_VERIFIED", requested_by)
        entry = record["entry"]
        validation = entry["validation"]
        if (spec.get("source_sha256") != validation["sha256"] or spec.get("file_name") != entry.get("file_name")
                or spec.get("source_url") != entry.get("source_url") or spec.get("page_authority") != entry.get("page_authority")):
            raise CompanySourceUnavailable("anchor_manifest_drift")
        name, sha = entry.get("file_name"), validation.get("sha256")
        if not isinstance(name, str) or name in {".", ".."} or Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
            raise CompanySourceUnavailable("material_identity_invalid")
        path = KNOWLEDGE_DIR / name
        try:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise CompanySourceUnavailable("material_sha256_drift")
            reader = PdfReader(BytesIO(data))
            page = spec["page"]
            if type(page) is not int or len(reader.pages) != validation.get("page_count") or not 1 <= page <= len(reader.pages):
                raise CompanySourceUnavailable("anchor_page_invalid")
            text = reader.pages[page - 1].extract_text() or ""
        except (OSError, KeyError, ValueError, TypeError, IndexError) as exc:
            raise CompanySourceUnavailable("material_unavailable") from exc
        # 标注与当前页面共同锁定全年表头、先后年份、单位、收入总计行及下一行边界。
        if (not isinstance(spec.get("table"), str) or spec.get("period") != f"For the year ended 31 December {record['year']}"
                or spec.get("currency") != "RMB" or spec.get("unit") != "Million"
                or not re.fullmatch(r"[1-9][\d,]*", str(spec.get("value")))
                or not re.fullmatch(r"[1-9][\d,]*", str(spec.get("comparative")))):
            raise CompanySourceUnavailable("annual_revenue_anchor_invalid")
        tokens = ("table", "section", "first_component", "last_component", "total_note", "next_section")
        if any(not isinstance(spec.get(key), str) or not spec[key] for key in tokens):
            raise CompanySourceUnavailable("annual_revenue_anchor_invalid")
        header = (rf"{re.escape(spec['table'])}\s+{re.escape(spec['period'])}\s+Year ended 31 December\s+"
                  rf"{record['year']}\s+{int(record['year'])-1}\s+Note RMB\W{{0,5}}Million RMB\W{{0,5}}Million\s+{re.escape(spec['section'])}")
        row = (rf"{re.escape(spec['section'])}\s+{re.escape(spec['first_component'])}[\s\S]{{0,500}}?"
               rf"{re.escape(spec['last_component'])}\s+[\d,]+\s+[\d,]+\s+"
               rf"{re.escape(spec['total_note'])}\s+{re.escape(spec['value'])}\s+"
               rf"{re.escape(spec['comparative'])}\s+{re.escape(spec['next_section'])}")
        match = re.search(header, text)
        total = re.search(row, text)
        if not match or not total or total.start() < match.end() - len("Revenues"):
            raise CompanySourceUnavailable("annual_revenue_anchor_mismatch")
        excerpt = text[match.start():total.end()]
        source = {"citation": "C1", "identity": {"ticker": ticker, "file_name": name, "source_sha256": sha,
                  "source_url": entry["source_url"], "page": page, "page_authority": entry["page_authority"],
                  "report_year": record["year"]}, "excerpt": excerpt}
        return {"status": "answered", "ticker": ticker, "question": question, "field": "全年收入",
                "answer": f"{record['display_name']} {record['year']} 年全年收入为人民币 {spec['value']} 百万元。[C1] 对比列为 {int(record['year']) - 1} 年人民币 {spec['comparative']} 百万元；不是单季度收入。",
                "sources": [source], "limitations": ["仅据合并利润表的全年列；页码为本地 PDF 页码。", "不构成实时行情或投资建议。"],
                "requested_by": requested_by}
