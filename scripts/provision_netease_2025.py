"""从固定 SEC 申报文件重建网易 2025 年本地检索 PDF，结果必须匹配已审定摘要。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

from investment_assistant.bounded_general_qa import ISSUER_SHA256, ISSUER_HTML_SHA256, NETEASE_SOURCE_URL
from investment_assistant.config import KNOWLEDGE_DIR
from investment_assistant.fetch_materials import OfficialMaterialFetcher, SEC_SOURCE

FILE_NAME = "NTES_SEC_20F_2025_official-html.pdf"


def main() -> None:
    fetcher = OfficialMaterialFetcher()
    html = fetcher._get(NETEASE_SOURCE_URL, SEC_SOURCE, "www.sec.gov").content
    if hashlib.sha256(html).hexdigest() != ISSUER_HTML_SHA256:
        raise RuntimeError("SEC 原始申报文件版本不匹配，未写入本地资料。")
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=KNOWLEDGE_DIR) as directory:
        candidate = Path(directory) / FILE_NAME
        fetcher._html_to_pdf(html, candidate, "NetEase SEC 20-F 2025 official HTML conversion", invariant=True)
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != ISSUER_SHA256:
            raise RuntimeError("本地转换结果版本不匹配，未覆盖已存在的资料。")
        candidate.replace(KNOWLEDGE_DIR / FILE_NAME)
    print(f"已核验并重建 {KNOWLEDGE_DIR / FILE_NAME}")


if __name__ == "__main__":
    main()
