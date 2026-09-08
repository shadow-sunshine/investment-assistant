from investment_assistant.golden_regression import check_golden_sample


def test_golden_sample_regression_passes():
    outcome = check_golden_sample()
    assert outcome["passed"]
    assert outcome["safety"]["passed"]
    assert outcome["snapshot_attribution"]["passed"]
    assert outcome["rag_attribution"]["passed"]
