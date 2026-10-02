"""中文官方年报问答服务层。

该模块只负责中文年报语料的加载、同标的检索、证据身份校验和回答/拒答，
不改动默认报告生成链路。模型与语料均在首次请求时加载，失败时显式返回服务不可用。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader

from .chinese_answer_e2e_eval import (
    FIELD_ALIASES as EVAL_FIELD_ALIASES,
    _extract_answer,
    _field_line_matches,
    _lexical_rank,
    NUMBER_RE,
    _norm,
    _should_refuse,
)
from .config import DATA_DIR, KNOWLEDGE_DIR
from .multilingual_evidence_retrieval import MODEL_NAME, MODEL_REVISION

MODEL_SNAPSHOT = Path.home() / ".cache/huggingface/hub" / f"models--{MODEL_NAME.replace('/', '--')}" / "snapshots" / MODEL_REVISION
MANIFEST_PATH = DATA_DIR / "materials_manifest.json"
TOP_K = 4
CANDIDATE_BUDGET = 48
SEMANTIC_BUDGET = 16

# 只把已经核验为中文官方年报的标的暴露给该服务；英文资料仍由原有链路负责。
CHINESE_TICKERS = frozenset({"600519.SS", "300750.SZ", "000001.SZ", "000858.SZ"})
COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "600519.SS": ("贵州茅台", "茅台"),
    "300750.SZ": ("宁德时代",),
    "000001.SZ": ("平安银行",),
    "000858.SZ": ("五粮液",),
}
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d{6}\.(?:SZ|SS)|\d{4}\.HK)(?![A-Za-z0-9])", re.IGNORECASE)

# 复用评测中的字段规则，并补齐业务入口最常见的年报字段。
FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(dict.fromkeys(
    list(EVAL_FIELD_ALIASES) + [
        ("营业收入", ("营业收入", "营业总收入")),
        ("归属于上市公司股东的净利润", ("归属于上市公司股东的净利润", "归母净利润")),
        ("净利润", ("净利润",)),
        ("经营活动产生的现金流量净额", ("经营活动产生的现金流量净额", "经营活动现金流量净额")),
        ("经营活动产生的现金", ("经营活动产生的现金",)),
        ("研发费用", ("研发费用",)),
        ("研发投入金额", ("研发投入金额", "研发投入")),
        ("销售费用", ("销售费用",)),
        ("资产总额", ("资产总额", "资产合计")),
        ("合同负债", ("合同负债",)),
        ("吸收存款本金", ("吸收存款本金",)),
        ("核心一级资本充足率", ("核心一级资本充足率",)),
        ("净息差", ("净息差",)),
    ],
))


class KnowledgeServiceError(Exception):
    """中文年报服务的稳定错误基类。"""


class KnowledgeInvalidInput(KnowledgeServiceError):
    """请求参数不满足服务协议。"""


class KnowledgeTickerNotFound(KnowledgeServiceError):
    """请求标的不在中文官方年报语料中。"""


class KnowledgeCorpusUnavailable(KnowledgeServiceError):
    """语料、模型或版本绑定不可用。"""


def _resolve_field(question: str) -> tuple[str | None, tuple[str, ...]]:
    normalized = _norm(question)
    matches = [
        (field, aliases) for field, aliases in FIELD_ALIASES
        if any(_norm(alias) in normalized for alias in aliases)
    ]
    if not matches:
        return None, ()
    return max(matches, key=lambda item: max(len(_norm(alias)) for alias in item[1]))


def _clean_question(question: str) -> str:
    if not isinstance(question, str):
        raise KnowledgeInvalidInput("question_type_invalid")
    normalized = unicodedata.normalize("NFKC", question).strip()
    if not normalized:
        raise KnowledgeInvalidInput("question_empty")
    if len(normalized) > 500:
        raise KnowledgeInvalidInput("question_too_long")
    if any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in normalized):
        raise KnowledgeInvalidInput("question_control_character")
    return normalized


def _clean_ticker(ticker: str) -> str:
    if not isinstance(ticker, str):
        raise KnowledgeInvalidInput("ticker_type_invalid")
    normalized = unicodedata.normalize("NFKC", ticker).strip().upper()
    if not re.fullmatch(r"\d{6}\.(?:SZ|SS)|\d{4}\.HK", normalized):
        raise KnowledgeInvalidInput("ticker_format_invalid")
    return normalized


def _resolve_foreign_reference(question: str, ticker: str) -> str | None:
    for token in TICKER_RE.findall(question.upper()):
        if token != ticker:
            return token
    for candidate, aliases in COMPANY_ALIASES.items():
        if candidate == ticker:
            continue
        if any(alias in question for alias in aliases):
            return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public_source(source: dict[str, Any], *, citation: str) -> dict[str, Any]:
    metadata = source.get("metadata") or {}
    identity = {
        "ticker": metadata.get("ticker"),
        "file_name": metadata.get("file_name"),
        "source_sha256": metadata.get("source_sha256"),
        "page": metadata.get("page"),
    }
    return {
        "citation": citation,
        "identity": identity,
        "excerpt": str(source.get("excerpt") or source.get("content") or "")[:1600],
    }


class ChineseKnowledgeAnswerService:
    """面向 API 的中文年报问答服务；所有检索都在请求 ticker 的资料子集内执行。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded = False
        self._model: Any = None
        self._pages_by_ticker: dict[str, list[dict[str, Any]]] = {}
        self._materials: dict[str, dict[str, Any]] = {}
        self._corpus_version = ""

    def _load_corpus(self) -> None:
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KnowledgeCorpusUnavailable("knowledge_manifest_unavailable") from exc
        if not isinstance(manifest, dict):
            raise KnowledgeCorpusUnavailable("knowledge_manifest_invalid")

        materials: dict[str, dict[str, Any]] = {}
        pages_by_ticker: dict[str, list[dict[str, Any]]] = {}
        for ticker in sorted(CHINESE_TICKERS):
            entry = manifest.get(ticker)
            validation = (entry or {}).get("validation") if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(validation, dict):
                raise KnowledgeCorpusUnavailable(f"material_manifest_missing:{ticker}")
            if entry.get("page_authority") != "official" or entry.get("report_date") != "2025":
                raise KnowledgeCorpusUnavailable(f"material_not_official:{ticker}")
            file_name = entry.get("file_name")
            source_sha = validation.get("sha256")
            page_count = validation.get("page_count")
            if (not isinstance(file_name, str) or Path(file_name).name != file_name or file_name in {".", ".."}
                    or not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
                    or type(page_count) is not int or page_count < 1):
                raise KnowledgeCorpusUnavailable(f"material_manifest_invalid:{ticker}")
            path = KNOWLEDGE_DIR / file_name
            if not path.is_file() or _sha256(path) != source_sha:
                raise KnowledgeCorpusUnavailable(f"material_sha256_drift:{ticker}")
            try:
                reader = PdfReader(str(path))
            except Exception as exc:
                raise KnowledgeCorpusUnavailable(f"material_pdf_invalid:{ticker}") from exc
            if len(reader.pages) != page_count:
                raise KnowledgeCorpusUnavailable(f"material_page_count_drift:{ticker}")
            spec = {"ticker": ticker, "file_name": file_name, "sha256": source_sha, "page_count": page_count, "path": path}
            materials[ticker] = spec
            pages: list[dict[str, Any]] = []
            for page_no, page in enumerate(reader.pages, 1):
                content = page.extract_text() or ""
                pages.append({
                    "content": content,
                    "lines": [line.strip() for line in content.splitlines() if line.strip()],
                    "metadata": {
                        "ticker": ticker,
                        "file_name": file_name,
                        "source_sha256": source_sha,
                        "page": page_no,
                    },
                })
            pages_by_ticker[ticker] = pages

        self._materials = materials
        self._pages_by_ticker = pages_by_ticker
        self._corpus_version = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()

    def _load_model(self) -> None:
        if not MODEL_SNAPSHOT.is_dir():
            raise KnowledgeCorpusUnavailable("multilingual_model_snapshot_missing")
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(str(MODEL_SNAPSHOT), local_files_only=True, device="cpu")
        except Exception as exc:
            raise KnowledgeCorpusUnavailable(f"multilingual_model_unavailable:{type(exc).__name__}") from exc

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_corpus()
            self._load_model()
            self._loaded = True

    def _retrieve(self, question: str, ticker: str, field: str, aliases: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pages = self._pages_by_ticker[ticker]
        ranked = _lexical_rank(pages, aliases, question, field)
        candidates = [page for page in ranked[:CANDIDATE_BUDGET] if page.get("excerpt")]
        head = candidates[:SEMANTIC_BUDGET]
        if not head:
            return [], candidates
        try:
            vectors = np.asarray(
                self._model.encode(
                [question] + [page["excerpt"] for page in head],
                batch_size=32,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
                dtype=np.float32,
            )
        except Exception as exc:
            raise KnowledgeCorpusUnavailable("semantic_retrieval_failed") from exc
        if vectors.ndim != 2 or vectors.shape[0] != len(head) + 1 or not np.isfinite(vectors).all():
            raise KnowledgeCorpusUnavailable("invalid_semantic_vectors")
        similarities = vectors[1:] @ vectors[0]
        ordered = sorted(
            enumerate(head),
            key=lambda item: (
                -(item[1]["lexical_score"] + float(similarities[item[0]]) * 0.5),
                item[1]["metadata"]["page"],
            ),
        )
        return [item for _, item in ordered[:TOP_K]], candidates

    def _verify_current_corpus(self, ticker: str) -> None:
        # 索引启动后源文件或清单变化时拒绝交付，避免旧索引附上新文件的 SHA。
        if _sha256(MANIFEST_PATH) != self._corpus_version:
            raise KnowledgeCorpusUnavailable("material_manifest_drift")
        spec = self._materials[ticker]
        if _sha256(spec["path"]) != spec["sha256"]:
            raise KnowledgeCorpusUnavailable("material_sha256_drift")

    def _source_identity(self, source: dict[str, Any], ticker: str) -> bool:
        metadata = source.get("metadata") or {}
        spec = self._materials.get(ticker) or {}
        return (
            metadata.get("ticker") == ticker
            and metadata.get("file_name") == spec.get("file_name")
            and metadata.get("source_sha256") == spec.get("sha256")
            and isinstance(metadata.get("page"), int)
            and 1 <= metadata["page"] <= int(spec.get("page_count", 0))
        )

    def answer(self, ticker: str, question: str, *, requested_by: str) -> dict[str, Any]:
        normalized_ticker = _clean_ticker(ticker)
        normalized_question = _clean_question(question)
        if normalized_ticker not in CHINESE_TICKERS:
            raise KnowledgeTickerNotFound("chinese_material_not_found")
        foreign = _resolve_foreign_reference(normalized_question, normalized_ticker)
        if foreign is not None:
            return {
                "status": "refused",
                "error_code": "CROSS_TICKER_QUERY",
                "ticker": normalized_ticker,
                "question": normalized_question,
                "answer": "问题同时涉及其他标的，系统拒绝跨标的混答。请一次只询问一个标的。",
                "reason": f"foreign_reference:{foreign}",
                "field": None,
                "sources": [],
                "evidence": None,
                "limitations": ["当前接口只在请求 ticker 对应的中文官方年报资料范围内回答。"],
                "requested_by": requested_by,
            }

        years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", normalized_question))
        if years != {"2025"}:
            return {
                "status": "refused", "error_code": "REPORT_PERIOD_UNSUPPORTED",
                "ticker": normalized_ticker, "question": normalized_question,
                "answer": "当前接口仅核验2025年官方年报；请明确询问2025年单一期间事实。",
                "reason": "period_unverified", "field": None, "sources": [], "evidence": None,
                "limitations": ["不跨年度推断，也不把旧报告作为实时资料。"],
                "requested_by": requested_by,
            }
        field, aliases = _resolve_field(normalized_question)
        refusal_reason = _should_refuse(normalized_question, field)
        if refusal_reason:
            return {
                "status": "refused",
                "error_code": "OUT_OF_SCOPE",
                "ticker": normalized_ticker,
                "question": normalized_question,
                "answer": "当前证据边界不足，无法可靠回答该问题。",
                "reason": refusal_reason,
                "field": field,
                "sources": [],
                "evidence": None,
                "limitations": ["只回答中文官方年报中可核验的字段事实，不提供预测或投资建议。"],
                "requested_by": requested_by,
            }

        if field is None:
            return {
                "status": "refused",
                "error_code": "INSUFFICIENT_EVIDENCE",
                "ticker": normalized_ticker,
                "question": normalized_question,
                "answer": "当前中文官方年报语料中没有可核验的字段证据，系统拒绝猜测。",
                "reason": "field_not_supported",
                "field": None,
                "sources": [],
                "evidence": None,
                "limitations": ["请询问年报中的已支持字段，例如营业收入、净利润、货币资金或研发费用。"],
                "requested_by": requested_by,
            }

        self._ensure_loaded()
        self._verify_current_corpus(normalized_ticker)
        sources, candidates = self._retrieve(normalized_question, normalized_ticker, field, aliases)
        if any(not self._source_identity(source, normalized_ticker) for source in sources + candidates):
            raise KnowledgeCorpusUnavailable("cross_ticker_or_version_source_detected")
        public_sources = [_public_source(source, citation=f"S{index + 1}") for index, source in enumerate(sources)]
        value, evidence = _extract_answer(sources, field, aliases, normalized_question)
        if (value is None or evidence is None or not self._evidence_identity(evidence, normalized_ticker)
                or not self._value_line_verified(sources, evidence, aliases, value)):
            return {
                "status": "refused",
                "error_code": "INSUFFICIENT_EVIDENCE",
                "ticker": normalized_ticker,
                "question": normalized_question,
                "answer": "当前报告没有找到可核验的字段与数值证据，系统未返回猜测结果。",
                "reason": "top4_evidence_or_column_unverified",
                "field": field,
                "sources": public_sources,
                "evidence": None,
                "retrieval": {"top_k": TOP_K, "candidate_count": len(candidates), "corpus_version": self._corpus_version},
                "limitations": ["字段、数值与2025年列或单位无法绑定时拒答；复杂表格仍需人工复核。"],
                "requested_by": requested_by,
            }

        evidence_public = {
            "identity": {
                "ticker": evidence["metadata"]["ticker"],
                "file_name": evidence["metadata"]["file_name"],
                "source_sha256": evidence["metadata"]["source_sha256"],
                "page": evidence["metadata"]["page"],
            },
            "excerpt": evidence.get("excerpt", "")[:1600],
        }
        return {
            "status": "answered",
            "error_code": None,
            "ticker": normalized_ticker,
            "question": normalized_question,
            "answer": f"{field}为 {value}。",
            "reason": None,
            "field": field,
            "sources": public_sources,
            "evidence": evidence_public,
            "retrieval": {"top_k": TOP_K, "candidate_count": len(candidates), "corpus_version": self._corpus_version},
            "limitations": ["回答仅适用于所引2025年年报中可定位的字段数值行，不构成投资建议；复杂表格仍需人工复核。"],
            "requested_by": requested_by,
        }

    def _value_line_verified(self, sources: list[dict[str, Any]], evidence: dict[str, Any], aliases: tuple[str, ...], value: str) -> bool:
        """回答只接受字段后首列数值及可定位2025列；多期间/单位无表头时拒答。"""
        metadata = evidence["metadata"]
        source = next((item for item in sources if item.get("metadata") == metadata), None)
        if source is None:
            return False
        lines = source.get("lines") or []
        for index, line in enumerate(lines):
            normalized = _norm(line)
            if not _field_line_matches(line, None, aliases):
                continue
            starts = [normalized.find(_norm(alias)) for alias in aliases if _norm(alias) in normalized]
            if not starts:
                continue
            start = max(starts)
            suffix = normalized[start + max(len(_norm(alias)) for alias in aliases if _norm(alias) in normalized):]
            numbers = list(NUMBER_RE.finditer(suffix))
            if not numbers or numbers[0].group().replace(" ", "") != value:
                continue
            # 叙述句在同一行明确2025年的比例可以核验；表格需前置的2025首列表头。
            if re.search(r"2025\s*年", normalized[:start]) and value.endswith("%"):
                return True
            header = " ".join(lines[max(0, index - 35):index])
            if not re.search(r"2025\s*(?:年|年度|期末)|期末余额|本期金额", header):
                continue
            if value.endswith("%"):
                return True
            # 对金额仅支持可见单位；多单位页面应交由人工审核。
            unit_headers = [part for part in lines[max(0, index - 35):index]
                            if "单位" in part and any(unit in part for unit in ("千元", "百万元", "人民币元", "单位：元", "单位:元"))]
            if unit_headers:
                return True
        return False

    def _evidence_identity(self, evidence: dict[str, Any], ticker: str) -> bool:
        return self._source_identity(evidence, ticker) and bool(str(evidence.get("excerpt") or "").strip())
