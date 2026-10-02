"""按token预算切块，不把中文字符数误作模型可读token数。"""

import pytest
from investment_assistant.multilingual_token_retrieval import split_token_windows


class FakeTokenizer:
    def __call__(self, text, *, add_special_tokens, truncation):
        assert add_special_tokens and not truncation
        return {"input_ids": [0, *list(text), 1]}


def test_long_chinese_line_splits_without_repeating_full_line():
    text = "甲" * 45
    windows = split_token_windows(text, FakeTokenizer(), token_budget=18, overlap_segments=0)
    assert len(windows) == 3
    assert all(len(FakeTokenizer()(w[2], add_special_tokens=True, truncation=False)["input_ids"]) <= 18 for w in windows)
    assert "".join(w[2] for w in windows) == text
    assert all(w[0] == w[1] == 1 for w in windows)


def test_normal_lines_preserve_provenance_and_budget():
    raw = "项目 2025\n收入 100\n成本 50\n利润 50"
    windows = split_token_windows(raw, FakeTokenizer(), token_budget=18, overlap_segments=1)
    assert windows
    assert all(1 <= a <= b <= 4 for a,b,_ in windows)
    assert all(len(FakeTokenizer()(x, add_special_tokens=True, truncation=False)["input_ids"]) <= 18 for _,_,x in windows)
    assert "收入 100" in "\n".join(x for _,_,x in windows)


def test_invalid_budget_and_text_block():
    with pytest.raises(ValueError):
        split_token_windows("x", FakeTokenizer(), token_budget=3)
    with pytest.raises(ValueError):
        split_token_windows(None, FakeTokenizer())
