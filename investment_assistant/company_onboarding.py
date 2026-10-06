"""按预审官方快照接入资料；下载与问答证据锚点是两个独立审核步骤。

闭环边界
--------
``request_onboarding`` → ``provision_approved`` → ``register_field_anchor``

* 只有预审清单（固定 URL + SHA256 + 页数）中的官方来源才允许触发网络；
* 任何调用方传入的 URL 都被拒绝，抓取只认白名单主机与固定路径；
* 下载或校验失败**不改变** ``materials_manifest.json`` 与 ``onboarded_materials.json``；
* 入库成功只代表 ``onboarded_material_only``：还没有字段证据锚点，不能回答财务数字；
* 只有 :func:`register_field_anchor` 通过后状态才允许进入 ``verified_field_available``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from .company_qa import MANIFEST_PATH, ONBOARDED_PATH, CompanySourceUnavailable, _json
from .config import DATA_DIR, KNOWLEDGE_DIR

APPROVAL_PATH = DATA_DIR / "company_onboarding_allowlist.json"
REQUESTS_PATH = DATA_DIR / "company_material_requests.json"
ANCHORS_PATH = DATA_DIR / "financial_fact_anchors.json"
MAX_PDF_BYTES = 25 * 1024 * 1024

STATE_MATERIAL_ONLY = "onboarded_material_only"
STATE_FIELD_VERIFIED = "verified_field_available"
STATE_NOT_ONBOARDED = "not_onboarded"

#: 官方来源白名单：港股优先 HKEX、美股优先 SEC、A 股优先 CNINFO。
OFFICIAL_HOSTS: dict[str, str] = {
    "CNINFO": "static.cninfo.com.cn",
    "HKEX": "www1.hkexnews.hk",
    "SEC": "www.sec.gov",
}
URL_PATTERNS: dict[str, re.Pattern[str]] = {
    "CNINFO": re.compile(r"https://static\.cninfo\.com\.cn/finalpage/20\d{2}-\d{2}-\d{2}/\d+\.PDF"),
    "HKEX": re.compile(r"https://www1\.hkexnews\.hk/listedco/listconews/sehk/20\d{2}/\d{4}/\d+\.pdf"),
    "SEC": re.compile(r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+[\w./-]*\.htm"),
}

#: 官方渠道与市场的对应关系，用于把候选市场映射到唯一权威披露渠道。
MARKET_OFFICIAL_CHANNEL: dict[str, str] = {"HK": "HKEX", "US": "SEC", "A": "CNINFO"}


def market_for_ticker(ticker: str) -> str:
    """从证券代码推断市场；推断不出时抛错，不猜。"""
    code = str(ticker or "").strip().upper()
    if re.fullmatch(r"\d{4,5}\.HK", code):
        return "HK"
    if re.fullmatch(r"\d{6}\.(?:SZ|SS)", code):
        return "A"
    if re.fullmatch(r"[A-Z]{1,5}", code):
        return "US"
    raise ValueError("ticker_format_invalid")


def official_channel_for(ticker: str) -> str:
    return MARKET_OFFICIAL_CHANNEL[market_for_ticker(ticker)]


def _validate_approval_entry(code: str, year: str, approved: dict) -> None:
    """预审项必须逐字段合法；任何一项不满足都 fail-closed。"""
    market = approved.get("market")
    url, expected_sha, pages = approved.get("source_url"), approved.get("sha256"), approved.get("page_count")
    if (market not in URL_PATTERNS
            or not isinstance(url, str) or not URL_PATTERNS[market].fullmatch(url)
            or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha))
            or type(pages) is not int or pages < 1 or pages > 1500
            or approved.get("report_date") != year or approved.get("approved") is not True
            or not isinstance(approved.get("company"), str) or not 1 <= len(approved["company"]) <= 80):
        raise CompanySourceUnavailable("approval_invalid")
    expected_channel = official_channel_for(code)
    if market != expected_channel:
        # 市场与官方渠道错配意味着预审项本身写错，不接受"另一个官方站点"顶替。
        raise CompanySourceUnavailable("approval_market_channel_mismatch")
    parsed = urlparse(url)
    if parsed.username or parsed.query or parsed.fragment or parsed.hostname != OFFICIAL_HOSTS[market]:
        raise CompanySourceUnavailable("approval_url_invalid")


def provision_approved(ticker: str, year: str, *, session: requests.Session | None = None) -> dict[str, object]:
    """仅固定 URL/摘要的预审清单可触发网络；失败不变更正式 manifest。"""
    code = str(ticker).strip().upper()
    year = str(year).strip()
    if not re.fullmatch(r"\d{6}\.(?:SZ|SS)|\d{4,5}\.HK|[A-Z]{1,5}", code) or not re.fullmatch(r"20\d{2}", year):
        raise ValueError("ticker_or_year_invalid")
    manifest = {**_json(MANIFEST_PATH), **_json(ONBOARDED_PATH)}
    if code in manifest:
        return {"status": "already_onboarded", "ticker": code, "year": str(manifest[code].get("report_date")),
                "evidence_state": _state_for(code, manifest[code]),
                "message": "该公司已有资料；不覆盖现存版本。"}
    approved = _json(APPROVAL_PATH).get(f"{code}:{year}")
    if not isinstance(approved, dict):
        return {"status": "not_approved", "ticker": code, "year": year, "evidence_state": STATE_NOT_ONBOARDED,
                "official_channel": None,
                "message": "尚无该市场与年度经预审的官方来源 URL、文件 SHA256、页数；没有启动任意网页抓取。"}
    _validate_approval_entry(code, year, approved)
    url, expected_sha, pages = approved["source_url"], approved["sha256"], approved["page_count"]
    is_html = approved["market"] == "SEC"
    client = session or requests.Session()
    suffix = ".htm" if is_html else ".pdf"
    destination = KNOWLEDGE_DIR / f"approved_{code.replace('.', '_')}_{year}_{expected_sha[:12]}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with client.get(url, stream=True, timeout=(5, 30), allow_redirects=False) as response:
            # PDF 端点收到 HTML 通常是反爬/错误页；SEC 官方归档本身就是 HTML，需分别处理。
            content_type = response.headers.get("Content-Type", "").lower()
            if response.status_code != 200 or (not is_html and content_type.startswith("text/html")):
                raise CompanySourceUnavailable("official_download_failed")
            length = 0
            digest = hashlib.sha256()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    length += len(chunk)
                    if length > MAX_PDF_BYTES:
                        raise CompanySourceUnavailable("pdf_too_large")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if length < 1000 or digest.hexdigest() != expected_sha:
            raise CompanySourceUnavailable("approved_sha256_mismatch")
        text_layer = _validate_payload(temporary, approved, pages, is_html)
        # 再读一次审批与清单，防止等待网络期间审批撤销或另一请求覆盖。
        if _json(APPROVAL_PATH).get(f"{code}:{year}") != approved or code in _json(MANIFEST_PATH) or code in _json(ONBOARDED_PATH):
            raise CompanySourceUnavailable("approval_or_manifest_changed")
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha:
                raise CompanySourceUnavailable("destination_collision")
            temporary.unlink()
        else:
            temporary.replace(destination)
        current = _json(ONBOARDED_PATH)
        if code in current or code in _json(MANIFEST_PATH):
            raise CompanySourceUnavailable("manifest_changed")
        current[code] = {"ticker": code, "source": approved["market"], "source_url": url, "report_date": year,
                         "page_authority": "official", "material_kind": "official_filing", "company": approved["company"],
                         "filing_form": "annual_report", "file_name": destination.name,
                         "onboarded_at": datetime.now(UTC).isoformat(),
                         "validation": {"sha256": expected_sha, "page_count": pages, "file_size_bytes": length,
                                        "text_layer_verified": bool(text_layer)}}
        manifest_tmp = ONBOARDED_PATH.with_suffix(".json.part")
        manifest_tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(manifest_tmp, ONBOARDED_PATH)
        return {"status": "material_onboarded", "ticker": code, "year": year,
                "evidence_state": STATE_MATERIAL_ONLY,
                "official_channel": approved["market"],
                "next_step": "field_anchor_required",
                "message": "官方文件已按预审哈希入库；尚无字段证据锚点，财务问答将继续拒答，待人工核验。"}
    except (requests.RequestException, OSError, ValueError) as exc:
        raise CompanySourceUnavailable("approved_download_unavailable") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validate_payload(path: Path, approved: dict, pages: int, is_html: bool) -> str:
    """校验文件真实格式、页数与文本层；任一不符即拒绝入库。"""
    with path.open("rb") as handle:
        magic = handle.read(5)
    if is_html:
        if not magic.lower().startswith((b"<!doc", b"<html", b"<?xml")):
            raise CompanySourceUnavailable("not_official_filing_html")
        body = path.read_bytes().decode("utf-8", errors="replace")
        if approved["company"].casefold() not in body.casefold() or not body.strip():
            raise CompanySourceUnavailable("filing_text_layer_invalid")
        return body
    if magic != b"%PDF-":
        raise CompanySourceUnavailable("not_pdf")
    try:
        reader = PdfReader(str(path))
        lead = " ".join((page.extract_text() or "") for page in reader.pages[:min(pages, 3)])
        page_count = len(reader.pages)
    except CompanySourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏 PDF 的解析异常也必须转为受控拒绝
        raise CompanySourceUnavailable("pdf_unreadable") from exc
    if page_count != pages or not lead.strip():
        raise CompanySourceUnavailable("pdf_page_or_text_invalid")
    if approved["market"] == "HKEX" and approved["company"].casefold() not in lead.casefold():
        raise CompanySourceUnavailable("pdf_lead_page_company_mismatch")
    return lead


def _state_for(ticker: str, entry: dict) -> str:
    """依据已登记资料与字段锚点判断证据状态。"""
    try:
        anchors = _json(ANCHORS_PATH)
    except CompanySourceUnavailable:
        return STATE_MATERIAL_ONLY
    spec = (anchors.get(ticker) or {}).get("revenue")
    validation = entry.get("validation") or {}
    if (isinstance(spec, dict) and spec.get("file_name") == entry.get("file_name")
            and spec.get("source_sha256") == validation.get("sha256")):
        return STATE_FIELD_VERIFIED
    return STATE_MATERIAL_ONLY


# --- 接入申请（人工审批待办）------------------------------------------------------


def request_onboarding(ticker: str, year: str, *, requested_by: str, market: str | None = None) -> dict[str, object]:
    """登记一条官方资料接入申请；**不联网、不写正式清单**。

    返回结构化待办：需要的官方渠道、还缺哪些预审字段，便于人工补齐白名单。
    """
    code = str(ticker or "").strip().upper()
    year = str(year or "").strip()
    if not re.fullmatch(r"\d{6}\.(?:SZ|SS)|\d{4,5}\.HK|[A-Z]{1,5}", code) or not re.fullmatch(r"20\d{2}", year):
        raise ValueError("ticker_or_year_invalid")
    actor = str(requested_by or "").strip()[:80]
    if not actor:
        raise ValueError("requested_by_required")
    try:
        resolved_market = market_for_ticker(code)
    except ValueError as exc:
        raise ValueError("ticker_format_invalid") from exc
    if market is not None and str(market).strip().upper() not in {"", resolved_market}:
        raise ValueError("market_conflicts_with_ticker")
    channel = MARKET_OFFICIAL_CHANNEL[resolved_market]

    manifest = {**_json(MANIFEST_PATH), **_json(ONBOARDED_PATH)}
    if code in manifest:
        return {"status": "already_onboarded", "ticker": code, "year": str(manifest[code].get("report_date")),
                "evidence_state": _state_for(code, manifest[code]),
                "message": "该公司已有正式资料；不会重复申请或覆盖。"}
    approved = _json(APPROVAL_PATH).get(f"{code}:{year}")
    if isinstance(approved, dict) and approved.get("approved") is True:
        try:
            _validate_approval_entry(code, year, approved)
        except CompanySourceUnavailable as exc:
            approved = None
            approval_error = str(exc)
        else:
            approval_error = None
    else:
        approval_error = "not_preapproved"

    pending = _json(REQUESTS_PATH) if REQUESTS_PATH.exists() else {}
    key = f"{code}:{year}"
    record = {
        "ticker": code, "year": year, "market": resolved_market, "official_channel": channel,
        "requested_by": actor, "requested_at": datetime.now(UTC).isoformat(),
        "status": "approved_pending_download" if isinstance(approved, dict) else "awaiting_preapproval",
        "evidence_state": STATE_NOT_ONBOARDED,
        "missing_fields": ([] if isinstance(approved, dict)
                           else ["approved", "market", "company", "report_date", "source_url", "sha256", "page_count"]),
        "approval_error": approval_error,
        "network_started": False,
    }
    pending[key] = record
    _write_json(REQUESTS_PATH, pending)
    return {"status": record["status"], **record,
            "message": ("已登记接入申请并命中预审白名单，可由受控服务执行下载。"
                        if isinstance(approved, dict) else
                        f"已登记接入申请，待人工把 {channel} 官方文件的 URL、SHA256 与页数写入预审白名单；本次未联网。")}


def pending_requests() -> list[dict[str, object]]:
    """列出待人工审批的接入申请（只读）。"""
    if not REQUESTS_PATH.exists():
        return []
    pending = _json(REQUESTS_PATH)
    return [pending[key] for key in sorted(pending)
            if isinstance(pending.get(key), dict) and pending[key].get("status") != "completed"]


# --- 字段证据锚点（回答财务数字的唯一前置）-----------------------------------------

_ANCHOR_TOKENS = ("table", "section", "first_component", "last_component", "total_note", "next_section")


def register_field_anchor(
    ticker: str, field: str, spec: dict[str, object], *, approved_by: str, expected_sha256: str
) -> dict[str, object]:
    """登记字段证据锚点；必须与当前已入库资料的字节、页数与年度完全一致。

    锚点只描述"哪个文件哪一页的哪一行"，不写业务规则；回答层仍会重新校验。
    """
    code = str(ticker or "").strip().upper()
    actor = str(approved_by or "").strip()[:80]
    if not actor:
        raise ValueError("approver_required")
    if not re.fullmatch(r"\d{6}\.(?:SZ|SS)|\d{4,5}\.HK|[A-Z]{1,5}", code):
        raise ValueError("ticker_format_invalid")
    if field != "revenue":
        raise CompanySourceUnavailable("anchor_field_unsupported")
    entry = _json(ONBOARDED_PATH).get(code) or _json(MANIFEST_PATH).get(code)
    if not isinstance(entry, dict):
        raise CompanySourceUnavailable("anchor_material_missing")
    validation = entry.get("validation") or {}
    sha = validation.get("sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(sha)) or str(sha) != str(expected_sha256 or "").lower():
        raise CompanySourceUnavailable("anchor_sha_mismatch")
    if not isinstance(spec, dict):
        raise CompanySourceUnavailable("anchor_spec_invalid")
    year = _report_year(entry)
    if spec.get("year") != year or spec.get("file_name") != entry.get("file_name"):
        raise CompanySourceUnavailable("anchor_identity_drift")
    if spec.get("source_url") != entry.get("source_url") or spec.get("page_authority") != entry.get("page_authority"):
        raise CompanySourceUnavailable("anchor_identity_drift")
    page = spec.get("page")
    if type(page) is not int or not 1 <= page <= int(validation.get("page_count") or 0):
        raise CompanySourceUnavailable("anchor_page_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("source_sha256"))) or spec.get("source_sha256") != sha:
        raise CompanySourceUnavailable("anchor_sha_mismatch")
    for key in ("value", "comparative"):
        if not re.fullmatch(r"[1-9][\d,]*", str(spec.get(key))):
            raise CompanySourceUnavailable("anchor_value_invalid")
    # 币种与单位必须是非空字符串；空串会让回答层"看起来有单位"实则没有。
    for key in ("currency", "unit"):
        if not isinstance(spec.get(key), str) or not str(spec[key]).strip():
            raise CompanySourceUnavailable("anchor_unit_invalid")
    engine = "chinese" if str(spec.get("table") or "").startswith("2.") else "anchored"
    if engine == "anchored" and any(not isinstance(spec.get(key), str) or not spec[key] for key in _ANCHOR_TOKENS):
        raise CompanySourceUnavailable("anchor_tokens_missing")

    # 锚点登记前重读真实文件字节，防止登记时资料已被替换。
    path = KNOWLEDGE_DIR / str(entry["file_name"])
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CompanySourceUnavailable("material_unavailable") from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise CompanySourceUnavailable("material_sha256_drift")

    anchors = _json(ANCHORS_PATH) if ANCHORS_PATH.exists() else {}
    anchors.setdefault(code, {})["revenue"] = {
        **spec, "approved_by": actor, "approved_at": datetime.now(UTC).isoformat(),
    }
    _write_json(ANCHORS_PATH, anchors)
    return {"status": "anchor_registered", "ticker": code, "field": field, "year": year,
            "evidence_state": STATE_FIELD_VERIFIED,
            "message": "字段证据锚点已登记；回答前仍会按当前字节重新校验。"}


def _report_year(entry: dict) -> str | None:
    match = re.fullmatch(r"(20\d{2})(?:-\d{2}-\d{2})?", str(entry.get("report_date") or ""))
    return match.group(1) if match else None


def _write_json(path: Path, payload: dict) -> None:
    """原子写：先写临时文件再 replace，避免半截文件被后续读取当成正式数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def coverage_state(ticker: str) -> dict[str, object]:
    """返回该标的的资料覆盖与证据状态，供 API / UI 展示"已识别/已核验/待接入"。"""
    code = str(ticker or "").strip().upper()
    try:
        manifest = {**_json(MANIFEST_PATH), **_json(ONBOARDED_PATH)}
    except CompanySourceUnavailable:
        return {"ticker": code, "material": "unavailable", "evidence_state": STATE_NOT_ONBOARDED}
    entry = manifest.get(code)
    if not isinstance(entry, dict):
        return {"ticker": code, "material": "not_onboarded", "evidence_state": STATE_NOT_ONBOARDED}
    return {"ticker": code, "material": "onboarded", "year": _report_year(entry),
            "official_channel": entry.get("source"),
            "source_url": entry.get("source_url"),
            "sha256": (entry.get("validation") or {}).get("sha256"),
            "evidence_state": _state_for(code, entry)}
