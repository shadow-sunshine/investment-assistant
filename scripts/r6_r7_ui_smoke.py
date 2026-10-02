"""R6/R7 UI 离线烟测服务；仅临时目录与合成报告，不访问真实研究数据。"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--resume-root", type=Path)
    args = parser.parse_args()
    root = args.resume_root.resolve() if args.resume_root else Path(tempfile.mkdtemp(prefix="ia-r6r7-smoke-"))
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not root.name.startswith("ia-r6r7-smoke-"):
        raise ValueError("烟测目录必须是临时目录内的 ia-r6r7-smoke-*。")
    from investment_assistant import config
    config.REPORT_DIR = root / "reports"
    config.JOB_DIR = root / "jobs"
    config.REPORT_DIR.mkdir(exist_ok=True)
    config.JOB_DIR.mkdir(exist_ok=True)
    from investment_assistant import api, audit_log, research_memory, review_publish, report_jobs
    audit_log.AUDIT_LOG_DIR = root / "audit"
    review_publish.REVIEW_DIR = root / "reviews"
    review_publish.PUBLISH_DIR = root / "publish"
    research_memory.WATCHLIST_PATH = root / "watches.json"
    research_memory.MEMORY_PATH = root / "memories.json"
    api.load_material_index = lambda: {}
    os.environ["IA_AUTH_TOKENS"] = json.dumps({
        "fixture-analyst-token": {"actor": "analyst", "tenant": "fixture-team", "roles": ["analyst"]},
        "fixture-reviewer-token": {"actor": "reviewer", "tenant": "fixture-team", "roles": ["reviewer"]},
        "fixture-publisher-token": {"actor": "publisher", "tenant": "fixture-team", "roles": ["publisher"]},
        "fixture-other-tenant-token": {"actor": "other", "tenant": "other-team", "roles": ["analyst"]},
    })
    audit = {"ticker": "AAPL", "topic": "OFFLINE FIXTURE 年度收入", "horizon": "中期", "tenant_id": "fixture-team", "actor_id": "analyst",
             "market_snapshot": {"data_available": True, "latest_close": 50, "currency": "USD"},
             "financial_snapshot": {"data_available": True, "currency": "USD", "revenue": 100, "revenue_period_end": "2025-09-30"},
             "sources": [], "claims": [{"claim_id": "c1", "claim_text": "收入 100 美元", "anchors": [
                 {"kind": "snapshot", "field_path": "financial_snapshot.revenue", "value": 100, "period": "2025-09-30", "unit": "USD"}]}],
             "report": "# OFFLINE FIXTURE · AAPL\n\n合成烟测资料，不是真实投研。\n\n收入 100 美元（2025-09-30，USD）。", "evaluation": {"passed": True}}
    for report_id in ("AAPL_offline_fixture", "AAPL_manual_claim_fixture"):
        data = copy.deepcopy(audit)
        if "manual" in report_id:
            data.pop("claims")
        if not (config.REPORT_DIR / f"{report_id}.json").exists():
            (config.REPORT_DIR / f"{report_id}.md").write_text(data["report"], encoding="utf-8")
            (config.REPORT_DIR / f"{report_id}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    class OfflineGraph:
        def stream(self, initial, stream_mode):
            final = {**copy.deepcopy(audit), **initial}
            for name, _label in report_jobs.WORKFLOW_STEPS:
                time.sleep(.15)
                yield "updates", {name: {}}
            yield "values", final

    api.job_service = report_jobs.JobService(report_jobs.JobStore(config.JOB_DIR), graph_factory=OfflineGraph)
    print(f"Offline smoke data only: {root}", flush=True)
    import uvicorn
    uvicorn.run(api.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
