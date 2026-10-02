"""下载并校验中文官方年报，作为中文检索评测语料。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from investment_assistant.config import DATA_DIR, KNOWLEDGE_DIR

MANIFEST_PATH = DATA_DIR / "materials_manifest.json"

# URL、报告期和文件名均显式固定，避免“最新”查询漂移造成评测不可复现。
CHINESE_MATERIALS: tuple[dict[str, str], ...] = (
    {
        "ticker": "300750.SZ",
        "company": "\u5b81\u5fb7\u65f6\u4ee3",
        "sector": "\u65b0\u80fd\u6e90\u7535\u6c60",
        "source": "\u5de8\u6f6e\u8d44\u8baf",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-03-10/1225002214.PDF",
        "report_date": "2025",
        "announcement_title": "\u5b81\u5fb7\u65f6\u4ee32025\u5e74\u5e74\u5ea6\u62a5\u544a",
        "file_name": "300750_2025_annual_report.pdf",
        "sha256": "c15272977147dee7e6935a38ea0e4fd6855370aabb106f54cfe20f7cf6048ec9",
    },
    {
        "ticker": "000001.SZ",
        "company": "\u5e73\u5b89\u94f6\u884c",
        "sector": "\u5546\u4e1a\u94f6\u884c",
        "source": "\u5de8\u6f6e\u8d44\u8baf",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-03-21/1225022887.PDF",
        "report_date": "2025",
        "announcement_title": "\u5e73\u5b89\u94f6\u884c2025\u5e74\u5e74\u5ea6\u62a5\u544a",
        "file_name": "000001_2025_annual_report.pdf",
        "sha256": "2273565ecbe1b32536631fd4a019a4f4a990f4c793cfd5b70eae90d44d3ff16c",
    },
    {
        "ticker": "000858.SZ",
        "company": "\u4e94\u7cae\u6db2",
        "sector": "\u767d\u9152\u6d88\u8d39",
        "source": "\u5de8\u6f6e\u8d44\u8baf",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-30/1225273091.PDF",
        "report_date": "2025",
        "announcement_title": "\u4e94\u7cae\u6db22025\u5e74\u5e74\u5ea6\u62a5\u544a",
        "file_name": "000858_2025_annual_report.pdf",
        "sha256": "09133e1f44b3bb4b2cebe211529ad68f5b04b6be10b43870f2a78c947d5910a4",
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pdf(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF"):
        raise ValueError(f"not_pdf:{path.name}")
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    text_page_count = sum(1 for page in reader.pages if (page.extract_text() or "").strip())
    if page_count < 20 or text_page_count < page_count * 0.8:
        raise ValueError(f"weak_text_layer:{path.name}:{text_page_count}/{page_count}")
    return {
        "file_size_bytes": path.stat().st_size,
        "page_count": page_count,
        "text_page_count": text_page_count,
        "sha256": _sha256(path),
    }


def _download(spec: dict[str, str], *, timeout: tuple[float, float]) -> Path:
    destination = KNOWLEDGE_DIR / spec["file_name"]
    temporary = destination.with_suffix(destination.suffix + ".part")
    if not spec["source_url"].startswith("https://static.cninfo.com.cn/"):
        raise ValueError("unapproved_source_url")
    response = requests.get(
        spec["source_url"],
        headers={"User-Agent": "InvestmentAssistant corpus bootstrap/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    temporary.write_bytes(response.content)
    try:
        _validate_pdf(temporary)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def prepare(*, download: bool) -> dict[str, Any]:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.is_file() else {}
    prepared: list[dict[str, Any]] = []
    for spec in CHINESE_MATERIALS:
        destination = KNOWLEDGE_DIR / spec["file_name"]
        if download and not destination.is_file():
            _download(spec, timeout=(15.0, 90.0))
        if not destination.is_file():
            raise FileNotFoundError(destination)
        validation = _validate_pdf(destination)
        if validation["sha256"] != spec["sha256"]:
            raise ValueError(f"sha256_mismatch:{spec['ticker']}")
        previous = manifest.get(spec["ticker"], {})
        entry = {
            "ticker": spec["ticker"],
            "source": spec["source"],
            "source_url": spec["source_url"],
            "downloaded_at": previous.get("downloaded_at") or datetime.now(UTC).isoformat(),
            "filing_form": "annual_report",
            "filing_date": None,
            "report_date": spec["report_date"],
            "material_kind": "official_pdf",
            "page_authority": "official",
            "file_name": spec["file_name"],
            "validation": validation,
            "announcement_title": spec["announcement_title"],
            "company": spec["company"],
            "sector": spec["sector"],
            "language": "zh-CN",
            "corpus_role": "chinese_retrieval_bootstrap",
        }
        manifest[spec["ticker"]] = entry
        prepared.append(entry)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", "materials": prepared}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    result = prepare(download=not args.check_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
