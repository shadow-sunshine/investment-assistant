from investment_assistant.evaluation import _load_cases


def test_apple_10k_evaluation_set_has_page_and_keyword_labels():
    cases = _load_cases()
    assert 15 <= len(cases) <= 20
    for case in cases:
        assert case["question"]
        assert case["target_pages"]
        assert case["keywords"]



def test_holdout_evaluation_set_is_disjoint_from_frozen_main_set():
    from investment_assistant.evaluation import DATA_DIR

    main_cases = _load_cases()
    holdout_cases = _load_cases(DATA_DIR / "apple_10k_holdout_eval_set.json")

    assert len(holdout_cases) == 5
    assert {case["id"] for case in main_cases}.isdisjoint(case["id"] for case in holdout_cases)
    for case in holdout_cases:
        assert case["question"]
        assert case["target_pages"]
        assert case["keywords"]
