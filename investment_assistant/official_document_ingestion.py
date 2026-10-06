"""官方非结构化文档入库：保留原件和 MinerU 解析身份，索引不等于字段核验。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import DATA_DIR, KNOWLEDGE_DIR, PROJECT_ROOT
from .rag import expand_financial_query

STORE_DIR = DATA_DIR / "official_documents"
_MANIFEST_NAMES = ("materials_manifest.json", "onboarded_materials.json", "issuer_materials.json")
_HOSTS = {"CNINFO": "static.cninfo.com.cn", "HKEX": "www1.hkexnews.hk", "SEC": "www.sec.gov"}
_WORD = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.I)
_SHA = re.compile(r"[0-9a-f]{64}")


class DocumentIngestError(RuntimeError):
    """入库失败不能产生可检索的半成品。"""


@dataclass(frozen=True)
class OfficialDocument:
    ticker: str
    source: str
    source_url: str
    title: str
    filing_form: str
    report_date: str | None
    filing_date: str | None
    file_path: Path
    source_sha256: str
    page_count: int
    page_authority: str
    registration: str | None = None
    registration_key: str | None = None


def _source_name(value: str) -> str:
    name = str(value or "").strip().upper()
    if name in {"巨潮资讯", "CNINFO"}:
        return "CNINFO"
    if name in {"披露易", "HKEX"}:
        return "HKEX"
    if name in {"SEC EDGAR", "SEC"}:
        return "SEC"
    raise DocumentIngestError("unsupported_official_source")


def _validate_url(source: str, url: str, *, registered: bool = False) -> None:
    """只认官方固定路径；历史 CNINFO 清单中的 HTTP 链接仅可用于已核 SHA 原件。"""
    parsed = urlparse(str(url or ""))
    host = _HOSTS[source]
    if parsed.hostname != host or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise DocumentIngestError("official_url_not_allowed")
    if parsed.scheme != "https" and not (registered and source == "CNINFO" and parsed.scheme == "http"):
        raise DocumentIngestError("official_url_not_allowed")
    path = parsed.path
    allowed = {
        "CNINFO": r"/finalpage/20\d{2}-\d{2}-\d{2}/[A-Za-z0-9._-]+\.pdf",
        "HKEX": r"/listedco/listconews/sehk/20\d{2}/\d{4}/[A-Za-z0-9._-]+\.pdf",
        "SEC": r"/Archives/edgar/data/\d+/\d+/[A-Za-z0-9._-]+\.(?:htm|html|pdf)",
    }
    if not re.fullmatch(allowed[source], path, flags=re.I):
        raise DocumentIngestError("official_url_path_not_allowed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise DocumentIngestError(f"registration_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise DocumentIngestError(f"registration_invalid:{path.name}")
    return value


def registered_document(ticker: str, *, file_name: str | None = None,
                        data_dir: Path = DATA_DIR, knowledge_dir: Path = KNOWLEDGE_DIR) -> OfficialDocument:
    """只从现有官方资料登记读取原件，不接受调用方自称的 URL 或 SHA。"""
    code = str(ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.]{2,16}", code):
        raise DocumentIngestError("ticker_invalid")
    matches: list[tuple[Path, str, dict[str, Any]]] = []
    for manifest_name in _MANIFEST_NAMES:
        manifest_path = data_dir / manifest_name
        if not manifest_path.is_file():
            continue
        for key, entry in _read_manifest(manifest_path).items():
            if not isinstance(entry, dict) or entry.get("ticker") != code:
                continue
            if file_name is not None and entry.get("file_name") != file_name:
                continue
            matches.append((manifest_path, key, entry))
    if len(matches) != 1:
        raise DocumentIngestError("registered_document_not_unique" if matches else "registered_document_missing")
    manifest_path, key, entry = matches[0]
    source = _source_name(str(entry.get("source") or ""))
    url = str(entry.get("source_url") or "")
    _validate_url(source, url, registered=True)
    validation = entry.get("validation") or {}
    sha, pages = validation.get("sha256"), validation.get("page_count")
    filename = entry.get("file_name")
    if (not isinstance(filename, str) or Path(filename).name != filename or filename in {"", ".", ".."}
            or not filename.lower().endswith(".pdf") or not _SHA.fullmatch(str(sha))
            or type(pages) is not int or pages <= 0):
        raise DocumentIngestError("registration_identity_invalid")
    file_path = knowledge_dir / filename
    if not file_path.is_file() or _sha256(file_path) != sha:
        raise DocumentIngestError("registered_source_sha256_drift")
    return OfficialDocument(
        ticker=code, source=source, source_url=url,
        title=str(entry.get("announcement_title") or entry.get("company") or filename),
        filing_form=str(entry.get("filing_form") or "announcement"),
        report_date=str(entry.get("report_date") or "") or None,
        filing_date=str(entry.get("filing_date") or "") or None,
        file_path=file_path, source_sha256=sha, page_count=pages,
        page_authority=str(entry.get("page_authority") or ("generated" if entry.get("material_kind") == "official_html_converted_to_pdf" else "official")),
        registration=str(manifest_path), registration_key=key,
    )


class StructuredParser(Protocol):
    def parse(self, pdf: Path, output_dir: Path) -> Path: ...


class MinerUParser:
    """通过隔离 Python 环境调用 MinerU 4 SDK，要求输出含页号的结构化 JSON。"""

    def __init__(self, python: str | Path | None = None, *, tier: str = "flash", ocr_mode: str = "txt",
                 timeout_seconds: int = 3600) -> None:
        if tier not in {"flash", "basic", "standard", "advanced"} or ocr_mode not in {"txt", "auto", "ocr"}:
            raise ValueError("mineru_parse_options_invalid")
        self.python = str(python or os.getenv("MINERU_PYTHON") or sys.executable)
        self.tier, self.ocr_mode, self.timeout_seconds = tier, ocr_mode, timeout_seconds

    def parse(self, pdf: Path, output_dir: Path) -> Path:
        script = PROJECT_ROOT / "scripts" / "mineru_parse_document.py"
        cmd = [self.python, str(script), str(pdf), str(output_dir), self.tier, self.ocr_mode]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=self.timeout_seconds, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise DocumentIngestError(f"mineru_unavailable:{type(exc).__name__}") from exc
        if result.returncode != 0:
            raise DocumentIngestError(f"mineru_parse_failed:{(result.stderr or result.stdout)[-600:]}")
        structured = output_dir / "structured_content.json"
        if not structured.is_file():
            raise DocumentIngestError("mineru_structured_output_missing")
        return structured


def _block_content(block: dict[str, Any]) -> str:
    """只取正文/表格文本，不把图片路径、base64 或几何信息当作证据。"""
    value = block.get("content")
    if isinstance(value, dict):
        for key in ("html", "markdown", "text", "paragraph_content", "table_content", "content"):
            if isinstance(value.get(key), str) and value[key].strip():
                value = value[key]
                break
        else:
            value = " ".join(str(part) for part in value.values() if isinstance(part, str))
    if not isinstance(value, str):
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True) if "<table" in value.lower() else value
    return " ".join(text.split())


def _records_from_structured(structured: dict[str, Any], doc: OfficialDocument, doc_id: str) -> list[dict[str, Any]]:
    pages = structured.get("pages")
    if not isinstance(pages, list) or len(pages) != doc.page_count or structured.get("is_full_document") is False:
        raise DocumentIngestError("mineru_page_coverage_invalid")
    page_numbers = [page.get("page_idx") for page in pages if isinstance(page, dict)]
    if page_numbers != list(range(doc.page_count)):
        raise DocumentIngestError("mineru_page_identity_invalid")
    metadata = {"document_id": doc_id, "ticker": doc.ticker, "source": doc.source,
                "source_url": doc.source_url, "source_sha256": doc.source_sha256,
                "title": doc.title, "filing_form": doc.filing_form, "report_date": doc.report_date,
                "filing_date": doc.filing_date, "page_authority": doc.page_authority}
    records: list[dict[str, Any]] = []
    for page in pages:
        page_no = page["page_idx"] + 1
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            raise DocumentIngestError("mineru_page_blocks_invalid")
        page_fragments: list[str] = []
        paragraph_no = table_no = 0
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "")
            if kind in {"image", "chart", "header", "footer", "page_number"}:
                continue
            content = _block_content(block)
            if not content:
                continue
            level = "table" if kind == "table" else "paragraph"
            ordinal = table_no if level == "table" else paragraph_no
            table_no += level == "table"
            paragraph_no += level == "paragraph"
            page_fragments.append(content)
            records.append({**metadata, "record_id": f"{doc_id}:p{page_no}:{level}{ordinal}",
                            "level": level, "page": page_no, "ordinal": ordinal, "content": content})
        records.append({**metadata, "record_id": f"{doc_id}:p{page_no}:page",
                        "level": "page", "page": page_no, "ordinal": 0,
                        "content": "\n".join(page_fragments)})
    if not any(record["level"] != "page" for record in records):
        raise DocumentIngestError("mineru_no_retrievable_content")
    return records


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_doc_manifest(doc_dir: Path) -> dict[str, Any]:
    path = doc_dir / "manifest.json"
    value = _read_manifest(path)
    if value.get("document_id") != doc_dir.name:
        raise DocumentIngestError("indexed_document_identity_invalid")
    raw = doc_dir / "raw.pdf"
    structured = doc_dir / "parsed" / "structured_content.json"
    index = doc_dir / "index.jsonl"
    if not all(item.is_file() for item in (raw, structured, index)):
        raise DocumentIngestError("indexed_document_incomplete")
    if _sha256(raw) != value.get("source_sha256") or _sha256(structured) != value.get("parsed_sha256") or _sha256(index) != value.get("index_sha256"):
        raise DocumentIngestError("indexed_document_sha256_drift")
    registration = value.get("registration")
    if registration:
        manifest_path = Path(registration)
        entry = _read_manifest(manifest_path).get(value.get("registration_key"))
        if not isinstance(entry, dict) or (entry.get("validation") or {}).get("sha256") != value.get("source_sha256"):
            raise DocumentIngestError("registration_sha256_drift")
    return value


class OfficialDocumentStore:
    """原件、MinerU 产物和索引按同一文档 ID 发布；不写正式资料清单。"""

    def __init__(self, root: Path = STORE_DIR, parser: StructuredParser | None = None) -> None:
        self.root = Path(root)
        self.parser = parser or MinerUParser()

    def ingest(self, document: OfficialDocument) -> dict[str, Any]:
        source = _source_name(document.source)
        _validate_url(source, document.source_url, registered=bool(document.registration))
        if not re.fullmatch(r"[A-Z0-9.]{2,16}", document.ticker) or document.page_authority not in {"official", "generated"}:
            raise DocumentIngestError("document_identity_invalid")
        if not _SHA.fullmatch(document.source_sha256) or document.file_path.suffix.lower() != ".pdf":
            raise DocumentIngestError("document_sha256_invalid")
        if not document.file_path.is_file() or _sha256(document.file_path) != document.source_sha256:
            raise DocumentIngestError("document_source_sha256_drift")
        reader = PdfReader(str(document.file_path))
        if len(reader.pages) != document.page_count or document.page_count <= 0:
            raise DocumentIngestError("document_page_count_drift")
        doc_id = hashlib.sha256(f"{source}|{document.ticker}|{document.source_url}|{document.source_sha256}".encode()).hexdigest()[:32]
        final = self.root / doc_id
        if final.exists():
            manifest = _read_doc_manifest(final)
            if manifest.get("source_sha256") != document.source_sha256 or manifest.get("ticker") != document.ticker:
                raise DocumentIngestError("document_id_collision")
            return manifest
        self.root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".ingest-", dir=self.root))
        try:
            shutil.copyfile(document.file_path, stage / "raw.pdf")
            if _sha256(stage / "raw.pdf") != document.source_sha256:
                raise DocumentIngestError("document_copy_sha256_drift")
            parsed = stage / "parsed"
            parsed.mkdir()
            structured_path = self.parser.parse(stage / "raw.pdf", parsed)
            if structured_path.resolve() != (parsed / "structured_content.json").resolve():
                raise DocumentIngestError("mineru_output_path_invalid")
            structured = json.loads(structured_path.read_text(encoding="utf-8"))
            records = _records_from_structured(structured, document, doc_id)
            index = stage / "index.jsonl"
            with index.open("w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            result = {"document_id": doc_id, "ticker": document.ticker, "source": source,
                      "source_url": document.source_url, "title": document.title,
                      "filing_form": document.filing_form, "filing_date": document.filing_date,
                      "report_date": document.report_date, "page_authority": document.page_authority,
                      "file_name": document.file_path.name, "source_sha256": document.source_sha256,
                      "parsed_sha256": _sha256(structured_path), "index_sha256": _sha256(index),
                      "parser": "MinerU", "parser_version": (structured.get("metadata") or {}).get("producer", {}).get("version"),
                      "indexed_at": datetime.now(UTC).isoformat(),
                      "page_count": document.page_count,
                      "paragraph_count": sum(record["level"] == "paragraph" for record in records),
                      "table_count": sum(record["level"] == "table" for record in records),
                      "state": "indexed_unverified", "registration": document.registration,
                      "registration_key": document.registration_key}
            _write_json(stage / "manifest.json", result)
            os.rename(stage, final)
            return result
        finally:
            if stage.exists() and stage.resolve().parent == self.root.resolve():
                shutil.rmtree(stage)

    def ingest_registered(self, ticker: str, *, file_name: str | None = None,
                          data_dir: Path = DATA_DIR, knowledge_dir: Path = KNOWLEDGE_DIR) -> dict[str, Any]:
        return self.ingest(registered_document(ticker, file_name=file_name, data_dir=data_dir, knowledge_dir=knowledge_dir))

    def list_documents(self, *, ticker: str | None = None) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        entries = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest = _read_doc_manifest(directory)
            if ticker and manifest.get("ticker") != ticker.upper().strip():
                continue
            entries.append(manifest)
        return entries


def _terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    terms: set[str] = set()
    for item in _WORD.findall(normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", item):
            terms.update(item[index:index + 2] for index in range(len(item) - 1))
            if len(item) == 1:
                terms.add(item)
        else:
            terms.add(item)
    return terms


def search_official_documents(query: str, *, ticker: str, year: str | None = None,
                              limit: int = 5, store: OfficialDocumentStore | None = None) -> list[dict[str, Any]]:
    """只返回原文片段和来源，不能把索引命中说成已核验财务事实。"""
    if not str(query or "").strip() or not ticker or not 1 <= limit <= 20:
        raise ValueError("official_search_input_invalid")
    store = store or OfficialDocumentStore()
    # 沿用现有中英财务术语映射；补充披露文档特有的少量词面别名。
    extensions = []
    for phrase, variant in (("总收入", "Total Revenue"), ("总收入", "Revenues"),
                            ("营业收入", "Total Revenue"),
                            ("提交日期", "Date Submitted"), ("研发费用", "R&D"),
                            ("研发开支", "R&D"), ("金额上限", "不超过"),
                            ("担保金额上限", "担保额度")):
        if phrase in query:
            extensions.append(variant)
    expanded_query = expand_financial_query(query) + " " + " ".join(extensions)
    query_terms = _terms(expanded_query)
    phrases = [part.strip().lower() for part in extensions if part.strip()]
    mapped = expanded_query[len(query):].strip()
    if mapped:
        # 一个映射可以有多个英文词；至少保留已映射的完整财务词组。
        phrases.extend([variant.lower() for variant in ("Net income", "Cash and cash equivalents",
                       "Total revenue", "share repurchase", "repurchased") if variant.lower() in mapped.lower()])
    if not query_terms:
        return []
    candidates: list[tuple[float, dict[str, Any]]] = []
    neighbors: dict[str, dict[str, Any]] = {}
    for document in store.list_documents(ticker=ticker):
        report_date = str(document.get("report_date") or "")
        if year and not report_date.startswith(year):
            continue
        index_path = store.root / document["document_id"] / "index.jsonl"
        rows = []
        for line in index_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if (record.get("source_sha256") != document["source_sha256"]
                    or record.get("source_url") != document["source_url"]
                    or record.get("ticker") != document["ticker"]):
                raise DocumentIngestError("indexed_record_identity_drift")
            content = str(record.get("content") or "")
            # SEC HTML 转换件的 XBRL 隐藏上下文不是人可读的报告正文。
            if content and not (document["source"] == "SEC" and
                                (content.count("us-gaap:") > 3 or content.count("Member") > 8)):
                rows.append((record, _terms(content)))
        if not rows:
            continue
        neighbors.update({record["record_id"]: record for record, _ in rows})
        # MinerU flash 可能将财务表的字段名与数字拆为相邻文本块；只在同一页合并检索窗口。
        expanded_rows = []
        for record, _ in rows:
            content = record["content"]
            adjacent_ids = []
            if (record["level"] == "paragraph" and len(content) < 80
                    and re.search(r"[A-Za-z\u4e00-\u9fff]", content)):
                for offset in (1, 2):
                    following = neighbors.get(f"{record['document_id']}:p{record['page']}:paragraph{record['ordinal'] + offset}")
                    if not following or following["page"] != record["page"]:
                        break
                    content += " " + following["content"]
                    adjacent_ids.append(following["record_id"])
            expanded_rows.append(({**record, "retrieved_content": content,
                                   "adjacent_record_ids": adjacent_ids}, _terms(content)))
        rows = expanded_rows
        scope_terms = _terms(ticker + " " + (year or ""))
        keywords = query_terms - scope_terms
        doc_frequency = {term: sum(term in terms for _, terms in rows) for term in keywords}
        for record, terms in rows:
            matches = terms & keywords
            if not matches:
                continue
            # 稀有词与短片段优先；页级文本只在跨块查询时作为补充。
            score = sum(math.log1p(len(rows) / (1 + doc_frequency[term])) for term in matches)
            content_lower = re.sub(r"[*\\]", "", record["retrieved_content"].lower())
            phrase_hits = sum(phrase in content_lower for phrase in phrases)
            score += 30.0 + 5.0 * (phrase_hits - 1) if phrase_hits else 0.0
            # 表格标签/指标名旁边的金额优于泛泛讨论该术语的注释段落。
            proximity = 0.0
            for phrase in phrases:
                if re.search(re.escape(phrase) + r"\s*(?:\$|\d|[:：]\s*\d)", content_lower):
                    proximity = max(proximity, 25.0)
                elif re.search(re.escape(phrase) + r".{0,80}(?:rmb\s*)?\d{1,3},\d{3}", content_lower):
                    proximity = max(proximity, 12.0)
            score += proximity
            if re.search(r"多少|是多少|how many|how much", query, re.I):
                score += 1.0 if re.search(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s*亿", record["retrieved_content"]) else -1.0
            score -= math.log1p(len(record["retrieved_content"])) * 0.10
            if record["level"] == "page":
                score *= 0.32
            else:
                score += 0.35
            candidates.append((score, record))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]["document_id"], pair[1]["page"], pair[1]["record_id"]))
    selected = []
    for _, record in candidates[:limit]:
        selected.append({**record, "citation": f"O{len(selected) + 1}",
                         "evidence_level": "indexed_official_unverified",
                         "content": record["retrieved_content"][:1400]})
    return selected
