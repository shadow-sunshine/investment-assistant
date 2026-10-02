"""R2 中文召回最小可证伪实验 —— 离线测试（fixture 语料，不依赖网络 / 真实模型 / 昂贵索引）。

核心覆盖：
- 受控复刻的 _lexical_overlap / _anchor_score 评分语义；
- baseline 漏召中文金标准页，而中文锚点 / 数值锚点可召回；
- 数值锚点只在证据片段真实含目标值时计命中，不伪造；
- 统一相关性判定（ticker + 页码）拒绝异标同页码；
- scoped / unscoped 两臂与跨标的污染计数；
- 样本 SHA / 资料快照 fail-closed（eval_set_mismatch / material_drift → BLOCKED，不构建语料）；
- 一笔受 `skipif` 保护的真实冻结语料集成测试，验证可证伪对比（数值锚点在 zh-zh 不差于 baseline）。
"""

import json

import pytest

from investment_assistant import bilingual_eval as be
from investment_assistant import chinese_anchor_eval as m


# ---------------------------------------------------------------------------
# 评分单元
# ---------------------------------------------------------------------------

def test_lexical_overlap_zero_for_disjoint():
    assert m._lexical_overlap("苹果 营收", "茅台 存货") == 0.0


def test_lexical_overlap_partial():
    # _lexical_overlap 按整段中文 run 取交集；必须共享同一段中文 run 才计重叠。
    score = m._lexical_overlap("营业收入", "营业收入 168")
    assert score > 0


def test_anchor_score_latin_and_number_only():
    # baseline 锚点只认 Latin 实体 / 数字；纯中文查询不进锚点。
    assert m._anchor_score("Net income 2025", "net income 133,749") >= 1
    assert m._anchor_score("营业收入 是多少", "营业收入 168") == 0.0


def test_numeric_anchor_score_only_when_value_present():
    assert m._numeric_anchor_score("168,838,102,514.79", "营业收入 168,838,102,514.79") == 1.0
    assert m._numeric_anchor_score("168,838,102,514.79", "营业收入 170,899,152,276.34") == 0.0
    assert m._numeric_anchor_score(None, "anything") == 0.0


def test_numeric_anchors_come_only_from_query_text():
    case = _zh_case()
    anchors = m._numeric_anchors(case["question"])
    assert "2025" in anchors
    assert "168,838,102,514.79" not in anchors


def test_lexical_anchors_require_term_in_query_not_gold_keywords():
    assert m._lexical_anchors("这家公司表现如何？") == []
    assert "revenue" in m._lexical_anchors("茅台 2025 年营业收入是多少？")


# ---------------------------------------------------------------------------
# 合成语料 + 用例（不触碰真实 PDF）
# ---------------------------------------------------------------------------

def _zh_case() -> dict:
    return {
        "id": "t_zh",
        "quadrant": "zh-zh",
        "ticker": "600519.SS",
        "question": "贵州茅台2025年营业收入是多少？",
        "target_pages": [6],
        "keywords": ["营业收入", "168,838,102,514.79"],
        "forbidden_tickers": ["AAPL", "MSFT", "0700.HK"],
        "question_lang": "zh",
        "doc_lang": "zh",
    }


def _zh_corpus() -> list[dict]:
    return [
        {
            "ticker": "600519.SS",
            "page": 6,
            "content": "主要会计数据 2025 年营业收入 168,838,102,514.79 170,899,152,276.34",
            "file_name": "600519.pdf",
            "metadata": {"ticker": "600519.SS", "page": 6, "file_name": "600519.pdf"},
        },
        {
            "ticker": "600519.SS",
            "page": 7,
            "content": "其他数据 示例内容",
            "file_name": "600519.pdf",
            "metadata": {"ticker": "600519.SS", "page": 7, "file_name": "600519.pdf"},
        },
        {
            "ticker": "AAPL",
            "page": 1,
            "content": "Apple 2025 total assets 359,241",
            "file_name": "aapl.pdf",
            "metadata": {"ticker": "AAPL", "page": 1, "file_name": "aapl.pdf"},
        },
    ]


def test_baseline_does_not_use_gold_keywords_as_query():
    case, corpus = _zh_case(), _zh_corpus()
    case["question"] = "普通问题"
    r = m.run_case(m.ChineseAnchorRetriever("baseline"), case, corpus, 4)
    assert r["page_hit_at_k"] is False
    assert r["top_result_is_relevant"] is False


def test_lexical_anchor_finds_chinese_gold_page():
    case, corpus = _zh_case(), _zh_corpus()
    r = m.run_case(m.ChineseAnchorRetriever("lexical_anchor"), case, corpus, 4)
    assert r["page_hit_at_k"] is True
    assert r["top_result_is_relevant"] is True


def test_numeric_anchor_uses_query_year_not_gold_answer_value():
    case, corpus = _zh_case(), _zh_corpus()
    retriever = m.ChineseAnchorRetriever("numeric_anchor")
    assert "2025" in m._numeric_anchors(case["question"])
    assert "168,838,102,514.79" not in m._numeric_anchors(case["question"])
    assert retriever.score("普通问题", "168,838,102,514.79") == 0.0
    result = m.run_case(retriever, case, corpus, 4)
    assert result["page_hit_at_k"] is True


def test_numeric_anchor_does_not_fabricate_when_value_absent():
    case = {
        "id": "x",
        "quadrant": "zh-zh",
        "ticker": "600519.SS",
        "question": "q",
        "target_pages": [6],
        "keywords": ["营业收入", "999,999,999.99"],
        "forbidden_tickers": [],
        "question_lang": "zh",
        "doc_lang": "zh",
    }
    corpus = _zh_corpus()
    r = m.run_case(m.ChineseAnchorRetriever("numeric_anchor"), case, corpus, 4)
    # 目标值不在语料 → 不得伪造命中
    assert r["page_hit_at_k"] is False


def test_unified_relevance_rejects_wrong_ticker_same_page():
    case = _zh_case()
    case["question_lang"] = "zh"
    case["doc_lang"] = "zh"
    corpus = [
        {
            "ticker": "AAPL",
            "page": 6,
            "content": "营业收入 168,838,102,514.79",
            "file_name": "a.pdf",
            "metadata": {"ticker": "AAPL", "page": 6, "file_name": "a.pdf"},
        }
    ]
    r = m.run_case(m.ChineseAnchorRetriever("numeric_anchor"), case, corpus, 4)
    assert r["page_hit_at_k"] is False


def test_scoped_vs_unscoped_cross_ticker_counting():
    case, corpus = _zh_case(), _zh_corpus()
    r = m.run_case(m.ChineseAnchorRetriever("baseline"), case, corpus, 4)
    # unscoped 臂含 forbidden 标的（AAPL）页 → 应计污染
    assert r["cross_ticker_source_count_unscoped"] >= 1
    assert "AAPL" in r["unscoped_tickers"]


def test_four_quadrant_aggregation_structure():
    cases = [
        _zh_case(),
        {
            "id": "en1",
            "quadrant": "en-en",
            "ticker": "AAPL",
            "question": "What were Apple total assets?",
            "target_pages": [1],
            "keywords": ["Total assets", "359,241"],
            "forbidden_tickers": ["600519.SS"],
            "question_lang": "en",
            "doc_lang": "en",
        },
    ]
    corpus = _zh_corpus()
    strat = m._run_strategy("numeric_anchor", {"cases": cases}, corpus, 4)
    assert strat["metrics"]["case_count"] == 2
    assert set(strat["by_quadrant"].keys()) == set(be.QUADRANT_ORDER)


def test_frozen_eval_set_sha_matches_constant():
    assert be._sha256_of_file(be.EVAL_SET_PATH) == be.get_frozen_r0_eval_set_sha256()


# ---------------------------------------------------------------------------
# fail-closed 护栏（不构建语料、不写盘）
# ---------------------------------------------------------------------------

def test_run_experiment_blocks_on_eval_set_mismatch(monkeypatch):
    monkeypatch.setattr(m, "_write_payload", lambda p: None)
    monkeypatch.setattr(m, "get_frozen_r0_eval_set_sha256", lambda: "deadbeef" * 8)
    payload = m.run_experiment(top_k=4, eval_set_path=be.EVAL_SET_PATH)
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "eval_set_mismatch"
    assert payload["results"] == {}


def test_run_experiment_blocks_on_material_drift(monkeypatch):
    monkeypatch.setattr(m, "_write_payload", lambda p: None)
    monkeypatch.setattr(m, "get_frozen_r0_eval_set_sha256", lambda: be._sha256_of_file(be.EVAL_SET_PATH))
    monkeypatch.setattr(
        m,
        "verify_materials",
        lambda materials: {t: {"status": "drift", "expected_sha256": "x", "actual_sha256": "y"} for t in materials},
    )
    payload = m.run_experiment(top_k=4, eval_set_path=be.EVAL_SET_PATH)
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "material_drift"


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        m.ChineseAnchorRetriever("combined")


# ---------------------------------------------------------------------------
# 受 skipif 保护的真实冻结语料集成测试（PDF 缺失则跳过；不写盘以免覆盖交付物）
# ---------------------------------------------------------------------------

_eval = json.loads(be.EVAL_SET_PATH.read_text(encoding="utf-8-sig"))
_pdf_present = all((be.KNOWLEDGE_DIR / spec["file_name"]).exists() for spec in _eval["materials"].values())


@pytest.mark.skipif(not _pdf_present, reason="冻结资料 PDF 缺失，跳过真实语料集成测试")
def test_real_corpus_falsifiable_claim(monkeypatch):
    monkeypatch.setattr(m, "_write_payload", lambda p: None)
    payload = m.run_experiment(top_k=4, eval_set_path=be.EVAL_SET_PATH)
    assert payload["status"] == "ok"
    assert payload["matches_frozen_baseline"] is True
    assert set(payload["timing_seconds"].keys()) == set(m.STRATEGIES)
    # 可证伪对比：数值锚点在中→中象限 Page Recall 不差于 baseline
    zhzh_base = payload["results"]["baseline"]["by_quadrant"]["zh-zh"]["page_recall_at_k"]
    zhzh_num = payload["results"]["numeric_anchor"]["by_quadrant"]["zh-zh"]["page_recall_at_k"]
    assert zhzh_num >= zhzh_base
    # 数值锚点不得伪造核验命中：核验 Recall == Page Recall（召回的金标准页均真实含关键词/值）
    for s in m.STRATEGIES:
        assert payload["results"][s]["metrics"]["keyword_verified_recall_at_k"] == payload["results"][s]["metrics"]["page_recall_at_k"]
