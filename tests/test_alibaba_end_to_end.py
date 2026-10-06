"""阿里巴巴全链路端到端：候选识别 → 受控接入 → 字段锚点 → 受控回答。

**当前真实状态：阿里巴巴尚未接入官方年报。** 本文件用本地 fixture 证明闭环代码路径可用，
并显式断言"在真实数据接入前，系统不会给出任何阿里数字"。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_assistant import api, company_discovery as cd, company_onboarding as onboarding
from investment_assistant.chat_session import new_context, route_message

ALIBABA_HK_URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0620/2026062001234.pdf"
ALIBABA_US_URL = "https://www.sec.gov/Archives/edgar/data/1577552/000119312526123456/d123456d20f.htm"

#: 仓库当前没有阿里巴巴官方文件，这里用一个最小可解析 PDF 作为**测试夹具**，
#: 它的 SHA 只存在于测试里，绝不会被写进正式预审清单。
ALIBABA_FIXTURE_PDF = Path("data/knowledge_base/0700HK_annual_report_2025.pdf")
ALIBABA_FIXTURE_SHA = "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98"


# --- 现状：未接入前不得给出任何阿里数字 ---------------------------------------------


def test_alibaba_is_not_onboarded_in_the_real_repository():
    """如实记录当前状态：阿里不在正式清单里，也不在预审白名单里。"""
    manifest = json.loads(onboarding.MANIFEST_PATH.read_text(encoding="utf-8"))
    onboarded = json.loads(onboarding.ONBOARDED_PATH.read_text(encoding="utf-8"))
    allowlist = json.loads(onboarding.APPROVAL_PATH.read_text(encoding="utf-8"))
    assert "9988.HK" not in manifest and "9988.HK" not in onboarded
    assert "BABA" not in manifest and "BABA" not in onboarded
    assert not [key for key in allowlist if key.startswith(("9988.HK", "BABA"))]
    assert onboarding.coverage_state("9988.HK")["material"] == "not_onboarded"


def test_alibaba_financial_question_is_refused_at_every_layer():
    """候选存在也不等于可回答：路由、工具、公司问答三层都必须拒答数字。"""
    for question in ("阿里巴巴2025年的收入怎么样", "Alibaba 2025 revenue", "阿里巴巴9988.HK 2025年收入"):
        kind, detail = route_message(question, new_context())
        assert kind == "guidance", question
        assert "不会" in detail or "尚未" in detail

    result = cd.resolve_company_candidates("阿里巴巴", market="HK")
    assert result.to_dict()["answerable"] is False

    from investment_assistant.research_tools import get_structured_financial_candidates_tool
    from investment_assistant.access_control import Principal

    payload = get_structured_financial_candidates_tool(
        principal=Principal(actor_id="a", tenant_id="t", roles=frozenset({"analyst"})), ticker="9988.HK",
    )
    assert payload["verified"] is False and payload["requires_official_verification"] is True

    # 已收录的腾讯也不能被拿来回答阿里。
    from investment_assistant.company_qa import CompanyAnswerService

    answer = CompanyAnswerService().answer("0700.HK", "阿里巴巴2025年收入", requested_by="a")
    assert answer["status"] == "refused" and answer["sources"] == []
    assert "751,766" not in answer["answer"]


def test_alibaba_discovery_api_never_claims_verified():
    body = TestClient(api.app).post("/api/company-discovery", json={"query": "阿里巴巴", "market": "HK"}).json()
    assert body["status"] == cd.STATUS_MATCHED
    assert body["error_code"] == cd.ERROR_OFFICIAL_MATERIAL_REQUIRED
    assert body["answerable"] is False
    assert body["candidates"][0]["verified"] is False
    assert body["candidates"][0]["official_channel"] == "HKEX"
    assert body["candidates"][0]["evidence_level"] == "candidate"


# --- 闭环：夹具证明代码路径可用（不写正式清单）---------------------------------------


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    base, attached = tmp_path / "base.json", tmp_path / "onboarded.json"
    approval, requests_path, anchors = tmp_path / "approval.json", tmp_path / "requests.json", tmp_path / "anchors.json"
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    for path in (base, attached, approval, requests_path, anchors):
        path.write_text("{}", encoding="utf-8")
    for name, value in (("MANIFEST_PATH", base), ("ONBOARDED_PATH", attached), ("APPROVAL_PATH", approval),
                        ("REQUESTS_PATH", requests_path), ("ANCHORS_PATH", anchors), ("KNOWLEDGE_DIR", pdf_dir)):
        monkeypatch.setattr(onboarding, name, value)
    return {"base": base, "onboarded": attached, "approval": approval, "anchors": anchors, "pdf": pdf_dir}


class FixtureSession:
    def __init__(self, payload: bytes, content_type: str = "application/pdf") -> None:
        self.payload, self.content_type = payload, content_type
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        session = self

        class Response:
            status_code = 200
            headers = {"Content-Type": session.content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_content(self, chunk_size):
                for start in range(0, len(session.payload), chunk_size):
                    yield session.payload[start:start + chunk_size]

        return Response()


def test_alibaba_full_loop_with_fixture_ends_at_material_only(sandbox):
    """完整闭环：登记申请 → 预审 → 入库校验 → 状态停在 material_only。"""
    request = onboarding.request_onboarding("9988.HK", "2025", requested_by="analyst-1")
    assert request["status"] == "awaiting_preapproval" and request["official_channel"] == "HKEX"
    assert request["network_started"] is False

    sandbox["approval"].write_text(json.dumps({
        "9988.HK:2025": {"approved": True, "market": "HKEX", "company": "Alibaba Group Holding Limited",
                         "report_date": "2025", "source_url": ALIBABA_HK_URL,
                         "sha256": ALIBABA_FIXTURE_SHA, "page_count": 282},
    }), encoding="utf-8")
    assert onboarding.request_onboarding("9988.HK", "2025", requested_by="analyst-1")["status"] == "approved_pending_download"

    # 夹具是腾讯年报，其前三页不含阿里公司名 → HKEX 通道的前置页公司名校验必须拒绝。
    # 这证明"光有正确 SHA 与页数还不够"，公司身份也必须能在官方文件里核验。
    with pytest.raises(onboarding.CompanySourceUnavailable, match="pdf_lead_page_company_mismatch"):
        onboarding.provision_approved(
            "9988.HK", "2025", session=FixtureSession(ALIBABA_FIXTURE_PDF.read_bytes()))
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}
    assert list(sandbox["pdf"].iterdir()) == []


def test_alibaba_hk_material_onboards_but_stays_material_only(sandbox, monkeypatch):
    """当公司名确实出现在官方文件里时：入库成功，但状态只能是 material_only。"""
    # 让夹具通过"公司名在前三页"这一项，其余校验（SHA/页数/PDF 格式）保持真实执行。
    monkeypatch.setattr(onboarding, "_validate_payload",
                        lambda path, approved, pages, is_html: "fixture lead text")
    sandbox["approval"].write_text(json.dumps({
        "9988.HK:2025": {"approved": True, "market": "HKEX", "company": "Alibaba Group Holding Limited",
                         "report_date": "2025", "source_url": ALIBABA_HK_URL,
                         "sha256": ALIBABA_FIXTURE_SHA, "page_count": 282},
    }), encoding="utf-8")
    result = onboarding.provision_approved(
        "9988.HK", "2025", session=FixtureSession(ALIBABA_FIXTURE_PDF.read_bytes()))
    assert result["status"] == "material_onboarded"
    assert result["evidence_state"] == onboarding.STATE_MATERIAL_ONLY
    assert result["next_step"] == "field_anchor_required"
    assert onboarding.coverage_state("9988.HK")["evidence_state"] == onboarding.STATE_MATERIAL_ONLY
    # 未登记字段锚点前，系统仍不能回答阿里收入。
    assert cd.resolve_company_candidates("阿里巴巴", market="HK").to_dict()["answerable"] is False
    assert json.loads(sandbox["base"].read_text(encoding="utf-8")) == {}


def test_alibaba_sec_path_requires_company_name_in_filing(sandbox):
    body = ("<html><body>Annual Report on Form 20-F for the fiscal year ended March 31, 2025 "
            "</body></html>").encode("utf-8") + b" " * 1500
    sha = hashlib.sha256(body).hexdigest()
    sandbox["approval"].write_text(json.dumps({
        "BABA:2025": {"approved": True, "market": "SEC", "company": "Alibaba Group Holding Limited",
                      "report_date": "2025", "source_url": ALIBABA_US_URL, "sha256": sha, "page_count": 1},
    }), encoding="utf-8")
    with pytest.raises(onboarding.CompanySourceUnavailable, match="filing_text_layer_invalid"):
        onboarding.provision_approved("BABA", "2025", session=FixtureSession(body, "text/html"))
    assert json.loads(sandbox["onboarded"].read_text(encoding="utf-8")) == {}

    # 公司名确实在文件里时才允许入库。
    body_with_name = body.replace(b"Annual Report", b"Alibaba Group Holding Limited Annual Report")
    sha2 = hashlib.sha256(body_with_name).hexdigest()
    sandbox["approval"].write_text(json.dumps({
        "BABA:2025": {"approved": True, "market": "SEC", "company": "Alibaba Group Holding Limited",
                      "report_date": "2025", "source_url": ALIBABA_US_URL, "sha256": sha2, "page_count": 1},
    }), encoding="utf-8")
    ok = onboarding.provision_approved("BABA", "2025", session=FixtureSession(body_with_name, "text/html"))
    assert ok["status"] == "material_onboarded"
    assert ok["evidence_state"] == onboarding.STATE_MATERIAL_ONLY
    entry = json.loads(sandbox["onboarded"].read_text(encoding="utf-8"))["BABA"]
    # SEC 通道入库后同样需要完整字段锚点（含表格定位 token）才能回答。
    base_spec = {"year": "2025", "file_name": entry["file_name"], "source_sha256": sha2,
                 "source_url": entry["source_url"], "page_authority": entry["page_authority"],
                 "page": 1, "value": "996,347", "comparative": "941,168",
                 "currency": "RMB", "unit": "Million"}
    with pytest.raises(onboarding.CompanySourceUnavailable, match="anchor_tokens_missing"):
        onboarding.register_field_anchor("BABA", "revenue", base_spec,
                                         approved_by="reviewer", expected_sha256=sha2)
    assert json.loads(sandbox["anchors"].read_text(encoding="utf-8")) == {}
