"""官方资料接入闭环的离线测试：白名单、校验、状态机与 fail-closed 行为。

全部使用本地 fixture，**不联网**；不写入仓库内的正式资料清单。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api, company_onboarding as onboarding
from investment_assistant.company_qa import CompanySourceUnavailable

TENCENT_PDF = Path("data/knowledge_base/0700HK_annual_report_2025.pdf")
TENCENT_SHA = "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98"
HKEX_URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.pdf"
CNINFO_URL = "https://static.cninfo.com.cn/finalpage/2025-04-25/1234567.PDF"
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1577552/0001193125-25-000001.htm"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把正式清单/预审/申请/锚点全部重定向到 tmp_path，测试不碰仓库数据。"""
    base = tmp_path / "base.json"
    attached = tmp_path / "onboarded.json"
    approval = tmp_path / "approval.json"
    requests_path = tmp_path / "requests.json"
    anchors = tmp_path / "anchors.json"
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    for path in (base, attached, approval, requests_path, anchors):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(onboarding, "MANIFEST_PATH", base)
    monkeypatch.setattr(onboarding, "ONBOARDED_PATH", attached)
    monkeypatch.setattr(onboarding, "APPROVAL_PATH", approval)
    monkeypatch.setattr(onboarding, "REQUESTS_PATH", requests_path)
    monkeypatch.setattr(onboarding, "ANCHORS_PATH", anchors)
    monkeypatch.setattr(onboarding, "KNOWLEDGE_DIR", pdf_dir)
    return {"base": base, "onboarded": attached, "approval": approval,
            "requests": requests_path, "anchors": anchors, "pdf": pdf_dir}


class FixtureSession:
    """按固定 URL 返回本地 fixture 字节；记录调用以证明是否真的发起过网络。"""

    def __init__(self, payload: bytes, status: int = 200, content_type: str = "application/pdf") -> None:
        self.payload, self.status, self.content_type = payload, status, content_type
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        session = self

        class Response:
            status_code = session.status
            headers = {"Content-Type": session.content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_content(self, chunk_size):
                for start in range(0, len(session.payload), chunk_size):
                    yield session.payload[start:start + chunk_size]

        return Response()


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("未预审或校验失败时不得发起网络请求")


def _approval_entry(**overrides) -> dict:
    entry = {"approved": True, "market": "HKEX", "company": "Tencent Holdings Limited",
             "report_date": "2025", "source_url": HKEX_URL, "sha256": "0" * 64, "page_count": 282}
    entry.update(overrides)
    return entry


# --- 官方来源白名单与市场映射 -----------------------------------------------------


@pytest.mark.parametrize("ticker, channel", [
    ("9988.HK", "HKEX"), ("0700.HK", "HKEX"),
    ("300750.SZ", "CNINFO"), ("600519.SS", "CNINFO"),
    ("BABA", "SEC"), ("AAPL", "SEC"),
])
def test_official_channel_matches_market(ticker, channel):
    assert onboarding.official_channel_for(ticker) == channel


def test_sec_and_cninfo_urls_are_whitelisted_patterns():
    assert onboarding.URL_PATTERNS["SEC"].fullmatch(SEC_URL)
    assert onboarding.URL_PATTERNS["CNINFO"].fullmatch(CNINFO_URL)
    assert not onboarding.URL_PATTERNS["HKEX"].fullmatch(SEC_URL)


@pytest.mark.parametrize("url", [
    "http://static.cninfo.com.cn/finalpage/2025-04-25/1234567.PDF",  # 非 https
    "https://evil.example.com/finalpage/2025-04-25/1234567.PDF",     # 非官方主机
    "https://static.cninfo.com.cn/finalpage/2025-04-25/1234567.pdf",  # 后缀不符
    "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.PDF",
])
def test_non_whitelisted_urls_are_rejected(sandbox, url):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(source_url=url),
    }), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match="approval_invalid"):
        onboarding.provision_approved("0700.HK", "2025", session=NoNetwork())


def test_approval_market_channel_mismatch_is_rejected(sandbox):
    """把 A 股代码配成 HKEX 来源（或反之）必须拒绝，不接受"另一个官方站点"顶替。"""
    sandbox["approval"].write_text(json.dumps({
        "300750.SZ:2025": _approval_entry(market="HKEX", source_url=HKEX_URL, company="宁德时代"),
    }), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match="approval_market_channel_mismatch"):
        onboarding.provision_approved("300750.SZ", "2025", session=NoNetwork())


# --- 未预审：绝不联网 -------------------------------------------------------------


def test_unapproved_ticker_never_opens_network(sandbox):
    result = onboarding.provision_approved("9988.HK", "2025", session=NoNetwork())
    assert result["status"] == "not_approved"
    assert result["evidence_state"] == onboarding.STATE_NOT_ONBOARDED
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}


def test_approval_with_query_string_or_credentials_is_rejected(sandbox):
    for bad in (HKEX_URL + "?token=abc", HKEX_URL.replace("https://", "https://user@")):
        sandbox["approval"].write_text(json.dumps({
            "0700.HK:2025": _approval_entry(source_url=bad),
        }), encoding="utf-8")
        with pytest.raises(CompanySourceUnavailable):
            onboarding.provision_approved("0700.HK", "2025", session=NoNetwork())


# --- 校验失败：fail-closed 且不改清单 ---------------------------------------------


def test_sha_mismatch_leaves_every_manifest_untouched(sandbox):
    sandbox["approval"].write_text(json.dumps({"0700.HK:2025": _approval_entry()}), encoding="utf-8")
    session = FixtureSession(TENCENT_PDF.read_bytes())
    with pytest.raises(CompanySourceUnavailable, match="approved_sha256_mismatch"):
        onboarding.provision_approved("0700.HK", "2025", session=session)
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}
    assert list(sandbox["pdf"].iterdir()) == [], "失败时不得留下半截文件"


def test_page_count_drift_is_rejected(sandbox):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=TENCENT_SHA, page_count=999),
    }), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match="approved_sha256_mismatch|page_or_text_invalid"):
        onboarding.provision_approved("0700.HK", "2025", session=FixtureSession(TENCENT_PDF.read_bytes()))
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}


def test_report_year_drift_is_rejected(sandbox):
    """预审项的 report_date 与 key 中的年度不一致时必须拒绝，不允许"借年份"下载。"""
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2024": _approval_entry(sha256=TENCENT_SHA, report_date="2025"),
    }), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match="approval_invalid"):
        onboarding.provision_approved("0700.HK", "2024", session=NoNetwork())
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("forged, expected", [
    (b"NOT-A-PDF" + b"0" * 2000, "not_pdf"),                      # 魔术字节不对
    (b"%PDF-forged" + b"0" * 2000, "pdf_unreadable"),             # 头对但内容损坏
])
def test_corrupt_pdf_payload_is_rejected(sandbox, forged, expected):
    sha = hashlib.sha256(forged).hexdigest()
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=sha),
    }), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match=expected):
        onboarding.provision_approved("0700.HK", "2025", session=FixtureSession(forged))
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}
    assert list(sandbox["pdf"].iterdir()) == []


def test_html_response_is_rejected_for_pdf_endpoint(sandbox):
    sandbox["approval"].write_text(json.dumps({"0700.HK:2025": _approval_entry(sha256=TENCENT_SHA)}), encoding="utf-8")
    with pytest.raises(CompanySourceUnavailable, match="official_download_failed"):
        onboarding.provision_approved("0700.HK", "2025",
                                      session=FixtureSession(b"<html>error</html>" * 100, content_type="text/html"))
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}


def test_download_never_follows_redirects_and_pins_the_whitelisted_url(sandbox):
    """即使服务器愿意重定向，也必须停在预审白名单的那一个 URL 上。"""
    sandbox["approval"].write_text(json.dumps({"0700.HK:2025": _approval_entry(sha256=TENCENT_SHA)}), encoding="utf-8")
    session = FixtureSession(TENCENT_PDF.read_bytes())
    onboarding.provision_approved("0700.HK", "2025", session=session)
    assert session.calls == [(HKEX_URL, session.calls[0][1])]
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["timeout"] == (5, 30)


# --- 成功入库：material_only，不能回答数字 -----------------------------------------


def test_successful_onboarding_yields_material_only_state(sandbox):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=TENCENT_SHA),
    }), encoding="utf-8")
    result = onboarding.provision_approved("0700.HK", "2025", session=FixtureSession(TENCENT_PDF.read_bytes()))
    assert result["status"] == "material_onboarded"
    # 入库 ≠ 可回答：状态必须是 material_only。
    assert result["evidence_state"] == onboarding.STATE_MATERIAL_ONLY
    assert result["next_step"] == "field_anchor_required"
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}, "不得改正式资料清单"
    entry = json.loads(sandbox["onboarded"].read_text(encoding="utf-8"))["0700.HK"]
    assert entry["validation"]["sha256"] == TENCENT_SHA
    assert entry["validation"]["text_layer_verified"] is True
    assert entry["page_authority"] == "official"
    assert onboarding.provision_approved("0700.HK", "2025", session=NoNetwork())["status"] == "already_onboarded"
    assert onboarding.coverage_state("0700.HK")["evidence_state"] == onboarding.STATE_MATERIAL_ONLY


def test_sec_html_official_filing_is_supported(sandbox):
    body = ("<html><body>Alibaba Group Holding Limited Annual Report on Form 20-F "
            "for the fiscal year ended March 31, 2025</body></html>").encode("utf-8") + b" " * 1200
    sha = hashlib.sha256(body).hexdigest()
    sandbox["approval"].write_text(json.dumps({
        "BABA:2025": {"approved": True, "market": "SEC", "company": "Alibaba Group Holding Limited",
                      "report_date": "2025", "source_url": SEC_URL, "sha256": sha, "page_count": 1},
    }), encoding="utf-8")
    result = onboarding.provision_approved("BABA", "2025", session=FixtureSession(
        body, content_type="text/html"))
    assert result["status"] == "material_onboarded"
    assert result["official_channel"] == "SEC"
    entry = json.loads(sandbox["onboarded"].read_text(encoding="utf-8"))["BABA"]
    assert entry["file_name"].endswith(".htm")


# --- 字段锚点：唯一能进入 verified_field_available 的路径 ----------------------------


def test_field_anchor_requires_matching_material_and_approver(sandbox):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=TENCENT_SHA),
    }), encoding="utf-8")
    onboarding.provision_approved("0700.HK", "2025", session=FixtureSession(TENCENT_PDF.read_bytes()))
    entry = json.loads(sandbox["onboarded"].read_text(encoding="utf-8"))["0700.HK"]
    spec = {
        "year": "2025", "file_name": entry["file_name"], "source_sha256": TENCENT_SHA,
        "source_url": entry["source_url"], "page_authority": "official", "page": 130,
        "value": "751,766", "comparative": "660,257", "currency": "RMB", "unit": "Million",
        "table": "Five Year Summary", "section": "Revenue", "first_component": "Value-added services",
        "last_component": "Others", "total_note": "Total", "next_section": "Cost of revenues",
    }
    with pytest.raises(ValueError):
        onboarding.register_field_anchor("0700.HK", "revenue", spec, approved_by="", expected_sha256=TENCENT_SHA)
    for broken, error in (
        ({**spec, "source_sha256": "0" * 64}, "anchor_sha_mismatch"),
        ({**spec, "page": 9999}, "anchor_page_invalid"),
        ({**spec, "year": "2024"}, "anchor_identity_drift"),
        ({**spec, "file_name": "other.pdf"}, "anchor_identity_drift"),
        ({**spec, "value": "abc"}, "anchor_value_invalid"),
        ({**spec, "unit": ""}, "anchor_unit_invalid"),
    ):
        with pytest.raises(CompanySourceUnavailable, match=error):
            onboarding.register_field_anchor("0700.HK", "revenue", broken,
                                             approved_by="reviewer", expected_sha256=TENCENT_SHA)
    with pytest.raises(CompanySourceUnavailable, match="anchor_sha_mismatch"):
        onboarding.register_field_anchor("0700.HK", "revenue", spec,
                                         approved_by="reviewer", expected_sha256="f" * 64)
    assert json.loads(sandbox["anchors"].read_text(encoding="utf-8")) == {}

    result = onboarding.register_field_anchor("0700.HK", "revenue", spec,
                                              approved_by="reviewer", expected_sha256=TENCENT_SHA)
    assert result["evidence_state"] == onboarding.STATE_FIELD_VERIFIED
    assert onboarding.coverage_state("0700.HK")["evidence_state"] == onboarding.STATE_FIELD_VERIFIED
    anchors = json.loads(sandbox["anchors"].read_text(encoding="utf-8"))
    assert anchors["0700.HK"]["revenue"]["approved_by"] == "reviewer"


def test_field_anchor_fails_when_material_bytes_drift(sandbox, monkeypatch):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=TENCENT_SHA),
    }), encoding="utf-8")
    onboarding.provision_approved("0700.HK", "2025", session=FixtureSession(TENCENT_PDF.read_bytes()))
    entry = json.loads(sandbox["onboarded"].read_text(encoding="utf-8"))["0700.HK"]
    spec = {"year": "2025", "file_name": entry["file_name"], "source_sha256": TENCENT_SHA,
            "source_url": entry["source_url"], "page_authority": "official", "page": 1,
            "value": "1", "comparative": "2", "currency": "RMB", "unit": "Million",
            "table": "T", "section": "S", "first_component": "F", "last_component": "L",
            "total_note": "N", "next_section": "X"}
    (sandbox["pdf"] / entry["file_name"]).write_bytes(b"%PDF-tampered")
    with pytest.raises(CompanySourceUnavailable, match="material_sha256_drift"):
        onboarding.register_field_anchor("0700.HK", "revenue", spec,
                                         approved_by="reviewer", expected_sha256=TENCENT_SHA)
    assert json.loads(sandbox["anchors"].read_text(encoding="utf-8")) == {}


def test_field_anchor_for_unknown_ticker_is_rejected(sandbox):
    with pytest.raises(CompanySourceUnavailable, match="anchor_material_missing"):
        onboarding.register_field_anchor("9988.HK", "revenue", {"year": "2025"},
                                         approved_by="reviewer", expected_sha256="0" * 64)


# --- 接入申请台账：不联网、不写正式清单 ---------------------------------------------


def test_onboarding_request_records_pending_todo_without_network(sandbox):
    result = onboarding.request_onboarding("9988.HK", "2025", requested_by="analyst-1")
    assert result["status"] == "awaiting_preapproval"
    assert result["official_channel"] == "HKEX"
    assert result["network_started"] is False
    assert "source_url" in result["missing_fields"] and "sha256" in result["missing_fields"]
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}
    pending = onboarding.pending_requests()
    assert len(pending) == 1 and pending[0]["ticker"] == "9988.HK"


def test_onboarding_request_rejects_market_conflict_and_bad_input(sandbox):
    with pytest.raises(ValueError):
        onboarding.request_onboarding("9988.HK", "2025", requested_by="a", market="US")
    with pytest.raises(ValueError):
        onboarding.request_onboarding("9988.HK", "25", requested_by="a")
    with pytest.raises(ValueError):
        onboarding.request_onboarding("9988.HK", "2025", requested_by="")


def test_onboarding_request_reports_preapproved_state(sandbox):
    sandbox["approval"].write_text(json.dumps({
        "0700.HK:2025": _approval_entry(sha256=TENCENT_SHA),
    }), encoding="utf-8")
    result = onboarding.request_onboarding("0700.HK", "2025", requested_by="analyst-1")
    assert result["status"] == "approved_pending_download"
    assert result["missing_fields"] == []


def test_onboarding_request_for_existing_material_does_not_duplicate(sandbox):
    sandbox["base"].write_text(json.dumps({
        "0700.HK": {"ticker": "0700.HK", "report_date": "2025", "source": "HKEX",
                    "file_name": "x.pdf", "validation": {"sha256": TENCENT_SHA, "page_count": 282}},
    }, ensure_ascii=False), encoding="utf-8")
    result = onboarding.request_onboarding("0700.HK", "2025", requested_by="analyst-1")
    assert result["status"] == "already_onboarded"


# --- API 权限 --------------------------------------------------------------------


def test_material_request_endpoint_requires_analyst(monkeypatch):
    token = "reviewer-" + "z" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "reviewer", "tenant": "t", "roles": ["reviewer"]}}))
    client = TestClient(api.app)
    assert client.post("/api/company-material-requests", json={"ticker": "9988.HK", "year": "2025"},
                       headers={"Authorization": "Bearer " + token}).status_code == 403


def test_material_request_endpoint_rejects_bad_ticker(sandbox, monkeypatch):
    from investment_assistant import api as api_module

    monkeypatch.setattr(api_module, "provision_approved", onboarding.provision_approved)
    client = TestClient(api.app)
    assert client.post("/api/company-material-requests", json={"ticker": "not a ticker", "year": "2025"}).status_code == 422
    assert client.post("/api/company-material-requests", json={"ticker": "9988.HK", "year": "25"}).status_code == 422


def test_onboarding_request_endpoints_enforce_admin_and_do_not_touch_manifest(sandbox, monkeypatch):
    from investment_assistant import api as api_module

    monkeypatch.setattr(api_module, "pending_requests", onboarding.pending_requests)
    client = TestClient(api.app)
    created = client.post("/api/company-onboarding-requests", json={"ticker": "9988.HK", "year": "2025"})
    assert created.status_code == 201
    assert created.json()["network_started"] is False
    assert client.get("/api/company-onboarding-requests").json()["requests"][0]["ticker"] == "9988.HK"
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}

    token = "analyst-" + "q" * 40
    monkeypatch.setenv("IA_AUTH_TOKENS", json.dumps({token: {"actor": "analyst", "tenant": "t", "roles": ["analyst"]}}))
    assert client.post("/api/company-onboarding-requests", json={"ticker": "9988.HK", "year": "2025"},
                       headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.get("/api/company-onboarding-requests",
                      headers={"Authorization": "Bearer " + token}).status_code == 403
