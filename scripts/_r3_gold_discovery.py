"""第三步：提取腾讯研发费用数值 + 打印完整术语映射（确认哪些中文词触发 treatment 扩展）。"""
from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

KB = Path(__file__).resolve().parent.parent / "data" / "knowledge_base"


def snippet(text: str, phrase: str, width: int = 160) -> str:
    idx = text.find(phrase)
    if idx < 0:
        return "<<NOT FOUND>>"
    s = max(0, idx - 10)
    e = min(len(text), idx + len(phrase) + width)
    return text[s:e].replace("\n", " ").strip()


def main() -> int:
    reader = PdfReader(str(KB / "0700HK_annual_report_2025.pdf"))
    for page in (30, 171):
        text = reader.pages[page - 1].extract_text() or ""
        print(f"\n===== 0700.HK p{page} [Research and development] =====", flush=True)
        print(snippet(text, "Research and development", 180), flush=True)
    # 完整术语映射
    tm = json.loads((Path(__file__).resolve().parent.parent / "data" / "financial_term_map.json").read_text(encoding="utf-8-sig"))
    print("\n===== TERM MAP KEYS =====", flush=True)
    for k in tm:
        print(f"  {k} -> {tm[k]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
