"""本地持久化 RAG：文本/PDF 页码解析、混合检索与可选语义嵌入。

默认使用确定性哈希向量，保证首次运行不依赖模型下载。设置
RAG_EMBEDDING_MODE=semantic 并安装 requirements-semantic.txt 后，改用
SentenceTransformers 本地语义向量；若模型未安装或加载失败，会明确降级并记录原因。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from pypdf import PdfReader

from .config import CHROMA_DIR, DATA_DIR, KNOWLEDGE_DIR

HASH_COLLECTION_NAME = "investment_research_sources_hash_v3"
SEMANTIC_COLLECTION_NAME = "investment_research_sources_semantic_v2"
HASH_EMBEDDING_DIMENSION = 384
SEMANTIC_MODEL_NAME = os.getenv("RAG_SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
TERM_MAP_PATH = DATA_DIR / "financial_term_map.json"
MATERIALS_MANIFEST_PATH = DATA_DIR / "materials_manifest.json"
CROSS_ENCODER_MODEL_NAME = os.getenv("RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
CROSS_ENCODER_CANDIDATE_LIMIT = 30


def _load_financial_term_map() -> dict[str, list[str]]:
    """加载可审阅的中英财务术语映射；缺失或格式错误时不扩展查询。"""
    try:
        raw = json.loads(TERM_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(term): [str(variant) for variant in variants if str(variant).strip()]
        for term, variants in raw.items()
        if isinstance(variants, list)
    }


def expand_financial_query(query: str) -> str:
    """将命中的中文财务术语扩展为英文变体，供向量与字面检索共同使用。"""
    variants: list[str] = []
    lowered = query.lower()
    for term, mapped_variants in _load_financial_term_map().items():
        if term.lower() in lowered:
            variants.extend(mapped_variants)
    unique_variants = list(dict.fromkeys(variant for variant in variants if variant not in query))
    return query if not unique_variants else f"{query} {' '.join(unique_variants)}"


def _pdf_material_metadata(file_path: Path, ticker: str) -> dict[str, str]:
    """Return manifest provenance for downloaded material; unlisted PDFs remain official-pagination sources."""
    material_kind = "official_pdf"
    try:
        manifest = json.loads(MATERIALS_MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    entry = manifest.get(ticker) if isinstance(manifest, dict) else None
    if isinstance(entry, dict) and entry.get("file_name") == file_path.name:
        material_kind = str(entry.get("material_kind") or material_kind)
    page_authority = "generated" if material_kind == "official_html_converted_to_pdf" else "official"
    return {"material_kind": material_kind, "page_authority": page_authority}


def _stable_id(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


TICKER_FILENAME_ALIASES = {
    "aapl": "AAPL",
    "apple": "AAPL",
    "msft": "MSFT",
    "microsoft": "MSFT",
}
KNOWN_TICKER_CODES = {
    "600519": "600519.SS",
    "0700": "0700.HK",
}


def infer_ticker_from_filename(file_path: Path) -> str:
    """仅按文件名归属本地资料；无法确认时显式标为 unknown，绝不猜测。"""
    name = file_path.stem.lower()
    for alias, ticker in TICKER_FILENAME_ALIASES.items():
        if alias in name:
            return ticker
    for code, ticker in KNOWN_TICKER_CODES.items():
        if code in name:
            return ticker
    ticker_match = re.search(r"(?<![a-z0-9])([a-z]{1,10}|\d{6})(?:[._-](ss|sz|hk))?(?![a-z0-9])", name)
    if ticker_match:
        symbol = ticker_match.group(1).upper()
        suffix = ticker_match.group(2)
        if suffix:
            return f"{symbol}.{suffix.upper()}"
    return "unknown"


def _chunks(text: str, chunk_size: int = 850, overlap: int = 120) -> list[str]:
    clean = " ".join(text.split())
    if not clean:
        return []
    result: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + chunk_size)
        result.append(clean[start:end])
        if end == len(clean):
            break
        start = end - overlap
    return result


def _hash_embedding(text: str) -> list[float]:
    """用 token 哈希构造固定维度向量，避免运行时下载默认 ONNX 模型。"""
    vector = np.zeros(HASH_EMBEDDING_DIMENSION, dtype=np.float32)
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9._-]+", text.lower())
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % HASH_EMBEDDING_DIMENSION
        sign = 1.0 if digest[4] % 2 else -1.0
        vector[bucket] += sign
    norm = float(np.linalg.norm(vector))
    return (vector / norm).tolist() if norm else vector.tolist()


class EmbeddingProvider:
    """统一封装哈希和可选 SentenceTransformers 嵌入模式。"""

    def __init__(self) -> None:
        requested_mode = os.getenv("RAG_EMBEDDING_MODE", "hash").strip().lower()
        self.mode = "hash"
        self.model: Any | None = None
        self.fallback_reason: str | None = None
        if requested_mode == "semantic":
            try:
                from sentence_transformers import SentenceTransformer

                self.model = SentenceTransformer(SEMANTIC_MODEL_NAME, local_files_only=True)
                self.mode = "semantic"
            except Exception as exc:
                self.fallback_reason = f"语义嵌入不可用，已降级为哈希向量（仅允许默认缓存离线加载，不会下载模型）：{type(exc).__name__}: {exc}"

    @property
    def collection_name(self) -> str:
        return SEMANTIC_COLLECTION_NAME if self.mode == "semantic" else HASH_COLLECTION_NAME

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.mode == "semantic" and self.model is not None:
            vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            return np.asarray(vectors, dtype=np.float32).tolist()
        return [_hash_embedding(text) for text in texts]


def _lexical_overlap(query: str, text: str) -> float:
    """轻量重排特征：按查询 token 覆盖率衡量文本字面相关性。"""
    query_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9._-]+", query.lower()))
    text_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9._-]+", text.lower()))
    if not query_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _anchor_score(query: str, text: str) -> float:
    """为同页候选块选择保留含问题实体、术语或数字锚点的文本块。"""
    anchors = re.findall(r"[A-Za-z][A-Za-z .,&-]{2,}|\d+(?:[,.]\d+)*(?:%|\s*%|\s*billion)?", query)
    lowered_text = text.lower()
    return float(sum(1 for anchor in anchors if anchor.strip() and anchor.lower().strip() in lowered_text))


class RerankerProvider:
    """可选 Cross-Encoder 重排器；仅允许本地缓存加载，失败必须记录降级原因。"""

    def __init__(self) -> None:
        requested_mode = os.getenv("RAG_RERANKER_MODE", "disabled").strip().lower()
        self.mode = "disabled"
        self.model: Any | None = None
        self.model_name: str | None = None
        self.fallback_reason: str | None = None
        if requested_mode not in {"cross_encoder", "cross-encoder", "crossencoder"}:
            return
        try:
            from sentence_transformers import CrossEncoder

            self.model = CrossEncoder(CROSS_ENCODER_MODEL_NAME, local_files_only=True)
            self.mode = "cross_encoder"
            self.model_name = CROSS_ENCODER_MODEL_NAME
        except Exception as exc:
            self.mode = "hybrid_fallback"
            self.fallback_reason = (
                "Cross-Encoder 重排器不可用，已显式降级为既有混合重排"
                f"（仅允许默认缓存离线加载，不会下载模型）：{type(exc).__name__}: {exc}"
            )

    def score(self, query: str, documents: list[str]) -> list[float] | None:
        if self.mode != "cross_encoder" or self.model is None:
            return None
        scores = self.model.predict([(query, document) for document in documents], show_progress_bar=False)
        return [float(score) for score in scores]


class LocalResearchRAG:
    """使用 Chroma PersistentClient 保存页码级证据，并执行向量召回后的混合重排。"""

    def __init__(self, path: Path = CHROMA_DIR) -> None:
        self.provider = EmbeddingProvider()
        self.reranker = RerankerProvider()
        self.client = chromadb.PersistentClient(path=str(path))
        self.collection = self.client.get_or_create_collection(name=self.provider.collection_name, embedding_function=None)

    def index_text(self, text: str, metadata: dict[str, str], source_id: str | None = None) -> int:
        base_id = source_id or _stable_id("source", text)
        documents = _chunks(text)
        if not documents:
            return 0
        now = datetime.now(UTC).isoformat()
        ids = [f"{base_id}-{index}" for index in range(len(documents))]
        metadatas = []
        for index in range(len(documents)):
            item = {str(key): str(value) for key, value in metadata.items() if value is not None}
            item.update({"source_id": base_id, "chunk_index": str(index), "indexed_at": now})
            metadatas.append(item)
        self.collection.upsert(ids=ids, documents=documents, embeddings=self.provider.embed(documents), metadatas=metadatas)
        return len(documents)

    def index_pdf(self, file_path: Path) -> int:
        """逐页抽取 PDF 文本，将页码与文件名归属 ticker 写入元数据。"""
        ticker = infer_ticker_from_filename(file_path)
        material_metadata = _pdf_material_metadata(file_path, ticker)
        try:
            reader = PdfReader(str(file_path))
            if reader.is_encrypted:
                decrypt_result = reader.decrypt("")
                if not decrypt_result:
                    raise ValueError("PDF 已加密且无法使用空密码解密")
            total = 0
            for page_number, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text(extraction_mode="layout") or ""
                except Exception:
                    continue
                if not page_text.strip():
                    continue
                total += self.index_text(
                    page_text,
                    {
                        "source": "本地 PDF 研究资料",
                        "path": str(file_path),
                        "file_name": file_path.name,
                        "source_type": "pdf",
                        "page": str(page_number),
                        "published_at": "未提供",
                        "url": "",
                        "ticker": ticker,
                        **material_metadata,
                    },
                    source_id=_stable_id("pdf", f"{file_path.resolve()}:{page_number}:{page_text}"),
                )
            return total
        except Exception as exc:
            raise RuntimeError(f"PDF 解析失败：{file_path.name}: {type(exc).__name__}: {exc}") from exc

    def index_local_documents(self, directory: Path = KNOWLEDGE_DIR) -> int:
        count = 0
        for file_path in sorted(directory.glob("*")):
            suffix = file_path.suffix.lower()
            if suffix == ".pdf":
                count += self.index_pdf(file_path)
                continue
            if suffix not in {".txt", ".md"}:
                continue
            text = file_path.read_text(encoding="utf-8")
            count += self.index_text(
                text,
                {
                    "source": "本地研究资料",
                    "path": str(file_path),
                    "file_name": file_path.name,
                    "source_type": "text",
                    "page": "",
                    "published_at": "未提供",
                    "url": "",
                    "ticker": infer_ticker_from_filename(file_path),
                },
                source_id=_stable_id("file", str(file_path.resolve()) + text),
            )
        return count

    def clear_news(self, ticker: str) -> int:
        """Delete only previously indexed news for one ticker before indexing the filtered current batch."""
        normalized_ticker = ticker.upper().strip()
        records = self.collection.get(where={"ticker": normalized_ticker}, include=["metadatas"])
        ids = records.get("ids", [])
        metadatas = records.get("metadatas", [])
        news_ids = [
            record_id
            for record_id, metadata in zip(ids, metadatas)
            if str((metadata or {}).get("source_type") or "").lower() == "news"
        ]
        if news_ids:
            self.collection.delete(ids=news_ids)
        return len(news_ids)


    def index_news(self, records: list[dict[str, str]]) -> int:
        count = 0
        for item in records:
            count += self.index_text(
                item["text"],
                {"source": item["source"], "url": item["url"], "published_at": item["published_at"], "fetched_at": item["fetched_at"], "ticker": item["ticker"], "title": item["title"], "source_type": "news", "page": ""},
                source_id=item["id"],
            )
        return count

    def search(self, query: str, limit: int = 4, ticker: str | None = None) -> list[dict[str, Any]]:
        normalized_ticker = ticker.upper().strip() if ticker else None
        if self.collection.count() == 0:
            return []
        where = {"ticker": normalized_ticker} if normalized_ticker else None
        matching_count = self.collection.count() if where is None else len(self.collection.get(where=where, include=[]).get("ids", []))
        if matching_count == 0:
            return []
        # Cross-Encoder 实际启用时固定召回 30 条候选；其他情况保留既有 48 条混合检索候选池。
        expanded_query = expand_financial_query(query)
        cross_encoder_active = self.reranker.mode == "cross_encoder"
        candidate_limit = min(CROSS_ENCODER_CANDIDATE_LIMIT if cross_encoder_active else max(limit * 12, 48), matching_count)
        response = self.collection.query(
            query_embeddings=self.provider.embed([expanded_query]),
            n_results=min(candidate_limit, matching_count),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        documents = response.get("documents", [[]])[0]
        metadata = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]
        candidates = []
        for index, document in enumerate(documents):
            distance = float(distances[index]) if index < len(distances) else 1.0
            vector_score = 1.0 / (1.0 + max(distance, 0.0))
            lexical_score = _lexical_overlap(expanded_query, document)
            rerank_score = round(vector_score * 0.7 + lexical_score * 0.3, 6)
            candidates.append({"content": document, "metadata": metadata[index], "distance": distance, "vector_score": round(vector_score, 6), "lexical_score": round(lexical_score, 6), "rerank_score": rerank_score, "anchor_score": _anchor_score(expanded_query, document)})
        cross_scores = self.reranker.score(expanded_query, [item["content"] for item in candidates])
        if cross_scores is not None:
            for item, score in zip(candidates, cross_scores):
                item["cross_encoder_score"] = round(score, 6)
                item["rerank_score"] = round(score, 6)
        # 每页可有多个块：页级排序按最高相关分，同页则展示含问题锚点最多的块。
        page_candidates: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for item in candidates:
            item_metadata = item["metadata"]
            source_key = (item_metadata.get("source_id"), item_metadata.get("page")) if item_metadata.get("source_type") == "pdf" else (item_metadata.get("source_id"), item_metadata.get("chunk_index"))
            existing = page_candidates.get(source_key)
            if existing is None:
                page_candidates[source_key] = {"display": item, "page_score": item["rerank_score"]}
                continue
            existing["page_score"] = max(existing["page_score"], item["rerank_score"])
            display = existing["display"]
            if (item["anchor_score"], item["rerank_score"]) > (display["anchor_score"], display["rerank_score"]):
                existing["display"] = item
        deduplicated = [entry["display"] for entry in sorted(page_candidates.values(), key=lambda entry: entry["page_score"], reverse=True)[:limit]]
        return [{**item, "citation": f"S{index + 1}"} for index, item in enumerate(deduplicated)]

    def local_document_count(self, ticker: str) -> int:
        """返回指定 ticker 的本地文本/PDF 文本块数量；新闻不计入本地资料。"""
        normalized_ticker = ticker.upper().strip()
        records = self.collection.get(where={"ticker": normalized_ticker}, include=["metadatas"])
        metadatas = records.get("metadatas", [])
        return sum(1 for metadata in metadatas if str((metadata or {}).get("source_type") or "").lower() in {"pdf", "text"})


    def retrieval_status(self) -> dict[str, str | None]:
        return {
            "embedding_mode": self.provider.mode,
            "semantic_model": SEMANTIC_MODEL_NAME if self.provider.mode == "semantic" else None,
            "fallback_reason": self.provider.fallback_reason,
            "reranker_mode": self.reranker.mode,
            "reranker_model": self.reranker.model_name,
            "reranker_fallback_reason": self.reranker.fallback_reason,
        }

