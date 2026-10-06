"""隔离环境中的 MinerU 4 PDF 解析入口。项目主环境不安装大型推理依赖。"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print("用法：python mineru_parse_document.py <pdf> <输出目录> <tier> <ocr_mode>", file=sys.stderr)
        return 2
    pdf, output, tier, ocr_mode = sys.argv[1:]
    if Path(pdf).suffix.lower() != ".pdf" or tier not in {"flash", "basic", "standard", "advanced"} or ocr_mode not in {"txt", "auto", "ocr"}:
        print("MinerU 参数不合法", file=sys.stderr)
        return 2
    try:
        from mineru.parser import parse
        from mineru.parser.writer import FileBasedDataWriter
    except ImportError:
        print("当前 Python 未安装 MinerU 4；请设置 MINERU_PYTHON 指向独立环境", file=sys.stderr)
        return 3
    result = parse(Path(pdf), tier=tier, ocr_mode=ocr_mode, page_range="all")
    result.save(FileBasedDataWriter(str(output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

