from investment_assistant.financial_table_rows import candidate_numeric_rows


def test_wrapped_chinese_label_keeps_raw_lines_and_review_flag():
    raw = "项目 2025年度 2024年度\n销售商品、提供劳务收到的现\n金 183,990,403,487.80 182,645,203,339.89"
    rows = candidate_numeric_rows(raw, page=64)
    target = next(row for row in rows if "183,990" in row["number_tokens"][0])
    assert target["label_candidate"] == "销售商品、提供劳务收到的现金"
    assert target["number_tokens"] == ["183,990,403,487.80", "182,645,203,339.89"]
    assert target["raw_lines"] == [{"line": 2, "text": "销售商品、提供劳务收到的现"},
                                    {"line": 3, "text": "金 183,990,403,487.80 182,645,203,339.89"}]
    assert target["derived_from_wrapped_label"]
    assert target["review_required"] and not target["period_column_verified"]


def test_unrelated_rows_not_claimed_as_verified_columns():
    raw = "营业收入 500 400\n：表注\n现金 50 40"
    rows = candidate_numeric_rows(raw, page=1)
    assert len(rows) == 2
    assert not rows[-1]["derived_from_wrapped_label"]
    assert all(not row["period_column_verified"] for row in rows)


def test_invalid_page_and_text_fail():
    import pytest
    with pytest.raises(ValueError):
        candidate_numeric_rows("x", page=0)
    with pytest.raises(ValueError):
        candidate_numeric_rows(None, page=1)
