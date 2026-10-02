"""融合精排只复用同模型同问题同正文的分数，不把缓存误当新模型推理。"""

import copy
import math

import pytest

from investment_assistant.fused_rerank_eval import ExactCachedScorer, _identity


def _prior():
    candidates = [
        {"content": "第一条原文", "metadata": {"ticker": "AAPL", "file_name": "a.pdf", "page": 1}},
        {"content": "第二条原文", "metadata": {"ticker": "AAPL", "file_name": "a.pdf", "page": 2}},
    ]
    snapshot = {"cases": [{"id": "x", "question": "问题", "fingerprint": "bound", "candidates": candidates}]}
    scores = {"results": {"diagnostics": [{"id": "x", "candidate_fingerprint": "bound",
                                           "ranking": {"candidate_scores": [0.3, 0.5]}}]}}
    return snapshot, scores


class Recorder:
    def __init__(self):
        self.calls = []
        self.last_stats = {"inference_ms": 1}

    def __call__(self, pairs):
        self.calls.append(list(pairs))
        return [1.2] * len(pairs)


def test_cache_only_matches_exact_query_and_content():
    snapshot, scores = _prior()
    model = Recorder()
    cached = ExactCachedScorer(snapshot, scores, model)
    pairs = (("问题", "第一条原文"), ("问题", "第三条新原文"), ("其他问题", "第一条原文"))
    result = cached(pairs)
    assert result == [0.3, 1.2, 1.2]
    assert model.calls == [[("问题", "第三条新原文"), ("其他问题", "第一条原文")]]
    assert cached.last_stats["reused_pairs"] == 1
    assert cached.last_stats["fresh_pairs"] == 2
    assert cached(pairs) == result
    assert len(model.calls) == 1


@pytest.mark.parametrize("mutation,error", [
    (lambda s,r: r["results"]["diagnostics"][0].update(candidate_fingerprint="bad"), "prior_score_binding"),
    (lambda s,r: r["results"]["diagnostics"][0]["ranking"].update(candidate_scores=[0.1]), "prior_score_length"),
    (lambda s,r: r["results"]["diagnostics"][0]["ranking"].update(candidate_scores=[float("nan"),0.1]), "prior_score_nonfinite"),
])
def test_prior_score_tampering_blocks(mutation,error):
    snapshot, scores = _prior()
    mutation(snapshot,scores)
    with pytest.raises(ValueError, match=error):
        ExactCachedScorer(snapshot,scores,Recorder())


def test_fresh_model_failure_never_reuses_wrong_score():
    snapshot,scores=_prior()
    class Failing:
        last_stats={}
        def __call__(self,pairs):
            raise RuntimeError("model down")
    cached=ExactCachedScorer(snapshot,scores,Failing())
    with pytest.raises(RuntimeError, match="model down"):
        cached((("问题","第一条原文"),("问题","新块")))


def test_identity_keeps_ticker_and_file():
    a={"metadata":{"ticker":"AAPL","file_name":"a.pdf","page":5}}
    b={"metadata":{"ticker":"MSFT","file_name":"m.pdf","page":5}}
    assert _identity(a)!=_identity(b)
