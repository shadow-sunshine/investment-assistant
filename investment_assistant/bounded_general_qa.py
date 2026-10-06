"""受控的公司资料和行业观察问答；只返回已固定的一手资料事实。"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .config import DATA_DIR, KNOWLEDGE_DIR

ISSUER_MANIFEST = DATA_DIR / "issuer_materials.json"
INDUSTRY_MANIFEST = DATA_DIR / "industry_sources.json"
INDUSTRY_DIR = DATA_DIR / "industry_sources"
NETEASE_SOURCE_URL = "https://www.sec.gov/Archives/edgar/data/1110646/000110465926043468/ntes-20251231x20f.htm"
NBS_URLS = {
    "nbs_20260915_activity": "https://www.stats.gov.cn/sj/zxfbhjd/202609/t20260915_1965307.html",
    "nbs_20260928_profit": "https://www.stats.gov.cn/sj/zxfbhjd/202609/t20260928_1965425.html",
}

ISSUER_HTML_SHA256 = "03040461c8973a557d94fb693e23e9e6862599872d53c68238a571badfa76315"
ISSUER_SHA256 = "1b5fb652d9f6fd641d9cd09e7b345325211a69e4f1df902370bba5ed5d29a83b"
NBS_HTML_SHA256 = {
    "nbs_20260915_activity": "7d339b3990b8fcac366131d2defad0b756d8583856491c6d1b211a19d7a90c11",
    "nbs_20260928_profit": "de0a8fa2bb77dc439eab0336a39b426f746d0c0efd9cc01de4c9725e46b262d0",
}
NBS_SHA256 = {
    "nbs_20260915_activity": "f4c08755d1878fb8b447328e85ceb064c7b64f00aec3d9842ba11b69ee4be9d3",
    "nbs_20260928_profit": "9426b75839a908097f60e22c27f7d28a6a2cbc59aad363cb71375a0b9b7e286e",
}


class BoundedSourceUnavailable(Exception):
    """官方来源快照缺失、版本漂移或证据锚点不匹配。"""


def _read_manifest(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoundedSourceUnavailable("source_manifest_unavailable") from exc


def _verified_file(directory: Path, file_name: Any, expected_sha: Any) -> Path:
    if (not isinstance(file_name, str) or not file_name or Path(file_name).name != file_name
            or file_name in {".", ".."} or not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)):
        raise BoundedSourceUnavailable("source_identity_invalid")
    path = directory / file_name
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BoundedSourceUnavailable("source_file_missing") from exc
    if actual != expected_sha:
        raise BoundedSourceUnavailable("source_sha256_drift")
    return path


def _source(entry: dict[str, Any], *, citation: str, excerpt: str, page: int | None = None) -> dict[str, Any]:
    identity = {
        "ticker": entry.get("ticker"), "file_name": entry["file_name"],
        "source_sha256": entry.get("text_sha256") or entry["validation"]["sha256"],
        "page": page, "page_authority": entry.get("page_authority"),
        "published_at": entry.get("published_at") or entry.get("filing_date"),
        "source_url": entry["source_url"],
    }
    return {"citation": citation, "identity": identity, "excerpt": excerpt}


def _quote(text: str, pattern: str) -> str:
    """只展示来源中真实存在的连续片段，不把归纳文字伪装成原文。"""
    match = re.search(pattern, text, re.I)
    if not match:
        raise BoundedSourceUnavailable("source_quote_mismatch")
    return match.group(0)


class IssuerAnswerService:
    """网易 2025 年 20-F 中有据可核的收入、证券代码和期末股数。"""

    def answer(self, ticker: str, question: str, *, requested_by: str) -> dict[str, Any]:
        if str(ticker).strip().upper() not in {"NTES", "9999.HK"}:
            raise ValueError("issuer_not_supported")
        normalized = str(question).strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("question_invalid")
        manifest = _read_manifest(ISSUER_MANIFEST)
        entry = manifest.get("NETEASE_2025") if isinstance(manifest, dict) else None
        if (not isinstance(entry, dict) or entry.get("ticker") != "9999.HK"
                or entry.get("source_url") != NETEASE_SOURCE_URL
                or entry.get("report_date") != "2025-12-31"
                or entry.get("page_authority") != "generated"
                or not isinstance(entry.get("validation"), dict)
                or entry["validation"].get("sha256") != ISSUER_SHA256
                or entry.get("file_name") != "NTES_SEC_20F_2025_official-html.pdf"
                or entry.get("filing_date") != "2026-04-15"
                or entry.get("source_html_sha256") != ISSUER_HTML_SHA256):
            raise BoundedSourceUnavailable("issuer_manifest_invalid")
        path = _verified_file(KNOWLEDGE_DIR, entry.get("file_name"), entry["validation"].get("sha256"))
        try:
            reader = PdfReader(str(path))
            pages = entry["verified_pages"]
            if len(reader.pages) != entry["validation"].get("page_count") or len(reader.pages) != 304:
                raise BoundedSourceUnavailable("issuer_page_count_drift")
            if (not isinstance(pages, dict) or pages != {"listing_us": 18, "listing_hk": 19, "shares_outstanding": 19, "revenue_table": 107, "revenue_summary": 108}
                    or any(type(number) is not int or number < 1 or number > len(reader.pages) for number in pages.values())):
                raise BoundedSourceUnavailable("issuer_pages_invalid")
            texts = {key: reader.pages[number - 1].extract_text() or "" for key, number in pages.items()}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise BoundedSourceUnavailable("issuer_pages_invalid") from exc
        if ("NTES" not in texts["listing_us"] or "9999" not in texts["listing_hk"]
                or "3,192,111,251" not in texts["shares_outstanding"]
                or "112,625,807" not in texts["revenue_table"]
                or "RMB112.6 billion" not in texts["revenue_summary"]):
            raise BoundedSourceUnavailable("issuer_evidence_mismatch")
        years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", normalized))
        if re.search(r"苹果公司|微软|腾讯|宁德时代|贵州茅台|平安银行|五粮液|(?<![A-Za-z0-9])(?:AAPL|MSFT|0700\.HK|600519\.SS|300750\.SZ|000001\.SZ|000858\.SZ)(?![A-Za-z0-9])", normalized, re.I):
            return {"status": "refused", "ticker": "9999.HK", "question": normalized,
                    "answer": "问题涉及其他公司，不能用网易的年报作答；请一次只问一家公司。", "sources": [],
                    "limitations": ["未混用跨标的证据。"], "requested_by": requested_by}
        general_limits = ["仅依据网易 2025 年 SEC 20-F；PDF 页码为本地转换页码，非官方页码。", "不提供实时股价、交易指令或投资建议。"]
        def response(status: str, answer: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
            return {"status": status, "ticker": "9999.HK", "question": normalized, "answer": answer,
                    "sources": sources, "limitations": general_limits, "requested_by": requested_by}
        listing_sources = [
            _source(entry, citation="N1", page=pages["listing_us"], excerpt=_quote(texts["listing_us"], r"NTES")),
            _source(entry, citation="N2", page=pages["listing_hk"], excerpt=_quote(texts["listing_hk"], r"9999\s+The Stock Exchange of Hong Kong Limited")),
        ]
        if re.search(r"买入|购买|怎么买|值得买|推荐买|下单|股价|行情|现价|实时价格", normalized):
            return response("refused", "我能核验证券代码和历史年报事实，但不能代你买入、提供实时股价或判断现在是否值得买。", [])
        if re.search(r"收入|营收|net\s*revenues?", normalized, re.I):
            if years != {"2025"}:
                return response("refused", "请明确询问 2025 年收入；这份年报不能证明其他年度或最新季度的收入。", [])
            excerpt = "Total net revenues increased by 7.0% to RMB112.6 billion (US$16.1 billion) in 2025 from RMB105.3 billion in 2024."
            if excerpt not in texts["revenue_summary"]:
                raise BoundedSourceUnavailable("issuer_revenue_quote_drift")
            return response("answered", "网易 2025 财年净收入约为人民币 1,126 亿元（年报原文为 RMB112.6 billion）。[N3] 这是全年历史数据，不代表 2026 年收入。",
                            [_source(entry, citation="N3", page=pages["revenue_summary"], excerpt=excerpt)])
        if re.search(r"总股本|股数|已发行股|股份数量|流通股", normalized):
            if years and years != {"2025"}:
                return response("refused", "我只有截至 2025 年末的年报股数，不能当作其他日期的实时股本。", [])
            return response("answered", "网易 20-F 披露截至 2025 年 12 月 31 日有 3,192,111,251 股普通股在外流通。[N4] 这不是当前实时股数。",
                            [_source(entry, citation="N4", page=pages["shares_outstanding"], excerpt=_quote(texts["shares_outstanding"], r"3,192,111,251\s+ordinary shares, par value US\$0\.0001 per share\."))])
        if re.search(r"代码|股票|股份|证券|上市|NTES|9999", normalized, re.I):
            return response("needs_clarification", "网易的港股代码是 9999，美国存托股代码是 NTES。[N1][N2] 你想查证券代码、2025 年末股数、2025 年收入，还是实时股价？最后一项目前不能核验。", listing_sources)
        return response("needs_clarification", "我已找到网易 2025 年官方年报。你想了解收入、年末股数，还是证券代码？", listing_sources)


class IndustryObservationService:
    """用定期固定的统计局资料提供研究线索，不输出证券或收益推荐。"""

    def answer(self, question: str, *, requested_by: str, today: date | None = None) -> dict[str, Any]:
        normalized = str(question).strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("question_invalid")
        today = today or date.today()
        manifest = _read_manifest(INDUSTRY_MANIFEST)
        entries = manifest.get("sources") if isinstance(manifest, dict) else None
        if not isinstance(entries, list) or {e.get("id") for e in entries if isinstance(e, dict)} != set(NBS_URLS) or len(entries) != 2:
            raise BoundedSourceUnavailable("industry_manifest_invalid")
        sources: dict[str, tuple[dict[str, Any], str]] = {}
        for entry in entries:
            if (entry.get("source_url") != NBS_URLS[entry["id"]] or entry.get("source") != "国家统计局"
                    or entry.get("text_sha256") != NBS_SHA256[entry["id"]]
                    or entry.get("source_html_sha256") != NBS_HTML_SHA256[entry["id"]]
                    or entry.get("file_name") != {"nbs_20260915_activity": "nbs_20260915_activity.txt", "nbs_20260928_profit": "nbs_20260928_profit.txt"}[entry["id"]]
                    or entry.get("published_at") != {"nbs_20260915_activity": "2026-09-15", "nbs_20260928_profit": "2026-09-28"}[entry["id"]]):
                raise BoundedSourceUnavailable("industry_source_invalid")
            path = _verified_file(INDUSTRY_DIR, entry.get("file_name"), entry.get("text_sha256"))
            try:
                published = date.fromisoformat(entry["published_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BoundedSourceUnavailable("industry_date_invalid") from exc
            if published > today or (today - published).days > 45:
                raise BoundedSourceUnavailable("industry_source_stale")
            try:
                sources[entry["id"]] = entry, path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise BoundedSourceUnavailable("industry_text_unavailable") from exc
        years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", normalized))
        if re.search(r"网易|NetEase|(?<![A-Za-z0-9])(?:NTES|AAPL|MSFT|9999\.HK)(?![A-Za-z0-9])|宁德时代|贵州茅台|平安银行|五粮液|腾讯|苹果公司|微软", normalized, re.I):
            return {"status": "refused", "answer": "这是行业层面的官方统计，不能回答单家公司问题；请单独询问公司年报。", "sources": [], "limitations": [], "requested_by": requested_by}
        if years and years != {"2026"}:
            return {"status": "refused", "answer": "当前行业观察只覆盖 2026 年截至 8 月的官方统计，不能回答其他年份。", "sources": [], "limitations": [], "requested_by": requested_by}
        activity, activity_text = sources["nbs_20260915_activity"]
        profit, profit_text = sources["nbs_20260928_profit"]
        activity_compact = re.sub(r"\s+", "", activity_text)
        profit_compact = re.sub(r"\s+", "", profit_text)
        expected = [
            (activity_compact, "高技术制造业增加值增长16.7%"),
            (activity_compact, "信息传输、软件和信息技术服务业", "9.6%"),
            (profit_compact, "计算机、通信和其他电子设备制造业利润同比增长1.1倍"),
            (profit_compact, "汽车制造业下降16.0%"),
        ]
        if not all(all(part in text for part in terms) for text, *terms in expected):
            raise BoundedSourceUnavailable("industry_evidence_mismatch")
        excerpts = [
            _source(activity, citation="I1", excerpt=_quote(activity_text, r"高技术制造业增加值增长\s*16\.7%")),
            _source(activity, citation="I2", excerpt=_quote(activity_text, r"信息传输、软件和信息技术服务业，租赁和商务服务业，交通运输、仓储和邮政业生产指数同比分别增长\s*9\.6%")),
            _source(profit, citation="I3", excerpt=_quote(profit_text, r"计算机、通信和其他电子设备制造业利润同比增长\s*1\.1\s*倍")),
            _source(profit, citation="I4", excerpt=_quote(profit_text, r"汽车制造业下降\s*16\.0%")),
        ]
        answer = (
            "如果你指 2026 年，可先把高技术制造业和信息软件服务列为进一步研究的方向，而不是买入清单。"
            "国家统计局披露：8 月高技术制造业增加值同比增长 16.7% [I1]，信息软件服务业生产指数同比增长 9.6% [I2]；"
            "1—8 月电子设备制造业利润同比增长 1.1 倍 [I3]，但汽车制造业利润同比下降 16.0% [I4]。"
            "这些是生产/利润统计，不等于板块股价表现；还需结合估值、企业财报和你的风险期限进一步核验。"
        )
        return {"status": "answered", "answer": answer, "sources": excerpts,
                "as_of": "2026-09-28", "data_period": "2026-01-01/2026-08-31",
                "limitations": ["仅为中国统计局截至 2026 年 8 月的有限行业观察；来源超过 45 天会停止交付。", "不提供收益预测、个股推荐或适合个人的买卖建议。"],
                "requested_by": requested_by}
