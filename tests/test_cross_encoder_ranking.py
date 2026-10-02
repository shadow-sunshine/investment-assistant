"""固定候选精排的同池、归属、数值与篡改边界测试；不调用任何模型 SDK。"""
from copy import deepcopy
import hashlib
import json
from fractions import Fraction

import pytest

from investment_assistant.cross_encoder_ranking import candidate_fingerprint, rank_candidates

TICKER = "600519.SS"
SHA = "a" * 64
QUERY = "  What was 营业收入 in 2025?  "


def material():
    return {"file_name": "annual.pdf", "sha256": SHA, "page_count": 100}


def candidates(count=6):
    return [
        {
            "content": f"原始页 {index + 1}\nRevenue 营业收入 {100 + index},000.00",
            "metadata": {
                "ticker": TICKER, "file_name": "annual.pdf", "source_sha256": SHA,
                "source_id": f"pdf-{index + 1}", "source_type": "pdf",
                "page": str(index + 1), "chunk_index": "0",
            },
            "distance": None if index == 0 else index / 10,
            "vector_score": 0.5, "lexical_score": 2.0, "rerank_score": float(count - index),
            "ranking_evidence": {"fields": ["revenue"], "number_evidence": [{"window": "100,000.00"}]},
            "citation": f"S{index + 1}",
        }
        for index in range(count)
    ]


def rank(items=None, scorer=None, spec=None, **kwargs):
    items = candidates() if items is None else items
    scorer = (lambda pairs: [float(index) for index in range(len(pairs))]) if scorer is None else scorer
    return rank_candidates(QUERY, items, scorer, TICKER, material() if spec is None else spec, **kwargs)


def test_identical_candidates_and_only_raw_question_content_reach_scorer():
    items = candidates()
    before = deepcopy(items)
    spec = material()
    spec["target_pages"] = [99]
    spec["keywords"] = ["private_gold"]
    seen = []

    def scorer(pairs):
        seen.append(pairs)
        assert type(pairs) is tuple
        assert all(type(pair) is tuple and len(pair) == 2 for pair in pairs)
        return [-1.0, 3.5, 0.0, 1.0, 3.0, 2.0]

    result = rank(items, scorer, spec)
    assert seen == [tuple((QUERY, source["content"]) for source in before)]
    assert result["ranked_indices"] == [1, 4, 5, 3, 2, 0]
    assert result["sources"] == [before[index] for index in [1, 4, 5, 3]]
    assert result["scores"] == [3.5, 3.0, 2.0, 1.0]
    assert result["candidate_scores"] == [-1.0, 3.5, 0.0, 1.0, 3.0, 2.0]
    assert result["candidate_fingerprint"] == candidate_fingerprint(before)
    assert items == before


def test_stable_ties_preserve_original_order_without_rewriting_sources():
    items = candidates()
    result = rank(items, lambda pairs: [0.0] * len(pairs))
    assert result["sources"] == items[:4]
    assert result["ranked_indices"] == list(range(6))
    assert [source["citation"] for source in result["sources"]] == ["S1", "S2", "S3", "S4"]
    assert result["returned_count"] == 4


def test_budget_is_literal_first_48_not_scored_selection_or_page_merge():
    items = candidates(60)
    seen = []

    def scorer(pairs):
        seen.extend(pairs)
        return list(range(len(pairs)))

    result = rank(items, scorer)
    assert seen == [(QUERY, source["content"]) for source in items[:48]]
    assert result["sources"] == [items[index] for index in [47, 46, 45, 44]]
    assert result["input_candidate_count"] == 60
    assert result["candidate_count"] == 48
    assert result["budget"] == 48 and result["limit"] == 4
    assert result["candidate_fingerprint"] == candidate_fingerprint(items[:48])
    assert result["input_fingerprint"] == candidate_fingerprint(items)
    assert result["candidate_fingerprint"] != result["input_fingerprint"]


def test_smaller_budget_is_prefix_and_short_pool_returns_actual_count():
    items = tuple(candidates(3))
    result = rank(items)
    assert result["sources"] == list(reversed(items))
    assert result["candidate_count"] == result["returned_count"] == 3
    result = rank(candidates(6), limit=2, budget=3)
    assert result["sources"] == [candidates()[2], candidates()[1]]
    assert result["ranked_indices"] == [2, 1, 0]


def test_empty_candidates_do_not_call_scorer():
    def scorer(pairs):
        pytest.fail("空池不得调用模型")
    result = rank([], scorer)
    assert result["sources"] == result["scores"] == result["candidate_scores"] == []
    assert result["ranked_indices"] == []
    assert result["candidate_count"] == result["returned_count"] == 0
    assert result["candidate_fingerprint"] == hashlib.sha256(b"[]").hexdigest()


def test_success_does_not_modify_input_or_return_nested_aliases():
    items, spec = candidates(), material()
    before, spec_before = deepcopy(items), deepcopy(spec)
    result = rank(items, spec=spec)
    assert items == before and spec == spec_before
    result["sources"][0]["content"] = "changed result"
    result["sources"][1]["metadata"]["page"] = "99"
    result["sources"][2]["ranking_evidence"]["fields"].append("changed result")
    assert items == before and spec == spec_before


def test_fingerprint_is_canonical_but_binds_order_and_entire_source():
    items = candidates()
    reordered = [{key: value for key, value in reversed(list(source.items()))} for source in items]
    for source in reordered:
        source["metadata"] = dict(reversed(list(source["metadata"].items())))
    expected = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    assert candidate_fingerprint(items) == candidate_fingerprint(tuple(reordered)) == expected
    assert candidate_fingerprint(list(reversed(items))) != expected
    for field in ["content", "metadata", "ranking_evidence", "rerank_score"]:
        changed = deepcopy(items)
        changed[0][field] = "changed"
        assert candidate_fingerprint(changed) != expected


@pytest.mark.parametrize("query", [None, 1, True, "", " \n\t"])
def test_invalid_question_blocks_before_scoring(query):
    with pytest.raises(ValueError, match="invalid_query"):
        rank_candidates(query, candidates(), lambda pairs: pytest.fail("不应调用"), TICKER, material())


@pytest.mark.parametrize("ticker", [None, 1, True, "", " \t"])
def test_invalid_ticker_blocks_before_scoring(ticker):
    with pytest.raises(ValueError, match="invalid_ticker"):
        rank_candidates(QUERY, candidates(), lambda pairs: pytest.fail("不应调用"), ticker, material())


@pytest.mark.parametrize("field,value", [("limit", 0), ("limit", -1), ("limit", True), ("limit", 1.5), ("limit", "4"), ("limit", 5), ("budget", 0), ("budget", -1), ("budget", True), ("budget", 1.5), ("budget", 49), ("budget", "48")])
def test_out_of_bounds_limit_budget(field, value):
    with pytest.raises(ValueError, match=f"invalid_{field}"):
        rank(scorer=lambda pairs: pytest.fail("不应调用"), **{field: value})


def test_limit_above_budget_and_noncallable_scorer_are_rejected():
    with pytest.raises(ValueError, match="limit_exceeds_budget"):
        rank(limit=4, budget=3)
    with pytest.raises(ValueError, match="invalid_scorer"):
        rank(scorer=123)


@pytest.mark.parametrize("raw", [None, 0.5, "scores", {"0": 1}, iter([1, 2, 3, 4, 5, 6]), [], [1], [1] * 7, [[1]] * 6, [True] * 6, [False] * 6, [None] * 6, ["1"] * 6, [1 + 2j] * 6, [float("nan")] * 6, [float("inf")] * 6, [-float("inf")] * 6, [10 ** 400] * 6])
def test_invalid_score_shape_count_type_and_finiteness_fail_closed(raw):
    items = candidates()
    before = deepcopy(items)
    with pytest.raises(ValueError):
        rank(items, lambda pairs: raw)
    assert items == before


def test_real_scores_tuple_negative_fraction_and_signed_zero_are_accepted():
    result = rank(scorer=lambda pairs: (Fraction(1, 2), -2, 0, -0.0, 4, 3))
    assert result["candidate_scores"] == [0.5, -2.0, 0.0, -0.0, 4.0, 3.0]
    assert result["ranked_indices"] == [4, 5, 0, 2, 3, 1]


def test_scorer_exception_preserves_cause_and_never_falls_back():
    def scorer(pairs):
        raise RuntimeError("model unavailable")
    with pytest.raises(ValueError, match="scorer_failed") as caught:
        rank(scorer=scorer)
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.mark.parametrize("field,value,error", [("ticker", "AAPL", "ticker_scope_violation"), ("file_name", "other.pdf", "file_scope_violation"), ("source_sha256", "b" * 64, "material_version_violation"), ("source_sha256", None, "material_version_violation"), ("source_id", "", "invalid_source_id"), ("source_id", " \n", "invalid_source_id"), ("source_id", 5, "invalid_source_id"), ("page", "", "invalid_source_page"), ("page", "0", "invalid_source_page"), ("page", "01", "invalid_source_page"), ("page", " 1", "invalid_source_page"), ("page", -1, "invalid_source_page"), ("page", True, "invalid_source_page"), ("page", 1.5, "invalid_source_page"), ("page", 101, "invalid_source_page")])
def test_source_provenance_and_page_validation(field, value, error):
    items = candidates()
    items[0]["metadata"][field] = value
    with pytest.raises(ValueError, match=error):
        rank(items, lambda pairs: pytest.fail("归属异常不应调用模型"))


@pytest.mark.parametrize("field", ["ticker", "file_name", "source_id", "page"])
def test_missing_provenance_is_rejected(field):
    items = candidates()
    del items[0]["metadata"][field]
    with pytest.raises(ValueError):
        rank(items, lambda pairs: pytest.fail("不应调用"))


@pytest.mark.parametrize("field,value", [("content", ""), ("content", " \n"), ("content", None), ("content", 1), ("metadata", None), ("metadata", []), ("metadata", {})])
def test_missing_or_invalid_content_metadata(field, value):
    items = candidates()
    items[0][field] = value
    with pytest.raises(ValueError):
        rank(items, lambda pairs: pytest.fail("不应调用"))


def test_duplicate_page_cannot_be_hidden_with_different_source_id_or_page_type():
    items = candidates()
    items[1]["metadata"]["page"] = 1
    assert items[1]["metadata"]["source_id"] != items[0]["metadata"]["source_id"]
    with pytest.raises(ValueError, match="duplicate_candidate_page"):
        rank(items)


def test_valid_integer_page_is_not_rewritten():
    items = candidates()
    items[5]["metadata"]["page"] = 6
    assert rank(items)["sources"][0]["metadata"]["page"] == 6


def test_invalid_tail_candidate_is_not_exempt_from_validation():
    items = candidates(49)
    items[48]["metadata"]["ticker"] = "AAPL"
    with pytest.raises(ValueError, match="ticker_scope_violation"):
        rank(items, lambda pairs: pytest.fail("不应调用"))


@pytest.mark.parametrize("spec", [None, [], {}, {"file_name": "", "sha256": SHA}, {"file_name": "annual.pdf", "sha256": "x" * 64}, {"file_name": "annual.pdf", "sha256": "A" * 64}, {"file_name": "annual.pdf", "sha256": SHA, "ticker": "AAPL"}, {"file_name": "annual.pdf", "sha256": SHA, "source_sha": "b" * 64}, {"file_name": "annual.pdf", "sha256": SHA, "page_count": True}, {"file_name": "annual.pdf", "sha256": SHA, "page_count": 0}, {"file_name": "annual.pdf", "sha256": SHA, "page_count": "100"}])
def test_invalid_material_manifest_blocks_before_scoring(spec):
    with pytest.raises(ValueError):
        rank_candidates(QUERY, candidates(), lambda pairs: pytest.fail("不应调用"), TICKER, spec)


@pytest.mark.parametrize("items", [None, {}, "sources", iter([]), [None], ["source"], [[1]]])
def test_invalid_candidate_shape(items):
    with pytest.raises(ValueError):
        rank(items, lambda pairs: pytest.fail("不应调用")) if items is not None else rank_candidates(QUERY, items, lambda pairs: [], TICKER, material())


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), {1: "non-string key"}, {"tuple": (1, 2)}, {"set": {1}}, object()])
def test_noncanonical_json_rejected_before_scorer_and_fingerprinting(bad_value):
    items = candidates()
    items[0]["audit_extra"] = bad_value
    with pytest.raises(ValueError, match="invalid_candidate_json"):
        rank(items, lambda pairs: pytest.fail("不应调用"))
    with pytest.raises(ValueError, match="invalid_candidate_json"):
        candidate_fingerprint(items)


def test_cyclic_input_fails_closed():
    items = candidates()
    items[0]["cycle"] = items
    with pytest.raises(ValueError, match="invalid_candidate_json"):
        rank(items)


@pytest.mark.parametrize("attack", ["content", "metadata", "audit", "reorder", "append", "tail", "nonjson", "manifest"])
def test_malicious_scorer_mutation_detected_for_entire_input_pool(attack):
    items, spec = candidates(49), material()

    def scorer(pairs):
        if attack == "content":
            items[0]["content"] = "tampered"
        elif attack == "metadata":
            items[0]["metadata"]["page"] = "90"
        elif attack == "audit":
            items[0]["ranking_evidence"]["fields"].append("tampered")
        elif attack == "reorder":
            items.reverse()
        elif attack == "append":
            items.append(deepcopy(items[0]))
        elif attack == "tail":
            items[-1]["content"] = "tail tampered"
        elif attack == "nonjson":
            items[0]["metadata"] = object()
        else:
            spec["sha256"] = "b" * 64
        return [0.0] * len(pairs)

    with pytest.raises(ValueError, match="scorer_modified_inputs"):
        rank(items, scorer, spec)


@pytest.mark.parametrize("bad_scores", [False, True])
def test_mutation_check_runs_on_exception_and_bad_score_exit(bad_scores):
    items = candidates()

    def scorer(pairs):
        items[0]["content"] = "tampered"
        if bad_scores:
            return [float("nan")] * len(pairs)
        raise RuntimeError("model failed after tampering")

    with pytest.raises(ValueError, match="scorer_modified_inputs"):
        rank(items, scorer)


def test_pairs_do_not_expose_mutable_candidate_references():
    def scorer(pairs):
        pairs[0][1] = "tampered"
    items = candidates()
    before = deepcopy(items)
    with pytest.raises(ValueError, match="scorer_failed"):
        rank(items, scorer)
    assert items == before


def test_missing_source_sha_uses_external_material_binding_without_rewriting_sources():
    items = candidates()
    for source in items:
        del source["metadata"]["source_sha256"]
    before = deepcopy(items)
    result = rank(items)
    assert result["sources"] == list(reversed(before))[:4]
    assert all("source_sha256" not in source["metadata"] for source in result["sources"])
    assert items == before


def test_numpy_float32_one_dimensional_scores_and_shape_fail_closed():
    np = pytest.importorskip("numpy")
    result = rank(scorer=lambda pairs: np.array([1, 6, 2, 5, 3, 4], dtype=np.float32))
    assert result["ranked_indices"] == [1, 3, 5, 4, 2, 0]
    assert result["scores"] == [6.0, 5.0, 4.0, 3.0]
    for bad in [np.array(1.0), np.ones((6, 1), dtype=np.float32), np.ones(5), np.ones(6, dtype=np.bool_), np.full(6, np.nan)]:
        with pytest.raises(ValueError):
            rank(scorer=lambda pairs: bad)