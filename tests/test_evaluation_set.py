from investment_assistant.evaluation import _load_cases


def test_apple_10k_evaluation_set_has_page_and_keyword_labels():
    cases = _load_cases()
    assert 15 <= len(cases) <= 20
    for case in cases:
        assert case["question"]
        assert case["target_pages"]
        assert case["keywords"]
