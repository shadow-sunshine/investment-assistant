"""本地持久化 RAG：文本/PDF 页码解析、混合检索与可选语义嵌入。

默认使用确定性哈希向量，保证首次运行不依赖模型下载。设置
RAG_EMBEDDING_MODE=semantic 并安装 requirements-semantic.txt 后，改用
SentenceTransformers 本地语义向量；若模型未安装或加载失败，会明确降级并记录原因。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from pypdf import PdfReader

from .config import CHROMA_DIR, KNOWLEDGE_DIR

HASH_COLLECTION_NAME = "investment_research_sources_hash_v3"
SEMANTIC_COLLECTION_NAME = "investment_research_sources_semantic_v2"
HASH_EMBEDDING_DIMENSION = 384
SEMANTIC_MODEL_NAME = os.getenv("RAG_SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")


def _stable_id(prefix: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


TICKER_FILENAME_ALIASES = {
    "aapl": "AAPL",
    "apple": "AAPL",
}
KNOWN_TICKER_CODES = {
    "600519": "600519.SS",
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


class LocalResearchRAG:
    """使用 Chroma PersistentClient 保存页码级证据，并执行向量召回后的混合重排。"""

    def __init__(self, path: Path = CHROMA_DIR) -> None:
        self.provider = EmbeddingProvider()
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
        # PDF 每页可能包含多个文本块，扩大召回池后再按来源页去重，避免同页块挤占 Top-K。
        candidate_limit = min(max(limit * 12, 48), matching_count)
        response = self.collection.query(
            query_embeddings=self.provider.embed([query]),
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
            lexical_score = _lexical_overlap(query, document)
            rerank_score = round(vector_score * 0.7 + lexical_score * 0.3, 6)
            candidates.append({"content": document, "metadata": metadata[index], "distance": distance, "vector_score": round(vector_score, 6), "lexical_score": round(lexical_score, 6), "rerank_score": rerank_score})
        candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
        deduplicated = []
        seen_sources = set()
        for item in candidates:
            metadata = item["metadata"]
            source_key = (metadata.get("source_id"), metadata.get("page")) if metadata.get("source_type") == "pdf" else (metadata.get("source_id"), metadata.get("chunk_index"))
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            deduplicated.append(item)
            if len(deduplicated) >= limit:
                break
        return [{**item, "citation": f"S{index + 1}"} for index, item in enumerate(deduplicated)]

    def local_document_count(self, ticker: str) -> int:
        """返回指定 ticker 的本地文本/PDF 文本块数量；新闻不计入本地资料。"""
        normalized_ticker = ticker.upper().strip()
        records = self.collection.get(where={"ticker": normalized_ticker}, include=["metadatas"])
        metadatas = records.get("metadatas", [])
        return sum(1 for metadata in metadatas if str((metadata or {}).get("source_type") or "").lower() in {"pdf", "text"})


    def retrieval_status(self) -> dict[str, str | None]:
        return {"embedding_mode": self.provider.mode, "semantic_model": SEMANTIC_MODEL_NAME if self.provider.mode == "semantic" else None, "fallback_reason": self.provider.fallback_reason}

