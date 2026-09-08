"""从 Apple 10-K 评测集抽取五题，端到端对比受控 LLM 与规则版报告。"""

from __future__ import annotations

import json
from datetime import datetime

from .config import REPORT_DIR
from .evaluation import _load_cases
from .workflow import run_research

SELECTED_IDS = ["services_revenue", "r_and_d_expense", "operating_cash_flow", "china_sales", "effective_tax_rate"]


def main() -> int:
    cases_by_id = {case["id"]: case for case in _load_cases()}
    selected = [cases_by_id[case_id] for case_id in SELECTED_IDS]
    output_dir = REPORT_DIR / f"llm_rule_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in selected:
        auto = run_research("AAPL", case["question"], "中期", report_mode="auto")
        rule = run_research("AAPL", case["question"], "中期", report_mode="rule")
        auto_path = output_dir / f"{case['id']}_auto.md"
        rule_path = output_dir / f"{case['id']}_rule.md"
        auto_path.write_text(auto["report"], encoding="utf-8")
        rule_path.write_text(rule["report"], encoding="utf-8")
        llm = auto.get("llm_result", {})
        rows.append({"id": case["id"], "question": case["question"], "auto_mode": "受控 LLM" if llm.get("used") else "规则版回退", "auto_reason": "引用校验通过" if llm.get("used") else llm.get("reason", "未知原因"), "auto_safety_passed": auto.get("evaluation", {}).get("passed"), "rule_safety_passed": rule.get("evaluation", {}).get("passed"), "auto_report": str(auto_path), "rule_report": str(rule_path)})
    summary = {"rows": rows}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 受控 LLM 与规则版端到端对比", "", "| 题目 | Auto 结果 | Auto safety | Rule safety |", "|---|---|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['id']} | {row['auto_mode']}：{row['auto_reason']} | {row['auto_safety_passed']} | {row['rule_safety_passed']} |")
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n输出目录：{output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
