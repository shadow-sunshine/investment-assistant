"""R1 候选池敏感性实验的离线测试：不加载模型、不访问网络、不索引真实 PDF。"""

from __future__ import annotations

import inspect
import json

import pytest

from investment_assistant import candidate_pool_eval as cpe
from investment_assistant import bilingual_eval
from investment_assistant import rag as rag_module


class _FakeRAG:
    """按 `rag.search()` 的候选池语义模拟检索。

    order 中每项为 (ticker, page, chunk_rank, content)：只有当 chunk_rank < 候选池时，
    该页才可能进入页级结果。这样候选池放大才会真实改变召回，而不是空转一遍。
    """

    def __init__(self, order: list[tuple[str, int, int, str]]) -> None:
        self.order = order
        self.calls: list[dict] = []

    def search(self, query: str, limit: int = 4, ticker: str | None = None) -> list[dict]:
        self.calls.append({"query": query, "limit": limit, "ticker": ticker})
        pool = cpe.pool_from_limit(limit)
        visible = [item for item in self.order if item[2] < pool and (ticker is None or item[0] == ticker)]
        return [
            {"content": content, "metadata": {"page": str(page), "ticker": item_ticker}}
            for item_ticker, page, _rank, content in visible[:limit]
        ]


def _case(case_id: str, quadrant: str, ticker: str, target_pages: list[int], keywords: list[str], forbidden: list[str]) -> dict:
    question_lang, doc_lang = quadrant.split("-")
    return {
        "id": case_id,
        "quadrant": quadrant,
        "question_lang": question_lang,
        "doc_lang": doc_lang,
        "ticker": ticker,
        "question": f"{ticker} {case_id}",
        "target_pages": target_pages,
        "keywords": keywords,
        "evidence_snippet": "snippet",
        "forbidden_tickers": forbidden,
    }


# AAPL 34 页的 chunk 排名是 60：候选池 48 时不可见，108 / 204 时才可见。
AAPL_ORDER = [
    ("AAPL", 10, 0, "Apple page 10"),
    ("AAPL", 34, 60, "Apple total assets 34"),
    ("AAPL", 22, 1, "Apple page 22"),
    ("AAPL", 2, 2, "Apple page 2"),
    ("AAPL", 80, 3, "Apple page 80"),
]
FOREIGN_FIRST_ORDER = [("MSFT", 1, 0, "Microsoft page 1")] + AAPL_ORDER


def _eval_set() -> dict:
    return {
        "materials": {
            "AAPL": {"file_name": "Apple_2025_Form_10-K.pdf", "sha256": "x", "doc_lang": "en"},
            "MSFT": {"file_name": "MSFT.pdf", "sha256": "y", "doc_lang": "en"},
        },
        "cases": [
            _case("deep_page", "en-en", "AAPL", [34], ["assets"], ["MSFT"]),
            _case("top_page", "en-en", "AAPL", [10], ["Apple"], ["MSFT"]),
        ],
    }


def test_pool_targets_map_to_reachable_pools_not_below_target():
    """不动 rag.py 时候选池只能取 12 的整数倍；必须取不小于目标的最小可达值。"""
    assert cpe.POOL_TARGETS == [48, 100, 200]
    assert cpe.pool_from_limit(cpe.limit_for_pool(48)) == 48
    assert cpe.pool_from_limit(cpe.limit_for_pool(100)) == 108
    assert cpe.pool_from_limit(cpe.limit_for_pool(200)) == 204
    for pool in cpe.POOL_TARGETS:
        assert cpe.pool_from_limit(cpe.limit_for_pool(pool)) >= pool


def test_online_default_retrieval_path_is_untouched():
    """实验只改评测器参数：线上默认 limit=4、默认候选池 48、Cross-Encoder 候选上限均未变。"""
    signature = inspect.signature(rag_module.LocalResearchRAG.search)
    assert signature.parameters["limit"].default == 4
    assert cpe.pool_from_limit(4) == 48
    assert rag_module.CROSS_ENCODER_CANDIDATE_LIMIT == 30


def test_top_k_is_frozen_at_four():
    with pytest.raises(ValueError):
        cpe.run_pool(_FakeRAG(AAPL_ORDER), _eval_set(), 48, top_k=8)
    with pytest.raises(ValueError):
        cpe.run_experiment([48], top_k=8)


def test_enlarging_pool_can_change_recall():
    """候选池 48 时排名 60 的目标页不可见；放大到 108 / 204 后进入 Top-4。"""
    eval_set = _eval_set()
    small = cpe.run_pool(_FakeRAG(AAPL_ORDER), eval_set, 48)
    large = cpe.run_pool(_FakeRAG(AAPL_ORDER), eval_set, 200)
    by_id_small = {item["id"]: item for item in small["cases"]}
    by_id_large = {item["id"]: item for item in large["cases"]}
    assert by_id_small["deep_page"]["page_hit_at_k"] is False
    assert by_id_large["deep_page"]["page_hit_at_k"] is True
    assert by_id_small["top_page"]["page_hit_at_k"] is True
    assert large["metrics"]["page_recall_at_k"] > small["metrics"]["page_recall_at_k"]


def test_only_top_four_results_are_scored():
    """候选池放大只影响候选集合，指标口径始终是 Top-4。"""
    eval_set = _eval_set()
    block = cpe.run_pool(_FakeRAG(AAPL_ORDER), eval_set, 200)
    for item in block["cases"]:
        assert len(item["retrieved_pages"]) <= 4
        assert item["citation_page_relevance"] == round(
            sum(1 for page in item["retrieved_pages"] if page in item["target_pages"]) / 4, 4
        )


def test_failure_reason_still_judged_from_scoped_arm_only():
    """unscoped 臂全是错误标的，也不能把已命中的样本改判为失败。"""
    eval_set = _eval_set()
    block = cpe.run_pool(_FakeRAG(FOREIGN_FIRST_ORDER), eval_set, 48)
    top_page = next(item for item in block["cases"] if item["id"] == "top_page")
    assert top_page["cross_ticker_source_count_unscoped"] > 0
    assert top_page["page_hit_at_k"] is True
    assert top_page["failure_reason"] == "none"


def test_pool_sweep_records_pool_size_and_timing():
    block = cpe.run_pool(_FakeRAG(AAPL_ORDER), _eval_set(), 100)
    assert block["candidate_pool_requested"] == 100
    assert block["search_limit"] == 9
    assert block["candidate_pool_formula"] == 108
    assert block["case_count"] == 2
    assert block["total_search_ms"] >= 0
    assert block["mean_search_ms_per_case"] >= 0
    assert all("candidate_pool_formula" in item and "search_seconds_ms" in item for item in block["cases"])


def test_run_pool_does_not_reindex_between_cases():
    """每个样本只发两次检索（scoped + unscoped），不重建索引。"""
    fake = _FakeRAG(AAPL_ORDER)
    cpe.run_pool(fake, _eval_set(), 48)
    assert len(fake.calls) == 4
    assert [call["limit"] for call in fake.calls] == [4, 4, 4, 4]
    assert [call["ticker"] for call in fake.calls] == ["AAPL", None, "AAPL", None]


def test_candidate_pool_is_capped_by_indexed_chunk_count():
    """标的块数不足时，实际池被截断，必须如实记录而不是宣称达到目标池。"""
    fake = _FakeRAG(AAPL_ORDER)
    item = cpe.run_case(
        fake,
        _eval_set()["cases"][0],
        cpe.limit_for_pool(200),
        chunk_counts={"AAPL": 30, "__total__": 100},
    )
    assert item["candidate_pool_formula"] == 204
    assert item["candidate_pool_effective"] == 30
    assert item["candidate_pool_effective_unscoped"] == 100


def test_r1_uses_the_same_r0_sample_set():
    """R1 必须与 R0 同样本、同资料快照；不得为提高指标增删样本。"""
    eval_set = cpe.load_eval_set()
    assert len(eval_set["cases"]) == 32
    counts: dict[str, int] = {}
    for case in eval_set["cases"]:
        counts[case["quadrant"]] = counts.get(case["quadrant"], 0) + 1
    assert counts == {"zh-zh": 8, "en-en": 8, "zh-en": 8, "en-zh": 8}
    assert set(eval_set["materials"]) == {"AAPL", "MSFT", "600519.SS", "0700.HK"}


def test_delta_section_uses_first_and_last_pool():
    """Δ 必须是最小的池到最大的池，而不是任意两个池之差。"""
    assert min(cpe.POOL_TARGETS) == 48
    assert max(cpe.POOL_TARGETS) == 200


def test_unscoped_hit_requires_same_ticker_not_just_same_page():
    """回归：0700.HK 第 57 页曾被当成 600519.SS 第 57 页，只比页码会误判命中。"""
    order = [
        ("600519.SS", 2, 0, "moutai p2"),
        ("600519.SS", 30, 1, "moutai p30"),
        ("600519.SS", 31, 2, "moutai p31"),
        ("0700.HK", 57, 3, "tencent p57"),
    ]
    case = _case("tencent_page_masquerade", "zh-zh", "600519.SS", [14, 57], ["x"], ["0700.HK"])
    item = cpe.run_case(_FakeRAG(order), case, cpe.limit_for_pool(48))
    assert item["unscoped_pages"][-1] == 57
    assert item["cross_ticker_source_count_unscoped"] == 1
    assert item["unscoped_page_hit_at_k"] is False
    assert item["page_hit_at_k"] is False


def test_material_drift_blocks_experiment_before_retrieval(tmp_path, monkeypatch):
    """资料漂移/缺失必须 fail-closed：不跑检索、不产指标、Markdown 显示 BLOCKED。"""
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(
        cpe,
        "verify_materials",
        lambda materials: {
            ticker: {"status": "drift", "expected_sha256": "a", "actual_sha256": "b"} for ticker in materials
        },
    )

    def _must_not_run(*args, **kwargs):
        raise AssertionError("资料漂移时不得跑检索")

    monkeypatch.setattr(cpe, "run_mode", _must_not_run)
    payload = cpe.run_experiment([48])
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "material_drift"
    assert payload["results"] == {}
    assert "BLOCKED" in (tmp_path / "candidate_pool_sensitivity.md").read_text(encoding="utf-8")


def test_missing_material_also_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(
        cpe, "verify_materials", lambda materials: {ticker: {"status": "missing"} for ticker in materials}
    )
    monkeypatch.setattr(cpe, "run_mode", lambda *a, **k: pytest.fail("资料缺失时不得跑检索"))
    assert cpe.run_experiment([48])["status"] == "blocked"


def test_rerender_preserves_blocked_state(tmp_path):
    payload = {
        "status": "blocked",
        "blocked_reason": "material_drift",
        "evaluated_at": "2026-09-25T00:00:00+00:00",
        "material_verification": {"AAPL": {"status": "drift", "expected_sha256": "a", "actual_sha256": "b"}},
        "results": {},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert "BLOCKED" in cpe.render_from_result(result_path).read_text(encoding="utf-8")


def test_report_can_be_rerendered_without_rerunning_retrieval(tmp_path):
    """只有指标变了才需要重跑检索；口径/结论段改动必须能只重写 Markdown。"""
    real_sha = cpe._sha256_of_file(cpe.EVAL_SET_PATH)
    payload = {
        "status": "ok",
        "eval_set_sha256": real_sha,
        "frozen_r0_eval_set_sha256": real_sha,
        "matches_frozen_baseline": True,
        "matches_r0_eval_set": True,
        "eval_set_case_count": 32,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "语义模型不可用，未运行"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    markdown_path = cpe.render_from_result(result_path)
    assert markdown_path.exists()
    content = markdown_path.read_text(encoding="utf-8")
    assert "R1 候选池规模敏感性实验" in content
    assert "semantic" in content


def test_top_result_is_relevant_rejects_wrong_ticker_same_page():
    """P2-2 回归：Top-1 是错误 ticker 但相同页码时，不得算 Top-1 relevant。"""
    case = {"ticker": "600519.SS", "target_pages": [57]}
    wrong = {"metadata": {"ticker": "0700.HK", "page": 57}}
    right = {"metadata": {"ticker": "600519.SS", "page": 57}}
    assert cpe.top_result_is_relevant([wrong], case) is False
    assert cpe.top_result_is_relevant([right], case) is True
    assert cpe.top_result_is_relevant([], case) is False


def test_run_experiment_records_eval_set_sha_and_gold_summary(tmp_path, monkeypatch):
    """P2-1：R1 必须固化「用了哪份样本」，eval_set_sha256 / case_count / gold_summary 都要进产物。"""
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(cpe, "verify_materials", lambda m: {t: {"status": "match"} for t in m})
    monkeypatch.setattr(cpe, "run_mode", lambda *a, **k: {})
    payload = cpe.run_experiment([48])
    assert payload["eval_set_sha256"] == cpe._sha256_of_file(cpe.EVAL_SET_PATH)
    assert payload["eval_set_case_count"] == 32
    assert isinstance(payload["eval_set_gold_summary"], list)
    assert len(payload["eval_set_gold_summary"]) == 32
    first = sorted(cpe.load_eval_set()["cases"], key=lambda c: c["id"])[0]
    assert payload["eval_set_gold_summary"][0]["id"] == first["id"]
    assert payload["eval_set_gold_summary"][0]["target_pages"] == sorted(first["target_pages"])


def test_run_experiment_reports_r0_consistency(tmp_path, monkeypatch):
    """P2-1：R1 跨文件校验是否与 R0 基线同一样本（matches_r0_eval_set）。"""
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(cpe, "verify_materials", lambda m: {t: {"status": "match"} for t in m})
    monkeypatch.setattr(cpe, "run_mode", lambda *a, **k: {})
    # 冻结基线 SHA 也一并打桩，使「当前评测集 SHA == 冻结基线」成立
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: "abc")
    monkeypatch.setattr(cpe, "_read_r0_eval_set_sha", lambda: "abc")
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: "abc")
    assert cpe.run_experiment([48])["matches_r0_eval_set"] is True
    # 当前评测集被改动（SHA 变了）但冻结基线没变 -> 先 BLOCKED，不再跑检索
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: "def")
    blocked = cpe.run_experiment([48])
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "eval_set_mismatch"


def test_r1_rerender_blocks_when_eval_set_changed(tmp_path):
    """P2-1：评测集被改动后，旧结果不得再被 --rerender 重渲染（RENDER BLOCKED）。"""
    payload = {
        "status": "ok",
        "eval_set_sha256": "stored-sha",
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "x"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path, eval_set_path=cpe.EVAL_SET_PATH)
    text = md.read_text(encoding="utf-8")
    assert "样本集已变更" in text


def test_r1_blocks_before_retrieval_when_eval_set_sha_mismatch(tmp_path, monkeypatch):
    """4.B.5：评测集 SHA 与冻结 R0 基线（commit c0f5782）不一致时，必须先于建索引/检索 BLOCKED。"""
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(cpe, "verify_materials", lambda m: {t: {"status": "match"} for t in m})
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: "frozen-baseline-sha")
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: "different-sha")
    calls: list = []

    def _must_not_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("评测集不一致时不得建索引/检索")

    monkeypatch.setattr(cpe, "run_mode", _must_not_run)
    payload = cpe.run_experiment([48])
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "eval_set_mismatch"
    assert payload["results"] == {}
    assert payload["frozen_r0_eval_set_sha256"] == "frozen-baseline-sha"
    assert calls == []  # run_mode 从未被调用
    assert "BLOCKED" in (tmp_path / "candidate_pool_sensitivity.md").read_text(encoding="utf-8")


def test_r1_proceeds_with_frozen_eval_set_sha_and_records_it(tmp_path, monkeypatch):
    """4.B.3/4.B.4：与冻结基线一致的评测集可继续；产物须记录 frozen_r0_eval_set_sha256 + matches_frozen_baseline。"""
    monkeypatch.setattr(cpe, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(cpe, "verify_materials", lambda m: {t: {"status": "match"} for t in m})
    monkeypatch.setattr(cpe, "run_mode", lambda *a, **k: {})
    payload = cpe.run_experiment([48])
    assert payload["status"] == "ok"
    assert payload["frozen_r0_eval_set_sha256"] == bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    assert payload["matches_frozen_baseline"] is True


def test_render_from_result_fail_closed_when_stored_sha_missing(tmp_path):
    """4.B.7：旧结果未记录 eval_set_sha256，不得静默用当前文件 SHA 补写、不得生成正常报告。"""
    payload = {
        "status": "ok",
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "x"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path, eval_set_path=cpe.EVAL_SET_PATH)
    text = md.read_text(encoding="utf-8")
    assert "样本集身份无法校验" in text
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text


# --- R1-F1：BLOCKED 结果可安全重渲染（不抛 KeyError、不生成正常报告） ---


def test_render_eval_set_mismatch_blocked_does_not_keyerror(tmp_path):
    """R1-F1：eval_set_mismatch 的 BLOCKED payload 没有 material_verification，重渲染不得抛 KeyError。"""
    payload = {
        "status": "blocked",
        "blocked_reason": "eval_set_mismatch",
        "eval_set_sha256": "changed-sha",
        "frozen_r0_eval_set_sha256": bilingual_eval.FROZEN_R0_EVAL_SET_SHA256,
        "evaluated_at": "2026-09-25T00:00:00+00:00",
        "results": {},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)  # 若抛 KeyError 则测试失败
    text = md.read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "冻结的 R0 基线" in text  # 走 eval_set_mismatch 报告
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text


def test_render_material_drift_blocked_rerender_without_material_verification(tmp_path):
    """R1-F1：material_drift BLOCKED payload 即使缺 material_verification 也能安全重渲染（不 KeyError）。"""
    payload = {
        "status": "blocked",
        "blocked_reason": "material_drift",
        "evaluated_at": "2026-09-25T00:00:00+00:00",
        "results": {},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)
    text = md.read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "资料快照" in text  # 仍走 material_drift 报告


def test_render_unknown_blocked_reason_fail_closed(tmp_path):
    """R1-F1：未知 BLOCKED 原因或必要字段缺失时 fail-closed，不生成正常报告、不抛 KeyError。"""
    payload = {
        "status": "blocked",
        "blocked_reason": "some_future_reason",
        "evaluated_at": "2026-09-25T00:00:00+00:00",
        "results": {},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)
    text = md.read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text


# --- R1-F2：复渲染再次校验冻结基线 ---


def test_render_blocks_when_sha_matches_current_but_not_frozen(tmp_path, monkeypatch):
    """R1-F2：结果 SHA 与当前一致、但不等于冻结 R0 SHA 时仍 BLOCKED，不生成正常报告。"""
    fake_sha = "not-the-frozen-baseline-sha"
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: fake_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: bilingual_eval.FROZEN_R0_EVAL_SET_SHA256)
    payload = {
        "status": "ok",
        "eval_set_sha256": fake_sha,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "x"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)
    text = md.read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "不是冻结的 R0 基线" in text
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text


def test_render_allows_when_three_shas_align(tmp_path, monkeypatch):
    """R1-F2：结果 SHA == 当前 SHA == 冻结 R0 SHA 时允许正常重渲染。"""
    real_sha = bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: real_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: real_sha)
    payload = {
        "status": "ok",
        "eval_set_sha256": real_sha,
        "frozen_r0_eval_set_sha256": real_sha,
        "matches_frozen_baseline": True,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "语义模型不可用，未运行"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)
    text = md.read_text(encoding="utf-8")
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" in text
    assert "BLOCKED" not in text
    assert "已校验 frozen_r0_eval_set_sha256 相同" in text


def test_render_reports_frozen_fields_from_payload(tmp_path, monkeypatch):
    """R1-F2：复渲染时把 payload 中的冻结基线字段透传给报告（与新增 JSON 产物对齐）。"""
    real_sha = bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: real_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: real_sha)
    payload = {
        "status": "ok",
        "eval_set_sha256": real_sha,
        "frozen_r0_eval_set_sha256": real_sha,
        "matches_frozen_baseline": True,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "语义模型不可用，未运行"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    text = cpe.render_from_result(result_path).read_text(encoding="utf-8")
    # 报告必须出现「与冻结 R0 基线一致」的判定标签，证明字段被正确透传与展示。
    assert "已校验 frozen_r0_eval_set_sha256 相同" in text
    assert "⚠️ **不一致**" not in text


# --- R1-F4：复渲染严格校验 payload 的冻结 SHA 元数据 ---


def test_render_blocks_when_payload_frozen_sha_forged(tmp_path, monkeypatch):
    """R1-F4：eval_set_sha256 正确（==当前==冻结基线），但 payload 自报的 frozen_r0_eval_set_sha256 伪造 -> BLOCKED。"""
    real_sha = bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: real_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: real_sha)
    payload = {
        "status": "ok",
        "eval_set_sha256": real_sha,
        "frozen_r0_eval_set_sha256": "forged-frozen-sha",  # 伪造的冻结 SHA
        "matches_frozen_baseline": True,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "x"}},
    }
    result_path = tmp_path / "candidate_pool_sensitivity.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md = cpe.render_from_result(result_path)
    text = md.read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "冻结元数据不完整或伪造" in text
    assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text


def test_render_blocks_when_frozen_fields_missing(tmp_path, monkeypatch):
    """R1-F4：缺少 frozen_r0_eval_set_sha256 或 matches_frozen_baseline 时（即便 eval_set_sha256 正确）-> BLOCKED。"""
    real_sha = bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: real_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: real_sha)
    base = {
        "status": "ok",
        "eval_set_sha256": real_sha,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {"hash": {"index_seconds": 0.0}, "semantic": {"error": "x"}},
    }
    for label, payload in [
        ("missing frozen_r0_eval_set_sha256", {**base}),
        (
            "missing matches_frozen_baseline",
            {**base, "frozen_r0_eval_set_sha256": real_sha},
        ),
        (
            "matches_frozen_baseline is False",
            {**base, "frozen_r0_eval_set_sha256": real_sha, "matches_frozen_baseline": False},
        ),
    ]:
        result_path = tmp_path / f"candidate_pool_sensitivity_{label}.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        text = cpe.render_from_result(result_path).read_text(encoding="utf-8")
        assert "BLOCKED" in text, f"[{label}] 应被 RENDER BLOCKED"
        assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text, f"[{label}] 不应生成正常报告"

def test_render_blocks_when_status_is_not_ok(tmp_path, monkeypatch):
    """状态不是 ok 时，即使字段完整且 SHA 正确，也不得渲染正常报告。"""
    real_sha = bilingual_eval.FROZEN_R0_EVAL_SET_SHA256
    monkeypatch.setattr(cpe, "_sha256_of_file", lambda p: real_sha)
    monkeypatch.setattr(cpe, "get_frozen_r0_eval_set_sha256", lambda: real_sha)
    base = {
        "eval_set_sha256": real_sha,
        "frozen_r0_eval_set_sha256": real_sha,
        "matches_frozen_baseline": True,
        "matches_r0_eval_set": True,
        "pools_requested": cpe.POOL_TARGETS,
        "material_verification": {},
        "results": {
            "hash": {"index_seconds": 0.0},
            "semantic": {"error": "x"},
        },
    }
    for status in ("pending", "running", "failed", None):
        payload = {**base}
        if status is not None:
            payload["status"] = status
        result_path = tmp_path / f"result_{status or 'missing'}.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        text = cpe.render_from_result(result_path).read_text(encoding="utf-8")
        assert "BLOCKED" in text, f"status={status!r} 应被 fail-closed"
        assert "R1 候选池规模敏感性实验（48 / 100 / 200）" not in text
