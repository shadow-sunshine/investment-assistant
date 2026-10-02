"""多语召回与页内证据窗口实验；不改默认 Chroma/RAG 路径。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader

from .hybrid_retrieval import FINANCIAL_GLOSSARY, _intent

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
DEFAULT_WINDOW_CHARS = 420
DEFAULT_OVERLAP_LINES = 1
DEFAULT_PACK_UNITS = 3


@dataclass(frozen=True)
class EvidenceUnit:
    """保留页码与原始行范围的最小证据单元；不跨页拼接。"""

    ticker: str
    file_name: str
    source_sha256: str
    page: int
    unit_index: int
    line_start: int
    line_end: int
    content: str

    def metadata(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "file_name": self.file_name,
            "source_sha256": self.source_sha256,
            "source_type": "pdf",
            "page": self.page,
            "unit_index": self.unit_index,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "source_id": (f"{self.ticker}|{self.file_name}|{self.source_sha256}|"
                          f"page={self.page}|unit={self.unit_index}"),
        }

    def as_source(self, content: str | None = None, score: float | None = None) -> dict[str, Any]:
        source = {"content": self.content if content is None else content, "metadata": self.metadata()}
        if score is not None:
            source["semantic_score"] = float(score)
        return source


def _clean_lines(text: str) -> list[str]:
    return [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip()]


def split_evidence_windows(
    text: str,
    max_chars: int = DEFAULT_WINDOW_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[tuple[int, int, str]]:
    """按原始换行组成窗口；超长单行才按字符拆分，窗口不跨页。"""
    if type(max_chars) is not int or max_chars < 80:
        raise ValueError("invalid_max_chars")
    if type(overlap_lines) is not int or overlap_lines < 0 or overlap_lines > 3:
        raise ValueError("invalid_overlap_lines")
    lines = _clean_lines(text)
    if not lines:
        return []
    windows: list[tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            line = lines[end]
            add = len(line) + (1 if end > start else 0)
            if end > start and size + add > max_chars:
                break
            if end == start and len(line) > max_chars:
                for offset in range(0, len(line), max_chars):
                    piece = line[offset:offset + max_chars]
                    windows.append((start + 1, start + 1, piece))
                end += 1
                size = len(line)
                break
            size += add
            end += 1
        if end <= start:
            end = start + 1
        content = "\n".join(lines[start:end])
        if content:
            windows.append((start + 1, end, content))
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap_lines)
    return windows


def build_pdf_units(
    pdf_path: Path,
    ticker: str,
    material_spec: dict[str, Any],
    *,
    max_chars: int = DEFAULT_WINDOW_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> tuple[dict[int, list[EvidenceUnit]], dict[int, str]]:
    """建立页内窗口索引，同时保留整页文本供上限诊断。"""
    if not pdf_path.is_file():
        raise ValueError(f"material_missing:{pdf_path.name}")
    file_name = material_spec.get("file_name")
    source_sha256 = material_spec.get("sha256")
    if file_name != pdf_path.name or not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise ValueError("material_binding_invalid")
    if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != source_sha256:
        raise ValueError("material_sha256_drift")
    reader = PdfReader(str(pdf_path))
    units: dict[int, list[EvidenceUnit]] = {}
    pages: dict[int, str] = {}
    for page_no, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        pages[page_no] = text
        windows = split_evidence_windows(text, max_chars=max_chars, overlap_lines=overlap_lines)
        units[page_no] = [EvidenceUnit(ticker, file_name, source_sha256, page_no, index,
                                       start, end, content)
                          for index, (start, end, content) in enumerate(windows)]
    if not any(units.values()):
        raise ValueError("empty_material_text")
    return units, pages


def _query_variants(query: str) -> list[str]:
    """仅用通用双语财务词生成语义查询变体，不读取金标或标的专名。"""
    intent = _intent(query)
    variants = [query]
    aliases: list[str] = []
    for key, terms in FINANCIAL_GLOSSARY:
        if key in intent["fields"]:
            aliases.extend(terms)
    if aliases:
        variants.append(f"{query} {' '.join(dict.fromkeys(aliases))}")
    return list(dict.fromkeys(variants))


def _stable_source_key(source: EvidenceUnit) -> tuple[int, int, str]:
    return source.page, source.unit_index, source.content


class MultilingualEvidenceRetriever:
    """本地多语语义召回 + 页内证据包；模型不可用时显式失败，不降级冒充。"""

    def __init__(
        self,
        knowledge_dir: Path,
        materials: dict[str, dict[str, Any]],
        *,
        model_name: str = MODEL_NAME,
        device: str = "cpu",
        max_chars: int = DEFAULT_WINDOW_CHARS,
        overlap_lines: int = DEFAULT_OVERLAP_LINES,
        pack_units: int = DEFAULT_PACK_UNITS,
    ) -> None:
        if type(pack_units) is not int or pack_units < 1 or pack_units > 5:
            raise ValueError("invalid_pack_units")
        self.knowledge_dir = knowledge_dir
        self.materials = materials
        self.model_name = model_name
        self.max_chars = max_chars
        self.overlap_lines = overlap_lines
        self.pack_units = pack_units
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name, local_files_only=True, device=device)
        except Exception as exc:
            raise RuntimeError(f"multilingual_model_unavailable:{type(exc).__name__}:{exc}") from exc
        self.units_by_ticker: dict[str, dict[int, list[EvidenceUnit]]] = {}
        self.pages_by_ticker: dict[str, dict[int, str]] = {}
        self.unit_list_by_ticker: dict[str, list[EvidenceUnit]] = {}
        self.embeddings_by_ticker: dict[str, np.ndarray] = {}
        for ticker, spec in materials.items():
            units, pages = build_pdf_units(knowledge_dir / spec["file_name"], ticker, spec,
                                           max_chars=max_chars, overlap_lines=overlap_lines)
            flat = [unit for page_units in units.values() for unit in page_units]
            if not flat:
                raise ValueError(f"empty_units:{ticker}")
            vectors = self.model.encode([unit.content for unit in flat], batch_size=32,
                                        normalize_embeddings=True, show_progress_bar=False,
                                        convert_to_numpy=True)
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.ndim != 2 or vectors.shape[0] != len(flat):
                raise ValueError(f"embedding_shape:{ticker}")
            self.units_by_ticker[ticker] = units
            self.pages_by_ticker[ticker] = pages
            self.unit_list_by_ticker[ticker] = flat
            self.embeddings_by_ticker[ticker] = vectors

    def _pack_page(self, page_units: list[EvidenceUnit], scores: dict[int, float]) -> tuple[str, list[dict[str, Any]]]:
        if not page_units:
            raise ValueError("empty_page_units")
        ranked = sorted(page_units, key=lambda unit: (-scores.get(unit.unit_index, -math.inf), _stable_source_key(unit)))
        anchor = ranked[0]
        selected_indices = {anchor.unit_index}
        # 优先补相邻窗口，保留表头/期间/数值的局部结构，不拼接非相邻块。
        for delta in range(1, self.pack_units):
            for index in (anchor.unit_index - delta, anchor.unit_index + delta):
                if 0 <= index < len(page_units) and len(selected_indices) < self.pack_units:
                    selected_indices.add(index)
        selected = sorted((unit for unit in page_units if unit.unit_index in selected_indices),
                          key=lambda unit: unit.unit_index)
        evidence_units = [{**unit.as_source(score=scores[unit.unit_index]),
                           "metadata": {**unit.metadata(), "selected": True}}
                          for unit in selected]
        content = "\n\n".join(unit["content"] for unit in evidence_units)
        return content, evidence_units

    def search(self, query: str, ticker: str, *, limit: int = 4, budget: int = 48) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("invalid_query")
        if ticker not in self.unit_list_by_ticker:
            raise ValueError("unknown_ticker")
        if type(limit) is not int or limit < 1 or limit > budget:
            raise ValueError("invalid_limit")
        if type(budget) is not int or budget < limit:
            raise ValueError("invalid_budget")
        variants = _query_variants(query)
        query_vectors = self.model.encode(variants, normalize_embeddings=True, show_progress_bar=False,
                                          convert_to_numpy=True)
        query_vectors = np.asarray(query_vectors, dtype=np.float32)
        units = self.unit_list_by_ticker[ticker]
        matrix = self.embeddings_by_ticker[ticker]
        if query_vectors.ndim != 2 or query_vectors.shape[0] != len(variants):
            raise ValueError("query_embedding_shape")
        if not np.isfinite(query_vectors).all() or not np.isfinite(matrix).all():
            raise ValueError("nonfinite_embedding")
        scores = np.max(matrix @ query_vectors.T, axis=1)
        page_scores: dict[int, float] = {}
        page_unit_scores: dict[int, dict[int, float]] = {}
        for unit, score in zip(units, scores.tolist()):
            if not math.isfinite(float(score)):
                raise ValueError("nonfinite_embedding_score")
            page_scores[unit.page] = max(page_scores.get(unit.page, -math.inf), float(score))
            page_unit_scores.setdefault(unit.page, {})[unit.unit_index] = float(score)
        ranked_pages = sorted(page_scores, key=lambda page: (-page_scores[page], page))
        candidates: list[dict[str, Any]] = []
        for rank, page in enumerate(ranked_pages[:budget], 1):
            page_units = self.units_by_ticker[ticker][page]
            content, evidence_units = self._pack_page(page_units, page_unit_scores[page])
            unit_ids = [unit["metadata"]["unit_index"] for unit in evidence_units]
            source = {
                "content": content,
                "metadata": {**page_units[0].metadata(), "unit_index": None,
                             "line_start": min(unit["metadata"]["line_start"] for unit in evidence_units),
                             "line_end": max(unit["metadata"]["line_end"] for unit in evidence_units),
                             "evidence_unit_indices": unit_ids},
                "semantic_score": page_scores[page],
                "semantic_rank": rank,
                "evidence_units": evidence_units,
            }
            candidates.append(source)
        sources = [{**candidate, "citation": f"S{index + 1}"} for index, candidate in enumerate(candidates[:limit])]
        return sources, candidates

    def corpus_stats(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_max_seq_length": int(getattr(self.model, "max_seq_length", 0)),
            "window_chars": self.max_chars,
            "overlap_lines": self.overlap_lines,
            "pack_units": self.pack_units,
            "tickers": {ticker: {"pages": len(self.units_by_ticker[ticker]),
                                 "units": len(self.unit_list_by_ticker[ticker]),
                                 "nonempty_pages": sum(bool(value) for value in self.units_by_ticker[ticker].values())}
                        for ticker in self.unit_list_by_ticker},
            "total_units": sum(len(value) for value in self.unit_list_by_ticker.values()),
        }
