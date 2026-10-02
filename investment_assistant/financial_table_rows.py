"""保留财报表格断行的原文和候选行，绝不自动断言期间/数值归属。"""
from __future__ import annotations

import re
from typing import Any

_NUMBER = re.compile(r"(?<![\d,.])\(?[-+]?\d[\d,]*(?:\.\d+)?\)?(?![\d,.])")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_TRAILING_FRAGMENT = re.compile(r"^([\u4e00-\u9fff、，]{1,6})\s+((?:\(?[-+]?\d[\d,.]*\)?\s*)+)$")


def candidate_numeric_rows(page_text: str, *, page: int) -> list[dict[str, Any]]:
    """生成带原始行号的行候选；断行复原是派生视图，需人工核对期间和列。"""
    if type(page) is not int or page < 1:
        raise ValueError("invalid_page")
    if type(page_text) is not str:
        raise ValueError("invalid_page_text")
    lines = [(i, line) for i, line in enumerate(page_text.splitlines(), 1) if line.strip()]
    rows: list[dict[str, Any]] = []
    for position, (line_no, line) in enumerate(lines):
        current = line.strip()
        if not _NUMBER.search(current):
            continue
        original = [(line_no, line)]
        label = current[:_NUMBER.search(current).start()].strip()
        wrapped = False
        if position and len(label) <= 6 and _CJK.search(label):
            match = _TRAILING_FRAGMENT.fullmatch(current)
            prev_no, prev_line = lines[position - 1]
            previous = prev_line.strip()
            if (match and prev_no == line_no - 1 and 4 <= len(previous) <= 60
                and _CJK.search(previous) and not _NUMBER.search(previous)
                and previous[-1] not in "：:。！？;；"):
                label = previous + match.group(1)
                original.insert(0, (prev_no, prev_line))
                wrapped = True
        numbers = [m.group() for m in _NUMBER.finditer(current)]
        rows.append({"page": page, "label_candidate": label, "number_tokens": numbers,
                     "raw_lines": [{"line": no, "text": text} for no, text in original],
                     "derived_from_wrapped_label": wrapped,
                     "review_required": True,
                     "period_column_verified": False})
    return rows
