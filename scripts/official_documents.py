"""官方文档发现、入库、检索的受控命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys

from investment_assistant.official_document_ingestion import (
    DocumentIngestError, MinerUParser, OfficialDocumentStore, search_official_documents,
)
from investment_assistant.official_document_sources import OfficialDocumentDiscovery


def main() -> int:
    parser = argparse.ArgumentParser(description="官方非结构化文档入库 MVP")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--source", choices=["CNINFO", "HKEX", "SEC"], required=True)
    discovery.add_argument("--ticker", required=True)
    discovery.add_argument("--year", required=True)
    discovery.add_argument("--company-name")
    discovery.add_argument("--limit", type=int, default=10)
    registered = sub.add_parser("ingest-registered")
    registered.add_argument("--ticker", required=True)
    registered.add_argument("--file-name")
    candidate = sub.add_parser("ingest-candidate")
    candidate.add_argument("--source", choices=["CNINFO", "HKEX", "SEC"], required=True)
    candidate.add_argument("--ticker", required=True)
    candidate.add_argument("--year", required=True)
    candidate.add_argument("--candidate-id", required=True)
    candidate.add_argument("--company-name")
    listing = sub.add_parser("list")
    listing.add_argument("--ticker")
    search = sub.add_parser("search")
    search.add_argument("--ticker", required=True)
    search.add_argument("--question", required=True)
    search.add_argument("--year")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.command == "discover":
            result = OfficialDocumentDiscovery().discover(args.source, args.ticker, year=args.year,
                                                            limit=args.limit, company_name=args.company_name)
        elif args.command == "ingest-registered":
            result = OfficialDocumentStore(parser=MinerUParser()).ingest_registered(args.ticker, file_name=args.file_name)
        elif args.command == "ingest-candidate":
            result = OfficialDocumentDiscovery().download_and_ingest(
                source=args.source, ticker=args.ticker, year=args.year,
                candidate_id=args.candidate_id, company_name=args.company_name,
            )
        elif args.command == "list":
            result = OfficialDocumentStore().list_documents(ticker=args.ticker)
        else:
            result = search_official_documents(args.question, ticker=args.ticker, year=args.year, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DocumentIngestError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
