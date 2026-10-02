"""多语token预算窗口检索隔离实验：防止中文字数与token数不一致造成截断。"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader

from .multilingual_evidence_retrieval import EvidenceUnit, MultilingualEvidenceRetriever
from .financial_table_rows import candidate_numeric_rows

TOKEN_BUDGET = 112
OVERLAP_SEGMENTS = 1


def split_token_windows(
    text: str,
    tokenizer: Any,
    *,
    token_budget: int = TOKEN_BUDGET,
    overlap_segments: int = OVERLAP_SEGMENTS,
) -> list[tuple[int, int, str]]:
    """将同页物理行按真实token数打包；超长行二分切片，保留原始行号。"""
    if type(token_budget) is not int or token_budget < 16:
        raise ValueError("invalid_token_budget")
    if type(overlap_segments) is not int or overlap_segments < 0 or overlap_segments > 2:
        raise ValueError("invalid_overlap_segments")
    if type(text) is not str:
        raise ValueError("invalid_page_text")

    def token_count(value: str) -> int:
        ids = tokenizer(value, add_special_tokens=True, truncation=False)["input_ids"]
        if not isinstance(ids, list):
            raise ValueError("invalid_tokenizer_output")
        return len(ids)

    pieces: list[tuple[int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            continue
        remaining = line
        while remaining:
            if token_count(remaining) <= token_budget:
                pieces.append((line_no, remaining))
                break
            lo, hi = 1, len(remaining)
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if token_count(remaining[:mid]) <= token_budget:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if not best:
                raise ValueError("tokenizer_cannot_fit_character")
            pieces.append((line_no, remaining[:best]))
            remaining = remaining[best:]

    windows: list[tuple[int, int, str]] = []
    start = 0
    while start < len(pieces):
        end = start + 1
        while end < len(pieces) and token_count("\n".join(p[1] for p in pieces[start:end + 1])) <= token_budget:
            end += 1
        content = "\n".join(p[1] for p in pieces[start:end])
        if token_count(content) > token_budget:
            raise ValueError("window_token_budget_violation")
        windows.append((pieces[start][0], pieces[end-1][0], content))
        if end >= len(pieces):
            break
        start = max(start + 1, end - overlap_segments)
    return windows


class TokenWindowRetriever(MultilingualEvidenceRetriever):
    """复用页级语义评分与证据包协议，但索引改为实测token预算窗口。"""

    def __init__(
        self,
        knowledge_dir: Path,
        materials: dict[str, dict[str, Any]],
        *,
        model_path: Path,
        device: str = "cpu",
        token_budget: int = TOKEN_BUDGET,
        overlap_segments: int = OVERLAP_SEGMENTS,
        pack_units: int = 3,
        encode_batch_size: int = 128,
    ) -> None:
        if type(pack_units) is not int or pack_units < 1 or pack_units > 5:
            raise ValueError("invalid_pack_units")
        if type(encode_batch_size) is not int or encode_batch_size < 1 or encode_batch_size > 256:
            raise ValueError("invalid_encode_batch_size")
        if not model_path.is_dir():
            raise ValueError("model_snapshot_missing")
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(str(model_path), local_files_only=True, device=device)
        if token_budget > int(getattr(self.model, "max_seq_length", 0)) - 2:
            raise ValueError("token_budget_exceeds_model_max_seq")
        self.knowledge_dir = knowledge_dir
        self.materials = materials
        self.model_name = str(model_path)
        self.max_chars = None
        self.overlap_lines = None
        self.pack_units = pack_units
        self.token_budget = token_budget
        self.overlap_segments = overlap_segments
        self.encode_batch_size = encode_batch_size
        self.units_by_ticker: dict[str, dict[int, list[EvidenceUnit]]] = {}
        self.pages_by_ticker: dict[str, dict[int, str]] = {}
        self.unit_list_by_ticker: dict[str, list[EvidenceUnit]] = {}
        self.embeddings_by_ticker: dict[str, np.ndarray] = {}
        self.table_rows_by_ticker: dict[str, dict[int, list[dict[str, Any]]]] = {}
        for ticker, spec in materials.items():
            if spec.get("ticker", ticker) != ticker:
                raise ValueError(f"material_ticker_binding_invalid:{ticker}")
            path = knowledge_dir / spec["file_name"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
                raise ValueError(f"material_sha256_drift:{ticker}")
            reader = PdfReader(str(path))
            units_by_page = {}
            pages = {}
            table_rows: dict[int, list[dict[str, Any]]] = {}
            for page_no, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                pages[page_no] = text
                table_rows[page_no] = candidate_numeric_rows(text, page=page_no)
                windows = split_token_windows(text, self.model.tokenizer,
                                              token_budget=token_budget, overlap_segments=overlap_segments)
                units_by_page[page_no] = [EvidenceUnit(ticker, spec["file_name"], spec["sha256"],
                                                      page_no, unit_index, line_start, line_end, content)
                                          for unit_index, (line_start, line_end, content) in enumerate(windows)]
            flat = [unit for group in units_by_page.values() for unit in group]
            if not flat:
                raise ValueError(f"empty_units:{ticker}")
            vectors = self.model.encode([unit.content for unit in flat], batch_size=self.encode_batch_size,
                                        normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
            matrix = np.asarray(vectors, dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[0] != len(flat) or not np.isfinite(matrix).all():
                raise ValueError(f"invalid_index_embedding:{ticker}")
            self.units_by_ticker[ticker] = units_by_page
            self.pages_by_ticker[ticker] = pages
            self.unit_list_by_ticker[ticker] = flat
            self.embeddings_by_ticker[ticker] = matrix
            self.table_rows_by_ticker[ticker] = table_rows

    def corpus_stats(self) -> dict[str, Any]:
        stats = super().corpus_stats()
        stats.pop("window_chars")
        stats.pop("overlap_lines")
        stats["token_budget"] = self.token_budget
        stats["overlap_segments"] = self.overlap_segments
        stats["encode_batch_size"] = self.encode_batch_size
        stats["table_numeric_rows"] = {ticker: sum(len(rows) for rows in pages.values())
                                      for ticker, pages in self.table_rows_by_ticker.items()}
        return stats
