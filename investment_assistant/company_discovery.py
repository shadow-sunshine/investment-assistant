"""公司候选解析：把"用户问的公司"映射为可审计的候选或已收录身份。

设计红线
--------
1. **本地官方清单是唯一身份权威**：``company_qa.catalog()`` 命中的公司直接返回
   ``onboarded``，不调用任何外部数据源。
2. **候选永远不是证据**：除官方清单命中外，所有候选 ``verified=False``。
   路由提示表（:data:`ROUTING_HINTS`）只是导航线索，不能回答任何财务数字。
3. **不静默选一个**：多 ticker 或多市场一律 ``ambiguous``，``selected_ticker=None``。
4. **不跨 ticker 合并**：本模块只做身份解析，不产出也不搬运任何财务数值。
5. **不接受任意 URL**：入参只有公司名/代码和市场枚举，抓取只经由受控 registry。
6. **默认不联网``：未注入 ``registry`` 时完全离线，只用本地清单与路由提示。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from . import company_qa
from .company_qa import CompanySourceUnavailable
from .market_data_sources import (
    STATUS_CANDIDATE,
    STATUS_EMPTY,
    STATUS_SUCCESS,
    STATUS_UNAVAILABLE,
    MarketDataSourceRegistry,
    SourceObservation,
    default_registry,
)

# --- 状态与错误码 ---------------------------------------------------------------

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNRESOLVED = "unresolved"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
STATUS_ONBOARDED = "onboarded"
DISCOVERY_STATUSES = frozenset(
    {STATUS_MATCHED, STATUS_AMBIGUOUS, STATUS_UNRESOLVED, STATUS_SOURCE_UNAVAILABLE, STATUS_ONBOARDED}
)

ERROR_COMPANY_NOT_FOUND = "COMPANY_NOT_FOUND"
ERROR_COMPANY_NOT_ONBOARDED = "COMPANY_NOT_ONBOARDED"
ERROR_COMPANY_AMBIGUOUS_MARKET = "COMPANY_AMBIGUOUS_MARKET"
ERROR_OFFICIAL_MATERIAL_REQUIRED = "OFFICIAL_MATERIAL_REQUIRED"
ERROR_FIELD_NOT_VERIFIED = "FIELD_NOT_VERIFIED"
ERROR_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
ERROR_CROSS_TICKER_QUERY = "CROSS_TICKER_QUERY"
ERROR_REPORT_PERIOD_UNSUPPORTED = "REPORT_PERIOD_UNSUPPORTED"
ERROR_CATALOG_UNAVAILABLE = "COMPANY_CATALOG_UNAVAILABLE"
ERROR_INPUT_INVALID = "COMPANY_INPUT_INVALID"

#: 资料接入状态（与总任务卡 3.3 的状态机一致）。
EVIDENCE_NOT_ONBOARDED = "not_onboarded"
EVIDENCE_MATERIAL_ONLY = "onboarded_material_only"
EVIDENCE_FIELD_VERIFIED = "verified_field_available"

MARKET_A = "A"
MARKET_HK = "HK"
MARKET_US = "US"
SUPPORTED_MARKETS = frozenset({MARKET_A, MARKET_HK, MARKET_US})

#: 各数据源声明支持的市场；解析时据此裁剪调用，不做无谓的跨市场抓取。
SOURCE_MARKETS: Mapping[str, frozenset[str]] = {
    "baostock": frozenset({MARKET_A}),
    "akshare": frozenset({MARKET_A, MARKET_HK, MARKET_US}),
}

WARNING_CANDIDATE_NOT_EVIDENCE = "candidate_not_official_annual_report"
WARNING_ROUTING_HINT = "routing_hint_not_evidence"
WARNING_CROSS_MARKET = "multiple_markets_require_confirmation"
WARNING_CROSS_TICKER = "multiple_tickers_require_confirmation"
WARNING_NO_MATCH_IN_REQUESTED_MARKET = "requested_market_no_match"
WARNING_SOURCE_FAILED = "external_source_unavailable"
WARNING_FIELD_GAP = "field_anchor_not_verified"
WARNING_MATERIAL_ONLY = "material_onboarded_field_pending"

MAX_QUERY_LENGTH = 100

# --- 路由提示表 -----------------------------------------------------------------

#: **导航线索，不是证据。** 只用于在离线/外部源不可用时把用户导向正确的市场与
#: 官方披露渠道，绝不用于回答收入、利润等任何数值。键为大小写折叠后的名称或代码别名。
ROUTING_HINTS: Mapping[str, tuple[dict[str, str], ...]] = {
    "阿里巴巴": ({"ticker": "9988.HK", "market": MARKET_HK, "official_channel": "HKEX"},
                {"ticker": "BABA", "market": MARKET_US, "official_channel": "SEC"}),
    "alibaba": ({"ticker": "9988.HK", "market": MARKET_HK, "official_channel": "HKEX"},
                {"ticker": "BABA", "market": MARKET_US, "official_channel": "SEC"}),
    "alibabagroup": ({"ticker": "9988.HK", "market": MARKET_HK, "official_channel": "HKEX"},
                    {"ticker": "BABA", "market": MARKET_US, "official_channel": "SEC"}),
    "9988.hk": ({"ticker": "9988.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "baba": ({"ticker": "BABA", "market": MARKET_US, "official_channel": "SEC"},),
    "小米": ({"ticker": "1810.HK", "market": MARKET_HK, "official_channel": "HKEX"},
            {"ticker": "XIAC", "market": MARKET_US, "official_channel": "SEC"}),
    "xiaomi": ({"ticker": "1810.HK", "market": MARKET_HK, "official_channel": "HKEX"},
               {"ticker": "XIAC", "market": MARKET_US, "official_channel": "SEC"}),
    "1810.hk": ({"ticker": "1810.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "美团": ({"ticker": "3690.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "meituan": ({"ticker": "3690.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "3690.hk": ({"ticker": "3690.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "京东": ({"ticker": "9618.HK", "market": MARKET_HK, "official_channel": "HKEX"},
            {"ticker": "JD", "market": MARKET_US, "official_channel": "SEC"}),
    "jd.com": ({"ticker": "9618.HK", "market": MARKET_HK, "official_channel": "HKEX"},
               {"ticker": "JD", "market": MARKET_US, "official_channel": "SEC"}),
    "百度": ({"ticker": "9888.HK", "market": MARKET_HK, "official_channel": "HKEX"},
            {"ticker": "BIDU", "market": MARKET_US, "official_channel": "SEC"}),
    "百度集团": ({"ticker": "9888.HK", "market": MARKET_HK, "official_channel": "HKEX"},
                {"ticker": "BIDU", "market": MARKET_US, "official_channel": "SEC"}),
    "bidu": ({"ticker": "BIDU", "market": MARKET_US, "official_channel": "SEC"},),
    "比亚迪": ({"ticker": "1211.HK", "market": MARKET_HK, "official_channel": "HKEX"},
              {"ticker": "002594.SZ", "market": MARKET_A, "official_channel": "CNINFO"}),
    "byd": ({"ticker": "1211.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "网易": ({"ticker": "9999.HK", "market": MARKET_HK, "official_channel": "HKEX"},
            {"ticker": "NTES", "market": MARKET_US, "official_channel": "SEC"}),
    "ntes": ({"ticker": "NTES", "market": MARKET_US, "official_channel": "SEC"},),
    "腾讯控股": ({"ticker": "0700.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "tencentholdings": ({"ticker": "0700.HK", "market": MARKET_HK, "official_channel": "HKEX"},),
    "苹果": ({"ticker": "AAPL", "market": MARKET_US, "official_channel": "SEC"},),
    "apple": ({"ticker": "AAPL", "market": MARKET_US, "official_channel": "SEC"},),
    "aapl": ({"ticker": "AAPL", "market": MARKET_US, "official_channel": "SEC"},),
    "微软": ({"ticker": "MSFT", "market": MARKET_US, "official_channel": "SEC"},),
    "microsoft": ({"ticker": "MSFT", "market": MARKET_US, "official_channel": "SEC"},),
    "msft": ({"ticker": "MSFT", "market": MARKET_US, "official_channel": "SEC"},),
}

_HINT_PROVENANCE: Mapping[str, Any] = {
    "provider": "local_routing_hint",
    "endpoint": "ROUTING_HINTS",
    "evidence_level": "navigation_only",
    "note": "routing hint for official filing channel; not financial evidence",
}

# --- 代码规范化 -----------------------------------------------------------------

_PREFIX_FORM = re.compile(r"^(?P<exchange>sz|sh)\.(?P<code>\d{6})$", re.I)
_SUFFIX_FORM = re.compile(r"^(?P<code>[0-9A-Za-z]+)\.(?P<suffix>HK|SZ|SS|SH|H|US|O|N)$", re.I)
_BARE_HK = re.compile(r"^\d{4,5}$")
_BARE_A = re.compile(r"^\d{6}$")
_BARE_US = re.compile(r"^[A-Z]{1,5}$")

_HINT_KEYS_BY_LENGTH = tuple(sorted(ROUTING_HINTS, key=len, reverse=True))


class CompanyDiscoveryInputError(ValueError):
    """入参不满足候选解析协议（空查询、非法市场、试图传入 URL 等）。"""

    error_code = ERROR_INPUT_INVALID


def normalize_market(value: Any) -> str | None:
    """把市场别名折叠为 ``A`` / ``HK`` / ``US``；``None`` 表示未指定。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise CompanyDiscoveryInputError("market_type_invalid")
    token = unicodedata.normalize("NFKC", value).strip().upper()
    if not token:
        return None
    alias = {
        "A": MARKET_A, "CN": MARKET_A, "SZ": MARKET_A, "SH": MARKET_A, "SS": MARKET_A,
        "A股": MARKET_A, "沪": MARKET_A, "深": MARKET_A,
        "HK": MARKET_HK, "HKEX": MARKET_HK, "港股": MARKET_HK,
        "US": MARKET_US, "USA": MARKET_US, "SEC": MARKET_US, "美股": MARKET_US,
    }.get(token)
    if alias is None:
        raise CompanyDiscoveryInputError("market_unsupported")
    return alias


def normalize_ticker(raw: str) -> dict[str, Any]:
    """规范化证券代码为 ``{ticker, market, ambiguous_market, raw}``。

    6 位裸代码无法区分深/沪，返回 ``ambiguous_market=True`` 而不是猜一个交易所。
    """
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text or len(text) > 24:
        raise CompanyDiscoveryInputError("ticker_invalid")
    if "://" in text or text.lower().startswith(("http:", "https:", "www.")):
        # 候选解析不接受任意 URL：抓取只能走受控 registry。
        raise CompanyDiscoveryInputError("url_not_accepted")
    prefix = _PREFIX_FORM.fullmatch(text)
    if prefix:
        exchange = prefix.group("exchange").lower()
        code = prefix.group("code")
        return {"ticker": f"{code}.{'SZ' if exchange == 'sz' else 'SS'}", "market": MARKET_A,
                "ambiguous_market": False, "raw": text}
    suffix = _SUFFIX_FORM.fullmatch(text)
    if suffix:
        code, marker = suffix.group("code").upper(), suffix.group("suffix").upper()
        if marker == "HK":
            if not code.isdigit() or not 1 <= len(code) <= 5:
                raise CompanyDiscoveryInputError("hk_code_invalid")
            return {"ticker": f"{code.zfill(4)}.HK", "market": MARKET_HK,
                    "ambiguous_market": False, "raw": text}
        if marker in {"SZ"}:
            if not (len(code) == 6 and code.isdigit()):
                raise CompanyDiscoveryInputError("a_code_invalid")
            return {"ticker": f"{code}.SZ", "market": MARKET_A, "ambiguous_market": False, "raw": text}
        if marker in {"SS", "SH"}:
            if not (len(code) == 6 and code.isdigit()):
                raise CompanyDiscoveryInputError("a_code_invalid")
            return {"ticker": f"{code}.SS", "market": MARKET_A, "ambiguous_market": False, "raw": text}
        if marker in {"US", "O", "N", "H"}:
            if not _BARE_US.fullmatch(code):
                raise CompanyDiscoveryInputError("us_code_invalid")
            return {"ticker": code, "market": MARKET_US, "ambiguous_market": False, "raw": text}
        raise CompanyDiscoveryInputError("ticker_suffix_unsupported")
    upper = text.upper()
    if _BARE_HK.fullmatch(upper):
        return {"ticker": f"{upper.zfill(4)}.HK", "market": MARKET_HK,
                "ambiguous_market": False, "raw": text}
    if _BARE_A.fullmatch(upper):
        # 深/沪同名代码真实存在（000001.SZ 平安银行 vs 000001.SH 上证综指），不猜。
        return {"ticker": upper, "market": MARKET_A, "ambiguous_market": True, "raw": text}
    if _BARE_US.fullmatch(upper):
        return {"ticker": upper, "market": MARKET_US, "ambiguous_market": False, "raw": text}
    raise CompanyDiscoveryInputError("ticker_format_invalid")


def _hint_key(query: str) -> str | None:
    """按子串长度倒序匹配提示表，避免短别名抢先命中。"""
    folded = unicodedata.normalize("NFKC", query).strip().casefold()
    if not folded:
        return None
    compact = re.sub(r"[\s\-_]+", "", folded)
    for key in _HINT_KEYS_BY_LENGTH:
        if key in folded or key in compact:
            return key
    return None


def _clean_query(query: Any) -> str:
    if not isinstance(query, str):
        raise CompanyDiscoveryInputError("query_type_invalid")
    text = unicodedata.normalize("NFKC", query).strip()
    if not text or len(text) > MAX_QUERY_LENGTH:
        raise CompanyDiscoveryInputError("query_empty_or_too_long")
    if any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in text):
        raise CompanyDiscoveryInputError("query_control_character")
    if "://" in text:
        raise CompanyDiscoveryInputError("url_not_accepted")
    return text


# --- 候选与结果 -----------------------------------------------------------------


@dataclass(frozen=True)
class CompanyCandidate:
    """一个候选标的；``verified`` 仅在官方清单命中时为 True。"""

    ticker: str
    company: str
    market: str
    source: str
    confidence: float
    match_reason: str
    verified: bool
    provenance: Mapping[str, Any] = field(default_factory=dict)
    official_channel: str | None = None
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "market": self.market,
            "source": self.source,
            "sources": list(self.sources),
            "confidence": round(float(self.confidence), 3),
            "match_reason": self.match_reason,
            "verified": bool(self.verified),
            "provenance": dict(self.provenance),
            "official_channel": self.official_channel,
            "evidence_level": "official_manifest" if self.verified else "candidate",
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """公司候选解析结果；不含任何财务数值。"""

    status: str
    query: str
    selected_ticker: str | None
    candidates: tuple[CompanyCandidate, ...]
    source_observations: tuple[SourceObservation, ...]
    warnings: tuple[str, ...]
    requested_market: str | None
    error_code: str | None = None
    message: str = ""
    evidence_state: str = EVIDENCE_NOT_ONBOARDED
    onboarded_year: str | None = None
    external_source_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "selected_ticker": self.selected_ticker,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "source_observations": [observation.to_dict() for observation in self.source_observations],
            "warnings": list(self.warnings),
            "requested_market": self.requested_market,
            "error_code": self.error_code,
            "message": self.message,
            "evidence_state": self.evidence_state,
            "onboarded_year": self.onboarded_year,
            "external_source_used": bool(self.external_source_used),
            "answerable": bool(self.evidence_state == EVIDENCE_FIELD_VERIFIED),
        }

    def candidate_for(self, market: str | None = None) -> CompanyCandidate | None:
        for candidate in self.candidates:
            if market is None or candidate.market == market:
                return candidate
        return None


class DiscoveryRegistry(Protocol):
    """解析层只依赖这一个窄接口，便于注入 fake 与避免耦合具体适配器。"""

    def resolve_company(self, query: str, *, market: str | None = None) -> list[SourceObservation]:
        ...


# --- 证据状态 -------------------------------------------------------------------


def evidence_state_for(ticker: str, record: Mapping[str, Any] | None) -> tuple[str, str | None]:
    """返回 ``(evidence_state, onboarded_year)``，只依据本地清单与已核验锚点。"""
    if not record:
        return EVIDENCE_NOT_ONBOARDED, None
    year = record.get("year")
    try:
        anchors = company_qa._json(company_qa.ANCHORS_PATH)
    except CompanySourceUnavailable:
        return EVIDENCE_MATERIAL_ONLY, year
    spec = (anchors.get(ticker) or {}).get("revenue")
    entry = record.get("entry") or {}
    validation = entry.get("validation") or {}
    if (isinstance(spec, dict) and spec.get("year") == year
            and spec.get("source_sha256") == validation.get("sha256")
            and spec.get("file_name") == entry.get("file_name")):
        return EVIDENCE_FIELD_VERIFIED, year
    return EVIDENCE_MATERIAL_ONLY, year


# --- 候选聚合 -------------------------------------------------------------------


def _merge_candidate(pool: dict[str, CompanyCandidate], candidate: CompanyCandidate) -> None:
    """同一 ticker 合并多来源，但保留每个来源的 provenance 痕迹。"""
    existing = pool.get(candidate.ticker)
    if existing is None:
        pool[candidate.ticker] = candidate
        return
    sources = tuple(dict.fromkeys([*existing.sources, candidate.source]))
    # 外部来源的 provenance 优先于路由提示：提示只是导航线索，真实调用才是事实来源。
    provenance = {**dict(existing.provenance), **dict(candidate.provenance)}
    pool[candidate.ticker] = CompanyCandidate(
        ticker=existing.ticker,
        company=existing.company or candidate.company,
        market=existing.market,
        source=existing.source,
        confidence=max(existing.confidence, candidate.confidence),
        match_reason=existing.match_reason,
        # 已核验身份不可被候选降级，也不可被候选改名。
        verified=existing.verified or candidate.verified,
        provenance=provenance,
        official_channel=existing.official_channel or candidate.official_channel,
        sources=sources,
    )


def _observation_candidates(observation: SourceObservation, query: str, market: str | None) -> list[CompanyCandidate]:
    """把外部来源行转成候选；字段漂移的行直接丢弃而不是猜。"""
    if observation.status not in {STATUS_CANDIDATE, STATUS_SUCCESS} or not observation.data:
        return []
    rows: Iterable[Any] = observation.data if isinstance(observation.data, (list, tuple)) else []
    results: list[CompanyCandidate] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_ticker = row.get("ticker") or row.get("code") or row.get("symbol")
        name = row.get("company") or row.get("code_name") or row.get("name") or row.get("名称")
        if not raw_ticker or not name:
            continue
        try:
            normalized = normalize_ticker(str(raw_ticker))
        except CompanyDiscoveryInputError:
            continue
        row_market = str(row.get("market") or "").upper()
        resolved_market = normalized["market"]
        if row_market in SUPPORTED_MARKETS and row_market != resolved_market:
            # 上游市场标注与代码后缀冲突时保守丢弃，不做静默纠正。
            continue
        if normalized["ambiguous_market"]:
            continue
        results.append(CompanyCandidate(
            ticker=normalized["ticker"],
            company=str(name).strip(),
            market=resolved_market,
            source=observation.source,
            confidence=0.6,
            match_reason=f"{observation.source} 按公司名匹配返回候选代码",
            verified=False,
            provenance={**dict(observation.provenance), "retrieved_at": observation.retrieved_at,
                        "operation": observation.operation},
            sources=(observation.source,),
        ))
    return results


def _hint_candidates(query: str) -> list[CompanyCandidate]:
    key = _hint_key(query)
    if key is None:
        return []
    return [
        CompanyCandidate(
            ticker=entry["ticker"],
            # 展示名称必须来自命中的别名，而不是把整句问题回显成公司名。
            company=key,
            market=entry["market"],
            source="local_routing_hint",
            confidence=0.4,
            match_reason=f"内置路由提示命中别名 {key}（仅用于定位官方披露渠道）",
            verified=False,
            provenance=dict(_HINT_PROVENANCE),
            official_channel=entry.get("official_channel"),
            sources=("local_routing_hint",),
        )
        for entry in ROUTING_HINTS[key]
    ]


def _resolve_ambiguous_bare_code(
    normalized_ticker: Mapping[str, Any], records: Mapping[str, Any]
) -> list[CompanyCandidate]:
    """裸 6 位代码：先查本地清单，查不到再问路由提示，仍不确定就保持歧义。"""
    code = str(normalized_ticker["ticker"])
    found: list[CompanyCandidate] = []
    for suffix in ("SZ", "SS"):
        probe = f"{code}.{suffix}"
        record = records.get(probe)
        if record is not None:
            found.append(CompanyCandidate(
                ticker=probe, company=str(record.get("display_name") or probe), market=MARKET_A,
                source="local_manifest", confidence=1.0,
                match_reason="本地官方资料清单命中该证券代码",
                verified=True,
                provenance={"provider": "local_manifest", "endpoint": "company_qa.catalog",
                            "evidence_level": "official_manifest"},
                official_channel="CNINFO", sources=("local_manifest",),
            ))
    if found:
        return found
    for key in (code, f"{code}.sz", f"{code}.ss"):
        for entry in ROUTING_HINTS.get(key, ()):
            found.append(CompanyCandidate(
                ticker=entry["ticker"], company=code, market=entry["market"],
                source="local_routing_hint", confidence=0.3,
                match_reason=f"裸 {code} 无法区分交易所，内置提示指向该市场",
                verified=False, provenance=dict(_HINT_PROVENANCE),
                official_channel=entry.get("official_channel"), sources=("local_routing_hint",),
            ))
    return found


def _local_manifest_candidates(query: str, records: Mapping[str, Any]) -> list[CompanyCandidate]:
    """本地清单别名命中即已核验身份；不调用外部源。"""
    try:
        targets, _unknown = company_qa.references(query, dict(records))
    except CompanySourceUnavailable:
        return []
    candidates: list[CompanyCandidate] = []
    for ticker in sorted(targets):
        record = records.get(ticker) or {}
        candidates.append(CompanyCandidate(
            ticker=ticker,
            company=str(record.get("display_name") or ticker),
            market=_market_of(ticker),
            source="local_manifest",
            confidence=1.0,
            match_reason="本地官方资料清单命中公司别名",
            verified=True,
            provenance={"provider": "local_manifest", "endpoint": "company_qa.catalog",
                        "evidence_level": "official_manifest"},
            official_channel=_official_channel(ticker),
            sources=("local_manifest",),
        ))
    return candidates


def _market_of(ticker: str) -> str:
    if str(ticker).upper().endswith(".HK"):
        return MARKET_HK
    if re.fullmatch(r"\d{6}\.(?:SZ|SS)", str(ticker)):
        return MARKET_A
    return MARKET_US


_OFFICIAL_CHANNELS = {"HK": "HKEX", "A": "CNINFO", "US": "SEC"}


def _official_channel(ticker: str) -> str:
    return _OFFICIAL_CHANNELS.get(_market_of(ticker), "SEC")


def _markets_to_probe(
    requested: str | None, registry: DiscoveryRegistry | None, hints: Sequence[CompanyCandidate]
) -> list[str]:
    if requested:
        return [requested]
    hinted = [candidate.market for candidate in hints]
    if hinted:
        return list(dict.fromkeys(hinted))
    if registry is None:
        return []
    supported: set[str] = set()
    for name in getattr(registry, "adapters", {}):
        supported |= SOURCE_MARKETS.get(name, frozenset({MARKET_A}))
    return sorted(supported)


def _probe_registry(
    query: str, registry: DiscoveryRegistry, markets: Sequence[str]
) -> tuple[list[SourceObservation], bool]:
    """按市场逐个调用 registry；记录真实失败，不吞异常也不混用别家公司缓存。"""
    observations: list[SourceObservation] = []
    used = False
    for market in markets:
        try:
            batch = registry.resolve_company(query, market=market)
        except Exception as exc:  # noqa: BLE001 - 外部边界统一转为可审计失败
            from .market_data_sources import _error_observation
            from .source_governance import SourceErrorCode

            observations.append(_error_observation(
                getattr(registry, "name", "registry"), "resolve_company",
                SourceErrorCode.UNKNOWN, type(exc).__name__,
            ))
            continue
        used = used or bool(batch)
        observations.extend(item for item in batch if isinstance(item, SourceObservation))
    return observations, used


# --- 主入口 ---------------------------------------------------------------------


def resolve_company_candidates(
    query: str,
    *,
    market: str | None = None,
    registry: MarketDataSourceRegistry | DiscoveryRegistry | None = None,
    allow_external: bool = True,
) -> DiscoveryResult:
    """解析公司候选；本地清单优先，外部源只用于未收录公司的候选发现。

    参数
    ----
    query
        公司中文名、英文名或证券代码。不接受 URL。
    market
        可选市场限定（``A`` / ``HK`` / ``US`` 及常见别名）。
    registry
        外部候选源；``None`` 时完全离线，只用本地清单与路由提示。
    allow_external
        显式关闭外部源（等价于 ``registry=None``），供严格离线场景使用。
    """
    text = _clean_query(query)
    requested_market = normalize_market(market)
    use_registry = registry if (allow_external and registry is not None) else None

    try:
        records = company_qa.catalog()
        catalog_error = None
    except CompanySourceUnavailable as exc:
        records, catalog_error = {}, str(exc)

    # 1) 本地官方清单优先：命中即返回 onboarded，绝不触发外部源。
    local = _local_manifest_candidates(text, records)
    if local:
        in_market = [c for c in local if requested_market is None or c.market == requested_market]
        pool: dict[str, CompanyCandidate] = {}
        for candidate in in_market:
            _merge_candidate(pool, candidate)
        ordered = tuple(pool[ticker] for ticker in sorted(pool))
        single = ordered[0] if len(ordered) == 1 else None
        state, year = evidence_state_for(single.ticker, records.get(single.ticker)) if single else (EVIDENCE_NOT_ONBOARDED, None)
        warnings: list[str] = []
        if len(ordered) > 1:
            warnings.append(WARNING_CROSS_TICKER)
        if requested_market is not None and not in_market:
            warnings.append(WARNING_NO_MATCH_IN_REQUESTED_MARKET)
        if state == EVIDENCE_MATERIAL_ONLY:
            warnings.append(WARNING_MATERIAL_ONLY)
        elif state == EVIDENCE_NOT_ONBOARDED:
            warnings.append(WARNING_FIELD_GAP)
        return DiscoveryResult(
            status=STATUS_ONBOARDED if single else STATUS_AMBIGUOUS,
            query=text, selected_ticker=single.ticker if single else None,
            candidates=ordered, source_observations=(), warnings=tuple(warnings),
            requested_market=requested_market,
            error_code=None if single else ERROR_COMPANY_AMBIGUOUS_MARKET,
            message=("已收录公司，直接使用本地官方资料。" if single
                     else "本地清单命中多个标的，请明确证券代码。"),
            evidence_state=state, onboarded_year=year, external_source_used=False,
        )

    # 2) 未收录：先做代码规范化（可离线给出已核验或候选）。
    warnings = []
    hint_pool: dict[str, CompanyCandidate] = {}
    for candidate in _hint_candidates(text):
        _merge_candidate(hint_pool, candidate)
    try:
        parsed = normalize_ticker(text)
    except CompanyDiscoveryInputError:
        parsed = None

    if parsed is not None and (parsed["ticker"] in records or parsed["ambiguous_market"]):
        if parsed["ambiguous_market"]:
            for candidate in _resolve_ambiguous_bare_code(parsed, records):
                _merge_candidate(hint_pool, candidate)
            if not any(c.ticker in records for c in hint_pool.values()):
                warnings.append(WARNING_CROSS_MARKET)
        else:
            record = records.get(parsed["ticker"])
            if record is not None:
                _merge_candidate(hint_pool, CompanyCandidate(
                    ticker=parsed["ticker"], company=str(record.get("display_name") or parsed["ticker"]),
                    market=parsed["market"], source="local_manifest", confidence=1.0,
                    match_reason="本地官方资料清单命中证券代码", verified=True,
                    provenance={"provider": "local_manifest", "endpoint": "company_qa.catalog",
                                "evidence_level": "official_manifest"},
                    official_channel=_official_channel(parsed["ticker"]), sources=("local_manifest",),
                ))

    # 3) 外部候选发现（仅未收录公司，且仅在注入 registry 时）。
    observations: list[SourceObservation] = []
    external_used = False
    hint_list = list(hint_pool.values())
    if use_registry is not None and not any(c.verified for c in hint_list):
        markets = _markets_to_probe(requested_market, use_registry, hint_list)
        if markets:
            observations, external_used = _probe_registry(text, use_registry, markets)
            for observation in observations:
                for candidate in _observation_candidates(observation, text, requested_market):
                    _merge_candidate(hint_pool, candidate)

    pool = {ticker: candidate for ticker, candidate in hint_pool.items()
            if requested_market is None or candidate.market == requested_market}
    if requested_market is not None and not pool and hint_pool:
        warnings.append(WARNING_NO_MATCH_IN_REQUESTED_MARKET)
    ordered = tuple(pool[ticker] for ticker in sorted(pool, key=lambda t: (-pool[t].confidence, t)))

    failed = [o for o in observations if o.status == STATUS_UNAVAILABLE]
    empty = [o for o in observations if o.status == STATUS_EMPTY]
    if failed:
        warnings.append(WARNING_SOURCE_FAILED)
    if any(candidate.source != "local_manifest" for candidate in ordered):
        warnings.append(WARNING_CANDIDATE_NOT_EVIDENCE)
    if any(candidate.source == "local_routing_hint" for candidate in ordered):
        warnings.append(WARNING_ROUTING_HINT)
    verified = [c for c in ordered if c.verified]
    if verified:
        state, year = evidence_state_for(verified[0].ticker, records.get(verified[0].ticker))
        return DiscoveryResult(
            status=STATUS_ONBOARDED, query=text, selected_ticker=verified[0].ticker,
            candidates=ordered, source_observations=tuple(observations), warnings=tuple(warnings),
            requested_market=requested_market, error_code=None,
            message="证券代码命中本地官方资料清单。", evidence_state=state,
            onboarded_year=year, external_source_used=external_used,
        )

    if not ordered:
        # 源可达但没有匹配 → 公司确实没找到；源不可达 → 数据源问题。两者不能混为一谈。
        status = STATUS_SOURCE_UNAVAILABLE if failed else STATUS_UNRESOLVED
        code = ERROR_SOURCE_UNAVAILABLE if failed else ERROR_COMPANY_NOT_FOUND
        if requested_market is not None and hint_pool:
            market_label = {MARKET_HK: "港股", MARKET_A: "A股", MARKET_US: "美股"}.get(requested_market, requested_market)
            available_labels = " / ".join({MARKET_HK: "港股", MARKET_A: "A股", MARKET_US: "美股"}.get(c.market, c.market) for c in hint_pool.values())
            status = STATUS_AMBIGUOUS
            code = ERROR_COMPANY_AMBIGUOUS_MARKET
            message = f"已识别到该公司，但没有{market_label}候选；可选市场为 {available_labels}。"
        else:
            message = ("数据源暂时不可用，无法确认该公司候选；未使用其他公司的资料替代。"
                       if failed else
                       ("外部数据源已响应但没有该公司记录；未使用其他公司的资料替代。"
                        if empty else
                        "未识别到该公司。请提供公司全称或证券代码，并说明港股 / A 股 / 美股。"))
        if catalog_error:
            code = ERROR_CATALOG_UNAVAILABLE
        return DiscoveryResult(
            status=status, query=text, selected_ticker=None, candidates=(),
            source_observations=tuple(observations), warnings=tuple(warnings),
            requested_market=requested_market, error_code=code, message=message,
            evidence_state=EVIDENCE_NOT_ONBOARDED, onboarded_year=None,
            external_source_used=external_used,
        )

    markets = {candidate.market for candidate in ordered}
    tickers = {candidate.ticker for candidate in ordered}
    if len(markets) > 1 or len(tickers) > 1:
        warnings.append(WARNING_CROSS_MARKET if len(markets) > 1 else WARNING_CROSS_TICKER)
        return DiscoveryResult(
            status=STATUS_AMBIGUOUS, query=text, selected_ticker=None, candidates=ordered,
            source_observations=tuple(observations), warnings=tuple(warnings),
            requested_market=requested_market, error_code=ERROR_COMPANY_AMBIGUOUS_MARKET,
            message=("该公司存在多个市场或多个证券代码候选，请确认港股 / A 股 / 美股；"
                     "系统不会自行选择，也不会跨市场混用数据。"),
            evidence_state=EVIDENCE_NOT_ONBOARDED, onboarded_year=None,
            external_source_used=external_used,
        )

    return DiscoveryResult(
        status=STATUS_MATCHED, query=text, selected_ticker=ordered[0].ticker, candidates=ordered,
        source_observations=tuple(observations), warnings=tuple(warnings),
        requested_market=requested_market, error_code=ERROR_OFFICIAL_MATERIAL_REQUIRED,
        message=("已识别候选公司，但官方年报证据尚未接入；不能据此回答收入等财务数字。"),
        evidence_state=EVIDENCE_NOT_ONBOARDED, onboarded_year=None,
        external_source_used=external_used,
    )


def default_discovery_registry() -> MarketDataSourceRegistry:
    """默认候选源注册表（懒加载，不导入也不登录外部依赖）。"""
    return default_registry()


def explain_unresolved_company(query: str, *, market: str | None = None) -> DiscoveryResult:
    """聊天层的严格离线解释入口：只读本地清单与路由提示，绝不联网。"""
    return resolve_company_candidates(query, market=market, registry=None, allow_external=False)
