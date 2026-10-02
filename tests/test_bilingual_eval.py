"""R0 双语评测样本与评测器的离线测试（不加载模型、不访问网络）。"""

from __future__ import annotations

import json

import pytest

from investment_assistant import bilingual_eval
from investment_assistant.config import KNOWLEDGE_DIR

EXPECTED_QUADRANTS = {"zh-zh": 8, "en-en": 8, "zh-en": 8, "en-zh": 8}
REQUIRED_FIELDS = {
    "id",
    "quadrant",
    "question_lang",
    "doc_lang",
    "ticker",
    "question",
    "target_pages",
    "keywords",
    "evidence_snippet",
    "forbidden_tickers",
}


@pytest.fixture(scope="module")
def eval_set():
    return bilingual_eval.load_eval_set()


def test_eval_set_covers_four_quadrants_evenly(eval_set):
    counts: dict[str, int] = {}
    for case in eval_set["cases"]:
        counts[case["quadrant"]] = counts.get(case["quadrant"], 0) + 1
    assert counts == EXPECTED_QUADRANTS


def test_every_case_has_complete_labels(eval_set):
    material_tickers = set(eval_set["materials"])
    seen_ids: set[str] = set()
    for case in eval_set["cases"]:
        assert REQUIRED_FIELDS <= set(case), f"{case.get('id')} 缺少字段"
        assert case["id"] not in seen_ids
        seen_ids.add(case["id"])
        assert case["question"].strip()
        assert case["target_pages"]
        assert len(case["keywords"]) >= 2
        assert case["evidence_snippet"].strip()
        assert case["ticker"] in material_tickers
        assert case["ticker"] not in case["forbidden_tickers"]
        assert set(case["forbidden_tickers"]) == material_tickers - {case["ticker"]}


def test_language_labels_are_consistent_with_quadrant_and_material(eval_set):
    for case in eval_set["cases"]:
        expected = f"{case['question_lang']}-{case['doc_lang']}"
        assert case["quadrant"] == expected, f"{case['id']} 象限标签与语言标签不一致"
        material_lang = eval_set["materials"][case["ticker"]]["doc_lang"]
        assert case["doc_lang"] == material_lang, f"{case['id']} 资料语言与标注不一致"


def test_top_k_is_frozen_at_four(eval_set):
    with pytest.raises(ValueError):
        bilingual_eval.run_mode("hash", eval_set, top_k=8)


def test_failure_reason_uses_scoped_arm_not_unscoped_pollution():
    """跨标的污染来自对照臂，不能顶替主因，否则会盖住"限定 ticker 也没召回"的真问题。"""
    scoped_hit_with_unscoped_pollution = {
        "cross_ticker_source_count_unscoped": 4,
        "page_hit_at_k": True,
        "keyword_verified_hit_at_k": True,
        "top_result_is_relevant": True,
    }
    assert (
        bilingual_eval._failure_reason(scoped_hit_with_unscoped_pollution, {"question_lang": "en", "doc_lang": "zh"})
        == "none"
    )


def test_failure_reason_distinguishes_cross_language_and_same_language_miss():
    same = {
        "cross_ticker_source_count_unscoped": 0,
        "page_hit_at_k": False,
        "keyword_verified_hit_at_k": False,
        "top_result_is_relevant": False,
    }
    assert bilingual_eval._failure_reason(same, {"question_lang": "en", "doc_lang": "en"}) == "candidate_miss"
    assert bilingual_eval._failure_reason(same, {"question_lang": "en", "doc_lang": "zh"}) == "cross_language_miss"


def test_failure_reason_separates_chunk_boundary_and_ranking():
    hit_not_verified = {
        "cross_ticker_source_count_unscoped": 0,
        "page_hit_at_k": True,
        "keyword_verified_hit_at_k": False,
        "top_result_is_relevant": False,
    }
    hit_but_not_top1 = {**hit_not_verified, "keyword_verified_hit_at_k": True}
    assert bilingual_eval._failure_reason(hit_not_verified, {"question_lang": "zh", "doc_lang": "zh"}) == "chunk_or_value_boundary"
    assert bilingual_eval._failure_reason(hit_but_not_top1, {"question_lang": "zh", "doc_lang": "zh"}) == "ranking"


def test_is_target_source_requires_ticker_and_page():
    """目标命中必须同时满足「同一标的」和「页码落在 gold page 内」。"""
    case = {"ticker": "600519.SS", "target_pages": [14, 57]}
    assert bilingual_eval._is_target_source({"metadata": {"ticker": "600519.SS", "page": "57"}}, case) is True
    assert bilingual_eval._is_target_source({"metadata": {"ticker": "0700.HK", "page": "57"}}, case) is False
    assert bilingual_eval._is_target_source({"metadata": {"ticker": "600519.SS", "page": "99"}}, case) is False


def test_top_result_is_relevant_rejects_wrong_ticker_same_page():
    """P2-2 回归：Top-1 是错误 ticker 但相同页码时，不得算 Top-1 relevant。"""
    case = {"ticker": "600519.SS", "target_pages": [57]}
    wrong = {"metadata": {"ticker": "0700.HK", "page": 57}}
    right = {"metadata": {"ticker": "600519.SS", "page": 57}}
    assert bilingual_eval.top_result_is_relevant([wrong], case) is False
    assert bilingual_eval.top_result_is_relevant([right], case) is True
    assert bilingual_eval.top_result_is_relevant([], case) is False


def test_run_evaluation_records_eval_set_sha(tmp_path, monkeypatch):
    """P2-1：R0 产物也要固化「用了哪份样本」（eval_set_sha256 + case_count）。"""
    monkeypatch.setattr(bilingual_eval, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(bilingual_eval, "verify_materials", lambda m: {t: {"status": "match"} for t in m})

    def _stub_mode(mode, eval_set, top_k):
        return {
            "mode": mode,
            "metrics": {},
            "by_quadrant": {
                q: {
                    "case_count": 0,
                    "page_recall_at_k": 0,
                    "keyword_verified_recall_at_k": 0,
                    "top1_page_relevance": 0,
                    "cross_ticker_sources_per_case_unscoped": 0,
                }
                for q in bilingual_eval.QUADRANT_ORDER
            },
            "cases": [],
            "status": {},
            "indexed_chunks": {},
            "failure_reason_counts": {},
        }

    monkeypatch.setattr(bilingual_eval, "run_mode", _stub_mode)
    payload = bilingual_eval.run_evaluation()
    assert "eval_set_sha256" in payload
    assert payload["eval_set_sha256"] == bilingual_eval._sha256_of_file(bilingual_eval.EVAL_SET_PATH)
    assert payload["eval_set_case_count"] == 32


def test_material_drift_blocks_r0_evaluation(tmp_path, monkeypatch):
    """资料漂移/缺失必须 fail-closed：不跑检索、不产指标、Markdown 显示 BLOCKED。"""
    monkeypatch.setattr(bilingual_eval, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(
        bilingual_eval,
        "verify_materials",
        lambda materials: {
            ticker: {"status": "drift", "expected_sha256": "a", "actual_sha256": "b"} for ticker in materials
        },
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError("资料漂移时不得跑检索")

    monkeypatch.setattr(bilingual_eval, "run_mode", _must_not_run)
    payload = bilingual_eval.run_evaluation()
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "material_drift"
    assert payload["results"] == {}
    assert "BLOCKED" in (tmp_path / "bilingual_r0.md").read_text(encoding="utf-8")


def test_assert_materials_comparable_raises_on_drift_and_missing():
    with pytest.raises(bilingual_eval.MaterialDriftError):
        bilingual_eval.assert_materials_comparable({"AAPL": {"status": "drift"}})
    with pytest.raises(bilingual_eval.MaterialDriftError):
        bilingual_eval.assert_materials_comparable({"AAPL": {"status": "missing"}})
    bilingual_eval.assert_materials_comparable({"AAPL": {"status": "match"}})


def test_material_verification_reports_status_per_ticker(eval_set):
    report = bilingual_eval.verify_materials(eval_set["materials"])
    assert set(report) == set(eval_set["materials"])
    for item in report.values():
        assert item["status"] in {"match", "drift", "missing"}


@pytest.mark.skipif(
    not all((KNOWLEDGE_DIR / name).exists() for name in (
        "Apple_2025_Form_10-K.pdf",
        "MSFT_SEC_10-K_2026-06-30_official-html.pdf",
        "600519_2025_annual_report.pdf",
        "0700HK_annual_report_2025.pdf",
    )),
    reason="本地资料未下载（由 fetch_materials.py 复现），跳过 gold page 实地校验",
)
def test_gold_keywords_actually_appear_on_target_pages(eval_set):
    """反编造护栏：每条样本的目标页里必须真能找到它自己声明的证据片段关键词。"""
    from pypdf import PdfReader

    readers = {
        ticker: PdfReader(str(KNOWLEDGE_DIR / spec["file_name"]))
        for ticker, spec in eval_set["materials"].items()
    }
    for case in eval_set["cases"]:
        reader = readers[case["ticker"]]
        matched_pages = []
        for page_number in case["target_pages"]:
            text = " ".join((reader.pages[page_number - 1].extract_text(extraction_mode="layout") or "").split())
            if all(keyword in text for keyword in case["keywords"]):
                matched_pages.append(page_number)
        assert matched_pages, f"{case['id']} 的目标页 {case['target_pages']} 未同时包含 {case['keywords']}"


def test_frozen_r0_eval_set_sha256_matches_current_file():
    """4.B.2/4.B.3：冻结基线常量必须等于当前评测集原始字节 SHA，否则默认路径会误 BLOCKED。"""
    assert len(bilingual_eval.FROZEN_R0_EVAL_SET_SHA256) == 64
    assert (
        bilingual_eval.get_frozen_r0_eval_set_sha256()
        == bilingual_eval._sha256_of_file(bilingual_eval.EVAL_SET_PATH)
    )


def test_reattribute_payload_fail_closed_on_eval_set_mismatch(tmp_path, monkeypatch):
    """1.3/4.B.6：R0 复渲染时评测集 SHA 不一致，拒绝用新样本重写旧报告。"""
    monkeypatch.setattr(bilingual_eval, "RESULT_DIR", tmp_path)
    payload = {
        "status": "ok",
        "eval_set_sha256": "stored-sha",
        "results": {"hash": {"cases": []}, "semantic": {"cases": []}},
    }
    (tmp_path / "bilingual_r0.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bilingual_eval, "_sha256_of_file", lambda p: "current-sha")
    with pytest.raises(bilingual_eval.MaterialDriftError):
        bilingual_eval.reattribute_payload(tmp_path / "bilingual_r0.json", bilingual_eval.EVAL_SET_PATH)


def test_reattribute_payload_fail_closed_when_sha_missing(tmp_path, monkeypatch):
    """4.B.7：R0 旧结果无 SHA 时 fail-closed，不得静默补写。"""
    monkeypatch.setattr(bilingual_eval, "RESULT_DIR", tmp_path)
    payload = {"status": "ok", "results": {"hash": {"cases": []}, "semantic": {"cases": []}}}
    (tmp_path / "bilingual_r0.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(bilingual_eval.MaterialDriftError):
        bilingual_eval.reattribute_payload(tmp_path / "bilingual_r0.json", bilingual_eval.EVAL_SET_PATH)
