"""Deterministic issuer-relevance filter for news evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import DATA_DIR

ALIAS_PATH = DATA_DIR / "ticker_aliases.json"
REJECTION_REASON = "\u6807\u9898\u4e0e\u6458\u8981\u5747\u672a\u51fa\u73b0\u4e3b\u4f53\u8bc6\u522b\u540d"


def _normalized(value: str) -> str:
    return value.casefold()


def load_ticker_aliases(path: Path = ALIAS_PATH) -> dict[str, list[str]]:
    """Load the reviewable alias dictionary; invalid data never triggers name guessing."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(ticker).upper().strip(): [str(alias).strip() for alias in aliases if str(alias).strip()]
        for ticker, aliases in raw.items()
        if isinstance(aliases, list)
    }


def subject_aliases(ticker: str, path: Path = ALIAS_PATH) -> list[str]:
    """Return ticker, suffix-free code, and configured aliases for literal matching."""
    normalized_ticker = ticker.upper().strip()
    aliases = [normalized_ticker]
    if "." in normalized_ticker:
        aliases.append(normalized_ticker.split(".", maxsplit=1)[0])
    aliases.extend(load_ticker_aliases(path).get(normalized_ticker, []))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def filter_news_records(records: list[dict[str, Any]], ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Audit every input item and return only issuer-matched items as evidence candidates."""
    aliases = subject_aliases(ticker)
    passed: list[dict[str, Any]] = []
    audited: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        searchable = _normalized(f"{title}\n{summary}")
        matched_aliases = [alias for alias in aliases if _normalized(alias) in searchable]
        item["filter"] = {
            "passed": bool(matched_aliases),
            "reason": None if matched_aliases else REJECTION_REASON,
            "matched_aliases": matched_aliases,
        }
        audited.append(item)
        if matched_aliases:
            passed.append(item)
    return passed, audited
