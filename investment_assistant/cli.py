"""命令行入口。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from .config import REPORT_DIR
from .rag import LocalResearchRAG
from .safety import validate_report
from .workflow import audit_json, run_research


def command_index() -> int:
    rag = LocalResearchRAG()
    count = rag.index_local_documents()
    print(f"已写入 {count} 个本地知识库文本块。")
    print("检索配置：" + json.dumps(rag.retrieval_status(), ensure_ascii=False))
    return 0


def command_research(ticker: str, topic: str, horizon: str, report_mode: str = "auto") -> int:
    result = run_research(ticker=ticker, topic=topic, horizon=horizon, report_mode=report_mode)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ticker = ticker.upper().strip().replace("/", "_")
    report_path = REPORT_DIR / f"{safe_ticker}_{timestamp}.md"
    audit_path = REPORT_DIR / f"{safe_ticker}_{timestamp}.json"
    report_path.write_text(result["report"], encoding="utf-8")
    audit_path.write_text(audit_json(result), encoding="utf-8")
    print(result["report"])
    print("\n--- 风险标记 ---")
    for item in result.get("risk_flags", []):
        print(f"- {item}")
    print("\n--- 报告质量评估 ---")
    print(audit_json(result.get("evaluation", {})))
    print("\n--- 检索与引用质量评估 ---")
    print(audit_json(result.get("retrieval_evaluation", {})))
    print("\n--- 检索配置 ---")
    print(audit_json(result.get("retrieval_status", {})))
    print(f"\n报告：{report_path}")
    print(f"审计记录：{audit_path}")
    return 0 if result.get("evaluation", {}).get("passed") else 2


def command_evaluate() -> int:
    reports = sorted(REPORT_DIR.glob("*.md"))
    if not reports:
        print("尚未发现报告，请先运行 research。")
        return 1
    failed = 0
    for path in reports:
        report = path.read_text(encoding="utf-8")
        audit_path = path.with_suffix(".json")
        financial_snapshot = None
        retrieval_evaluation = None
        if audit_path.exists():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            financial_snapshot = audit.get("financial_snapshot")
            retrieval_evaluation = audit.get("retrieval_evaluation")
        outcome = validate_report(report, sources=[{"citation": "S1"}], financial_snapshot=financial_snapshot)
        status = "通过" if outcome["passed"] else "失败"
        print(f"{status} {path.name}：报告评估={outcome}")
        if retrieval_evaluation is not None:
            print(f"  检索评估={retrieval_evaluation}")
        failed += int(not outcome["passed"])
    return 0 if failed == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="可追溯智能投研助手")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("index", help="索引 data/knowledge_base 下的本地文本和 PDF 资料")
    research = subparsers.add_parser("research", help="获取真实数据、检索证据并生成研究简报")
    research.add_argument("--ticker", required=True, help="Yahoo Finance ticker，例如 AAPL 或 0700.HK")
    research.add_argument("--topic", required=True, help="研究主题")
    research.add_argument("--horizon", default="中期", choices=["短期", "中期", "长期"], help="观察期限")
    research.add_argument("--report-mode", default="auto", choices=["auto", "rule"], help="auto: 尝试受控 LLM，失败回退；rule: 强制规则版")
    subparsers.add_parser("evaluate", help="离线检查已生成报告及其审计记录")
    args = parser.parse_args()
    if args.command == "index":
        return command_index()
    if args.command == "research":
        return command_research(args.ticker, args.topic, args.horizon, args.report_mode)
    return command_evaluate()


if __name__ == "__main__":
    raise SystemExit(main())


