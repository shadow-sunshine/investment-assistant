"""离线实验边界：中文候选增量、页级融合、ticker 与冻结校验。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from investment_assistant import hybrid_eval as ev
from investment_assistant.hybrid_retrieval import (
    ExperimentalHybridRetrieval, FINANCIAL_GLOSSARY, RULES_SHA256, _field_spans,
    _intent, _normalize, _terms,
)


class FakeCollection:
    def __init__(self):
        self.records = [
            ("wrong-0", "普通业务说明，没有财务目标字段", {"ticker": "600519.SS", "source_type": "pdf", "file_name": "a.pdf", "source_id": "wrong", "page": "1"}),
            ("right-0", "营业收入同比增加，营业收入合计 100", {"ticker": "600519.SS", "source_type": "pdf", "file_name": "a.pdf", "source_id": "right", "page": "6"}),
            ("right-1", "营业收入 100，更多表格", {"ticker": "600519.SS", "source_type": "pdf", "file_name": "a.pdf", "source_id": "right", "page": "6"}),
            ("other-0", "营业收入 999", {"ticker": "AAPL", "source_type": "pdf", "file_name": "other.pdf", "source_id": "other", "page": "6"}),
        ]

    def get(self, where, include):
        selected = [r for r in self.records if r[2]["ticker"] == where["ticker"]]
        return {"ids": [r[0] for r in selected], "documents": [r[1] for r in selected], "metadatas": [r[2] for r in selected]}

    def query(self, query_embeddings, n_results, where, include):
        selected = [r for r in self.records if r[2]["ticker"] == where["ticker"]]
        selected.sort(key=lambda r: r[0] != "wrong-0")
        selected = selected[:n_results]
        return {"ids": [[r[0] for r in selected]], "documents": [[r[1] for r in selected]],
                "metadatas": [[r[2] for r in selected]], "distances": [[i * 0.1 for i in range(len(selected))]]}


def fake_rag():
    return SimpleNamespace(provider=SimpleNamespace(mode="hash", embed=lambda items: [[0.1]]),
                           reranker=SimpleNamespace(mode="disabled"), collection=FakeCollection())


def test_chinese_ngrams_expose_substring_not_whole_run():
    assert "营业收入" not in _terms("营业收入合计")  # 四字串通过重叠 2/3-gram 表示
    assert _terms("营业收入") & _terms("营业收入同比")
    assert "revenue" in _terms("Total revenue for FY2025 was 100")


def test_literal_recovers_page_and_deduplicates_with_ticker_scope():
    retriever = ExperimentalHybridRetrieval(fake_rag())
    top, candidates = retriever.search_with_candidates("营业收入是多少？", "600519.SS")
    assert len([c for c in candidates if c["metadata"]["page"] == "6"]) == 1
    assert all(c["metadata"]["ticker"] == "600519.SS" for c in candidates)
    assert any(c["literal_rank"] is not None and c["metadata"]["page"] == "6" for c in candidates)
    assert [c["citation"] for c in top] == [f"S{i}" for i in range(1, len(top) + 1)]
    assert {"content", "metadata", "distance", "vector_score", "lexical_score", "rerank_score", "anchor_score"} <= top[0].keys()


def test_query_only_and_gold_mutation_does_not_change_rank():
    retriever = ExperimentalHybridRetrieval(fake_rag())
    case = {"question": "营业收入是多少？", "ticker": "600519.SS", "target_pages": [6], "keywords": ["营业收入", "100"]}
    first = retriever.search(case["question"], ticker=case["ticker"])
    case.update(target_pages=[999], keywords=["999999"], evidence_snippet="999999")
    second = retriever.search(case["question"], ticker=case["ticker"])
    assert [r["metadata"]["page"] for r in first] == [r["metadata"]["page"] for r in second]
    with pytest.raises(ValueError):
        retriever.search("营业收入", ticker="")


def test_semantic_not_silently_accepted():
    rag = fake_rag()
    rag.provider.mode = "semantic"
    with pytest.raises(ValueError, match="未测"):
        ExperimentalHybridRetrieval(rag)


def test_sha_drift_blocks_before_index(monkeypatch):
    monkeypatch.setattr(ev, "_sha", lambda _: "bad")
    payload = ev.run_experiment()
    assert payload["status"] == "BLOCKED" and payload["results"] == {}
    assert "sha_mismatch" in payload["reason"]


def test_material_drift_blocks_before_index(monkeypatch):
    original = ev.verify_materials

    def drift(materials):
        result = original(materials)
        result["AAPL"]["status"] = "drift"
        return result

    monkeypatch.setattr(ev, "verify_materials", drift)
    payload = ev.run_experiment()
    assert payload["status"] == "BLOCKED" and payload["results"] == {}
    assert "material_drift" in payload["reason"]


def test_foreign_ticker_from_get_fails_closed():
    rag = fake_rag()
    rag.collection.get = lambda where, include: {
        "ids": ["foreign"], "documents": ["营业收入 999"],
        "metadatas": [{"ticker": "AAPL", "source_type": "pdf", "source_id": "foreign", "page": "1"}],
    }
    with pytest.raises(ValueError, match="ticker_scope_violation:get"):
        ExperimentalHybridRetrieval(rag).search("营业收入", ticker="600519.SS")


def test_foreign_ticker_from_vector_query_fails_closed():
    rag = fake_rag()
    original = rag.collection.query
    def foreign_query(**kwargs):
        result = original(**kwargs)
        result["metadatas"][0][0]["ticker"] = "AAPL"
        return result
    rag.collection.query = foreign_query
    with pytest.raises(ValueError, match="ticker_scope_violation:query"):
        ExperimentalHybridRetrieval(rag).search("营业收入", ticker="600519.SS")


@pytest.fixture(autouse=True)
def protect_historical_output(monkeypatch, tmp_path):
    """连漂移拒绝测试也重定向输出，绝不允许默认入口覆盖历史证据。"""
    root = Path(__file__).resolve().parents[1]
    # 在所有漂移/开发测试读取前验证精确允许路径，不能被新评测器改成另一份题集。
    assert ev.EVAL_SET_PATH.resolve() == root / "data/bilingual_eval_set.json"
    assert ev.HOLDOUT_PATH.resolve() == root / "data/r3_holdout_eval_set_v3.json"
    assert ev.R0_RESULT.resolve() == root / "data/evaluations/bilingual_r0.json"
    assert ev.R3_RESULT.resolve() == root / "data/evaluations/r3_real_chroma_v3.json"
    monkeypatch.setattr(ev, "OUTPUT", tmp_path / "dev_only_output.json")


def make_records_rag(contents):
    rag = fake_rag()
    rag.collection.records = [
        (f"record-{index}", content, {"ticker": "TEST", "source_type": "pdf", "file_name": "report.pdf",
                                    "source_id": f"source-{page}", "page": str(page), "version": "original-v7"})
        for index, (page, content) in enumerate(contents)
    ]
    return rag


@pytest.mark.parametrize(("query", "content", "field"), [
    ("经营活动产生的现金净额是多少", "Net cash from operations 12,345 10,987", "operating_cash"),
    ("What were total assets?", "总资产 12,345.00 10,987.00", "total_assets"),
    ("Other receivables balance", "其他应收款 321,000.00", "other_receivables"),
    ("交易性金融资产余额", "Trading financial assets 654,000.00", "trading_assets"),
    ("年度盈利是多少", "Profit for the year 12,345 10,987", "net_income"),
    ("Total stockholders' equity", "所有者权益合计 12,345.00", "total_equity"),
])
def test_bidirectional_generic_glossary(query, content, field):
    rag = make_records_rag([(91, "说明与背景文字 2025"), (3, content)])
    result = ExperimentalHybridRetrieval(rag).search(query, "TEST")
    assert result[0]["content"] == content
    assert result[0]["ranking_evidence"]["tier"] == 4
    assert field in result[0]["ranking_evidence"]["matched_fields"]


def test_longest_field_disambiguates_neighboring_accounts():
    spans = _field_spans(_normalize("其他应收款 1,200 应收账款 2,500 营业总收入 8,000 营业收入 7,000 毛利率 90.2"))
    assert [key for _, _, key in spans] == ["other_receivables", "receivables", "total_revenue", "revenue", "gross_margin_ratio"]
    rag = make_records_rag([(1, "营业收入 7,000.00"), (2, "营业总收入 8,000.00")])
    assert ExperimentalHybridRetrieval(rag).search("营业总收入是多少", "TEST")[0]["metadata"]["page"] == "2"


def test_hash_noise_and_label_only_collection_do_not_beat_numeric_row():
    rag = make_records_rag([(i, "财报目录: 应收账款 附注说明") for i in range(1, 7)] + [(90, "应收账款 123,456.78 101,234.56")])
    top, candidates = ExperimentalHybridRetrieval(rag).search_with_candidates("应收账款余额是多少", "TEST")
    assert len(top) == 4
    assert top[0]["metadata"]["page"] == "90"
    assert top[0]["ranking_evidence"]["row_numbers"] >= 2
    assert candidates[0]["content"] == top[0]["content"]
    assert all(a["rerank_score"] >= b["rerank_score"] for a, b in zip(candidates, candidates[1:]))


@pytest.mark.parametrize("noise", [
    "应收账款 其他应收款 123,456.78",
    "应收账款：本年度存在重大信用风险 " + "背景说明" * 45 + " 123,456.78",
    "应收账款 2025 2024 12",
])
def test_other_field_distant_amount_or_year_is_not_a_numeric_row(noise):
    rag = make_records_rag([(1, noise), (8, "应收账款 123,456.78")])
    top, candidates = ExperimentalHybridRetrieval(rag).search_with_candidates("应收账款", "TEST")
    assert top[0]["metadata"]["page"] == "8"
    bad = next(item for item in candidates if item["metadata"]["page"] == "1")
    assert bad["ranking_evidence"]["tier"] < 4


def test_display_block_is_exact_scoring_block_without_page_stitching():
    rag = make_records_rag([(9, "总资产: 数值见另一表格"), (9, "无关收入项目 999,999.99"), (5, "总资产 123,456.78")])
    top, candidates = ExperimentalHybridRetrieval(rag).search_with_candidates("total assets", "TEST")
    assert top[0]["content"] == "总资产 123,456.78"
    page9 = next(item for item in candidates if item["metadata"]["page"] == "9")
    assert page9["content"] == "总资产: 数值见另一表格"
    assert "999,999.99" not in page9["content"]
    assert page9["ranking_evidence"]["tier"] == 2
    originals = {(content, json.dumps(meta, sort_keys=True)) for _, content, meta in rag.collection.records}
    assert all((item["content"], json.dumps(item["metadata"], sort_keys=True)) in originals for item in candidates)
    assert all(item["metadata"]["version"] == "original-v7" for item in candidates)


def test_changing_page_numbers_does_not_change_content_order():
    contents = [(1, "总资产 123,456.78"), (70, "总资产 123,456.78 110,000.00"), (22, "目录 总资产")]
    first = ExperimentalHybridRetrieval(make_records_rag(contents)).search("total assets", "TEST")
    second = ExperimentalHybridRetrieval(make_records_rag([(999 - p, text) for p, text in contents])).search("total assets", "TEST")
    assert [item["content"] for item in first] == [item["content"] for item in second]


def test_template_year_and_ownership_do_not_dominate_known_field():
    plain = _intent("total assets")
    verbose = _intent("What were Example Corporation's total assets as of the end of fiscal 2025?")
    assert plain["fields"] == verbose["fields"] == {"total_assets"}
    assert not any(any(ch.isdigit() for ch in term) for term in verbose["terms"])


def test_unknown_financial_label_is_explicitly_literal_only():
    top = ExperimentalHybridRetrieval(make_records_rag([(1, "未收录特殊准备金 123,456.78")])).search("未收录特殊准备金", "TEST")
    assert top[0]["matching_mode"] == "literal_only_unknown_field"
    assert top[0]["ranking_evidence"]["tier"] == 0
    assert top[0]["ranking_evidence"]["matched_fields"] == []


def test_eps_subrow_requires_context_in_same_original_block():
    rag = make_records_rag([(1, "Basic 2.34 Diluted 2.30"), (8, "Earnings per share (Note 2) Basic 2.34 Diluted 2.30")])
    top = ExperimentalHybridRetrieval(rag).search("basic earnings per share", "TEST")
    assert top[0]["metadata"]["page"] == "8"
    assert "basic_eps" in top[0]["ranking_evidence"]["matched_fields"]


def test_same_ticker_vector_content_mutation_fails_closed():
    rag = make_records_rag([(1, "总资产 123,456.78")])
    original = rag.collection.query
    def mutate(**kwargs):
        result = original(**kwargs)
        result["documents"][0][0] = "总资产 999,999.99"
        return result
    rag.collection.query = mutate
    with pytest.raises(ValueError, match="record_binding_violation:query"):
        ExperimentalHybridRetrieval(rag).search("total assets", "TEST")


@pytest.mark.parametrize("branch", ["get", "query"])
def test_misaligned_arrays_fail_closed(branch):
    rag = make_records_rag([(1, "总资产 123,456.78")])
    original = getattr(rag.collection, branch)
    def mutate(**kwargs):
        result = original(**kwargs)
        if branch == "get":
            result["documents"] = []
        else:
            result["distances"][0] = []
        return result
    setattr(rag.collection, branch, mutate)
    with pytest.raises(ValueError, match="ticker_scope_violation:" + branch):
        ExperimentalHybridRetrieval(rag).search("total assets", "TEST")


def test_rules_fingerprint_and_glossary_are_auditable():
    assert len(RULES_SHA256) == 64
    assert len({key for key, _ in FINANCIAL_GLOSSARY}) == len(FINANCIAL_GLOSSARY)
    assert not any(any(ch.isdigit() for ch in alias) for _, aliases in FINANCIAL_GLOSSARY for alias in aliases)


@pytest.mark.skipif(os.environ.get("IA_HYBRID_DEV") != "1", reason="仅显式运行旧 R0/R3 dev，禁止默认读取或运行新盲测")
def test_r0_r3_real_chroma_dev(monkeypatch, tmp_path):
    """最多两轮手动 dev；原评测函数只用于计量，不调用会写历史证据的入口。"""
    from investment_assistant.config import KNOWLEDGE_DIR
    from investment_assistant.rag import LocalResearchRAG
    from investment_assistant.r3_real_chroma_eval import _release_chroma_handles

    root = Path(__file__).resolve().parents[1]
    history = root / "data/evaluations/hybrid_hash_v1.json"
    expected_history = "a8f42b8aa5f7a43aac6cde6fa4e07b9221e21b6ac200aa9774332779af7b4a6d"
    assert hashlib.sha256(history.read_bytes()).hexdigest() == expected_history
    old = json.loads(history.read_text(encoding="utf-8"))
    r0, r3, material = ev.validate_snapshots()
    monkeypatch.setenv("RAG_EMBEDDING_MODE", "hash")
    monkeypatch.setenv("RAG_RERANKER_MODE", "disabled")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    index = tmp_path / "real_chroma_dev"
    rag = LocalResearchRAG(path=index)
    try:
        indexed = {ticker: rag.index_pdf(KNOWLEDGE_DIR / spec["file_name"]) for ticker, spec in r3["materials"].items()}
        retriever = ExperimentalHybridRetrieval(rag)
        all_results = {}
        for name, dataset in (("R0", r0), ("R3_v3", r3)):
            started = time.perf_counter()
            rows = []
            for case in dataset["cases"]:
                top, candidates = retriever.search_with_candidates(case["question"], case["ticker"], limit=4)
                assert len(top) == 4
                row = ev._measure(case, top, candidates)
                originals = retriever._records(case["ticker"])
                row["original_block_integrity"] = all(any(item["content"] == record["content"] and item["metadata"] == record["metadata"] for record in originals) for item in top)
                number_keys = [key for key in case["keywords"] if any(ch.isdigit() for ch in key)]
                row["label_number_hit"] = any(
                    ev._is_target_source(item, case) and item["ranking_evidence"]["tier"] >= 3
                    and item["ranking_evidence"]["matched_fields"] and ev._keyword_match(item["content"], case["keywords"])
                    and any(ev._keyword_match(evidence["window"], number_keys)
                            for evidence in item["ranking_evidence"]["number_evidence"])
                    for item in top
                )
                assert row["original_block_integrity"]
                rows.append(row)
                if not row["label_number_hit"]:
                    print("DEV_FAILURE", name, case["id"], "page_hit", row["page_hit_at_k"], "keyword_hit", row["keyword_verified_hit_at_k"])
                    for item in top:
                        evidence = {key: value for key, value in item["ranking_evidence"].items() if key != "number_evidence"}
                        print("  TOP", item["metadata"]["page"], evidence, item["content"][:160])
                    for item in candidates[:48]:
                        if ev._is_target_source(item, case):
                            print("  TARGET_CANDIDATE", item["metadata"]["page"], {key: value for key, value in item["ranking_evidence"].items() if key != "number_evidence"}, item["content"][:160])
            metrics = ev._summary(rows)
            metrics["label_number_at_4"] = round(sum(bool(row["label_number_hit"]) for row in rows) / len(rows), 4)
            quadrants = {}
            for quadrant in ev.QUADRANT_ORDER:
                subset = [row for row in rows if row["quadrant"] == quadrant]
                summary = ev._summary(subset)
                summary["label_number_at_4"] = round(sum(bool(row["label_number_hit"]) for row in subset) / len(subset), 4)
                quadrants[quadrant] = summary
            old_rows = {row["id"]: row for row in old["results"][name]["hybrid"]["cases"]}
            added = [row["id"] for row in rows if row["page_hit_at_k"] and not old_rows[row["id"]]["page_hit_at_k"]]
            lost = [row["id"] for row in rows if not row["page_hit_at_k"] and old_rows[row["id"]]["page_hit_at_k"]]
            all_results[name] = {"metrics": metrics, "by_quadrant": quadrants, "added_vs_v1": added, "lost_vs_v1": lost,
                                 "cases": rows, "seconds": round(time.perf_counter() - started, 3)}
            print("DEV_SUMMARY", name, json.dumps({k: v for k, v in all_results[name].items() if k != "cases"}, ensure_ascii=False))
        payload = {"usage": "R0/R3 explicitly development sets; not blind acceptance", "rules_sha256": RULES_SHA256,
                   "code_sha256": hashlib.sha256((root / "investment_assistant/hybrid_retrieval.py").read_bytes()).hexdigest(),
                   "indexed_chunks": indexed, "material_verification": material, "results": all_results}
        payload["dev_target_gate_met"] = all(
            result["metrics"]["page_recall_at_k"] >= .80
            and result["metrics"]["label_number_at_4"] >= .80
            and result["metrics"]["top1_page_relevance"] >= .60
            and result["metrics"]["scoped_ticker_pollution_count"] == 0
            and all(q["page_recall_at_k"] >= .80 and q["label_number_at_4"] >= .80 for q in result["by_quadrant"].values())
            for result in all_results.values()
        )
        print("DEV_TARGET_GATE_MET", payload["dev_target_gate_met"])
        output = tmp_path / "dev_metrics.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("DEV_TEMP_OUTPUT", output)
        print("DEV_RULES_SHA", RULES_SHA256, "DEV_CODE_SHA", payload["code_sha256"])
    finally:
        _release_chroma_handles(index)
    assert hashlib.sha256(history.read_bytes()).hexdigest() == expected_history


@pytest.mark.parametrize(("query", "content"), [
    ("net cash flow from investing activities", "投资活动产生的现金流 -123,456.78 -98,765.43 不适用 量净额"),
    ("应收账款", "应收 123,456.78 98,765.43 账款"),
])
def test_same_block_interleaved_label_and_numbers_are_auditable(query, content):
    top = ExperimentalHybridRetrieval(make_records_rag([(3, content)])).search(query, "TEST")
    assert top[0]["content"] == content
    assert top[0]["ranking_evidence"]["tier"] == 4
    assert top[0]["ranking_evidence"]["number_evidence"]


def test_interrupted_label_is_not_repaired_across_chunks():
    rag = make_records_rag([(3, "投资活动产生的现金流 -123,456.78"), (3, "量净额")])
    top = ExperimentalHybridRetrieval(rag).search("net cash flow from investing activities", "TEST")
    assert top[0]["ranking_evidence"]["tier"] < 4
    assert top[0]["content"] in {"投资活动产生的现金流 -123,456.78", "量净额"}


def test_coherent_income_table_beats_repeated_segment_field_rows():
    segment = "Revenue 1,500 1,200 Cost of sales 600 500 Operating income 900 700 " * 3
    income = "Total revenue 8,000 7,000 Total cost of revenue 3,000 2,500 Gross profit 5,000 4,500 Research and development 1,000 900 Operating income 4,000 3,600 Provision for income taxes 800 700 Net income 3,200 2,900"
    top = ExperimentalHybridRetrieval(make_records_rag([(1, segment), (2, income)])).search("operating income", "TEST")
    assert top[0]["content"] == income
    assert top[0]["ranking_evidence"]["structured"] == 1


def test_query_literal_segment_label_beats_generic_total_without_product_rules():
    total = "Consolidated statements of operations Net sales 8,000 7,000"
    detail = "Net sales by category in millions: Alpha, Beta and Gamma 3,000 2,500 Other 5,000 4,500"
    top = ExperimentalHybridRetrieval(make_records_rag([(1, total), (2, detail)])).search("What were Example's Alpha, Beta and Gamma net sales?", "TEST")
    assert top[0]["content"] == detail
    assert top[0]["ranking_evidence"]["table_header_row"]


def test_geographic_header_and_row_only_use_same_original_block():
    rag = make_records_rag([(1, "营业收入 8,000 7,000"), (9, "分地区 营业收入 营业成本 毛利率 国内 6,000 3,000 50.00 国外 2,000 1,000 50.00")])
    top = ExperimentalHybridRetrieval(rag).search("国外营业收入是多少", "TEST")
    assert top[0]["metadata"]["page"] == "9"
    assert top[0]["ranking_evidence"]["table_header_row"]


def test_numeric_audit_stops_at_next_field_and_eps_subrow():
    rag = make_records_rag([(1, "其他应收款 12,345.67 其他流动资产 98,765.43"), (2, "Earnings per share Basic 2.34 Diluted 2.30")])
    r = ExperimentalHybridRetrieval(rag)
    top = r.search("other receivables", "TEST")
    windows = top[0]["ranking_evidence"]["number_evidence"]
    assert any("12,345.67" in item["window"] for item in windows)
    assert all("98,765.43" not in item["window"] for item in windows)
    top = r.search("basic earnings per share", "TEST")
    windows = top[0]["ranking_evidence"]["number_evidence"]
    assert any("2.34" in item["window"] for item in windows)
    assert all("2.30" not in item["window"] for item in windows)


def test_gross_margin_ambiguity_is_not_silent_translation_equivalence():
    intent = _intent("gross margin")
    assert intent["fields"] == {"gross_profit", "gross_margin_ratio"}
    assert intent["ambiguities"] == ["gross_margin_absolute_or_ratio"]
    assert _intent("毛利")["fields"] == {"gross_profit"}
    assert _intent("毛利率")["fields"] == {"gross_margin_ratio"}


def test_ticker_renaming_does_not_change_rank():
    contents = [(1, "总资产 123,456.78"), (2, "总资产: 参阅财报附注")]
    first = ExperimentalHybridRetrieval(make_records_rag(contents)).search("total assets", "TEST")
    rag = make_records_rag(contents)
    for _, _, meta in rag.collection.records:
        meta["ticker"] = "OTHER"
    second = ExperimentalHybridRetrieval(rag).search("total assets", "OTHER")
    assert [r["content"] for r in first] == [r["content"] for r in second]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_fail_closed(limit):
    with pytest.raises(ValueError, match="limit"):
        ExperimentalHybridRetrieval(fake_rag()).search("营业收入", "600519.SS", limit)


def test_nonfinite_distance_fails_closed():
    rag = make_records_rag([(1, "总资产 123,456.78")])
    original = rag.collection.query
    def mutate(**kwargs):
        result = original(**kwargs)
        result["distances"][0][0] = float("nan")
        return result
    rag.collection.query = mutate
    with pytest.raises(ValueError, match="invalid_distance"):
        ExperimentalHybridRetrieval(rag).search("total assets", "TEST")


def test_normalization_preserves_negative_answer_values():
    content = "财务费用 -123,456.78 -98,765.43"
    top = ExperimentalHybridRetrieval(make_records_rag([(1, content)])).search("finance expense", "TEST")
    assert top[0]["content"] == content
    assert any("-123,456.78" in item["window"] for item in top[0]["ranking_evidence"]["number_evidence"])
    assert _normalize("right-of-use assets") == "right of use assets"
