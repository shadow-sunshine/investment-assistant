"""中文投研问答闭环评测：检索、证据、回答或拒答、结果统计。"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader

from .config import DATA_DIR, KNOWLEDGE_DIR
from .cross_encoder_eval import sha
from .multilingual_evidence_retrieval import MODEL_NAME, MODEL_REVISION

DATASET_PATH = DATA_DIR / "chinese_retrieval_holdout_v1.json"
RESULT_PATH = DATA_DIR / "evaluations" / "chinese_answer_e2e_v1.json"
REPORT_PATH = DATA_DIR / "evaluations" / "chinese_answer_e2e_v1.md"
MODEL_SNAPSHOT = Path.home() / ".cache/huggingface/hub" / f"models--{MODEL_NAME.replace('/', '--')}" / "snapshots" / MODEL_REVISION
TOP_K = 4
CANDIDATE_BUDGET = 48
SEMANTIC_BUDGET = 16

FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("货币资金", ("货币资金",)),
    ("交易性金融资产", ("交易性金融资产",)),
    ("研发投入占营业收入比例", ("研发投入占营业收入比例", "研发投入占营业收入的比例")),
    ("境外收入", ("境外收入", "境外销售收入", "境外")),
    ("不良贷款率", ("不良贷款率",)),
    ("拨备覆盖率", ("拨备覆盖率",)),
    ("营业利润", ("营业利润",)),
    ("每股经营活动产生的现金流量净额", ("每股经营活动产生的现金流量净额",)),
    ("财务费用", ("财务费用",)),
    ("现金及现金等价物净增加额", ("现金及现金等价物净增加额",)),
    ("应收账款", ("应收账款",)),
    ("长期股权投资", ("长期股权投资",)),
    ("存货", ("存货",)),
)
NUMBER_RE = re.compile(r"(?<![\d,.])(?:\([−-]?\d[\d,.]*(?:\.\d+)?\)|[−+-]?\d[\d,.]*(?:\.\d+)?)(?:\s*[%％])?(?![\d,.])")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text)).replace("−", "-")
    text = text.replace("\u200b", "").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _numeric(value: str) -> str:
    value = _norm(value).replace(",", "").replace(" ", "")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    return value.replace("％", "%")


def _field(question: str) -> tuple[str | None, tuple[str, ...]]:
    normalized = _norm(question)
    matches = [(label, aliases) for label, aliases in FIELD_ALIASES
               if any(_norm(alias) in normalized for alias in aliases)]
    return max(matches, key=lambda item: max(len(_norm(alias)) for alias in item[1])) if matches else (None, ())


def _should_refuse(question: str, field: str | None) -> str | None:
    if any(token in question for token in ("股价", "买入", "值得投资", "投资建议", "预测", "未来", "明年", "一定", "建议")):
        return "问题要求预测、投资建议或超出冻结年报事实范围。"
    if field is None:
        return "问题未匹配到当前中文财报事实字段。"
    return None


def _load_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != "chinese-retrieval-holdout-v1":
        raise ValueError("dataset_schema_mismatch")
    if len(dataset.get("cases", [])) < 12:
        raise ValueError("holdout_too_small")
    if len({case["id"] for case in dataset["cases"]}) != len(dataset["cases"]):
        raise ValueError("duplicate_case")
    return dataset


def _load_pages(dataset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    manifest = json.loads((DATA_DIR / "materials_manifest.json").read_text(encoding="utf-8"))
    pages_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for ticker, spec in dataset["materials"].items():
        path = KNOWLEDGE_DIR / spec["file_name"]
        entry = manifest.get(ticker) or {}
        if spec.get("ticker") != ticker or sha(path) != spec["sha256"]:
            raise ValueError(f"material_sha_drift:{ticker}")
        if entry.get("file_name") != spec["file_name"] or (entry.get("validation") or {}).get("sha256") != spec["sha256"]:
            raise ValueError(f"manifest_binding_drift:{ticker}")
        reader = PdfReader(str(path))
        if len(reader.pages) != int(spec["page_count"]):
            raise ValueError(f"page_count_drift:{ticker}")
        pages_by_ticker[ticker] = []
        for page_no, page in enumerate(reader.pages, 1):
            content = page.extract_text() or ""
            pages_by_ticker[ticker].append({
                "content": content,
                "lines": [line.strip() for line in content.splitlines() if line.strip()],
                "metadata": {"ticker": ticker, "file_name": spec["file_name"], "source_sha256": spec["sha256"], "page": page_no},
            })
    return pages_by_ticker




def _field_line_matches(line: str, field: str | None, aliases: tuple[str, ...]) -> bool:
    normalized = _norm(line)
    if not any(_norm(alias) in normalized for alias in aliases):
        return False
    exclusions = {
        "营业利润": ("减值损失前营业利润", "营业收入或营业利润"),
        "境外收入": ("境外债券", "境外法人", "境外资产", "存放在境外"),
        "应收账款": ("应收账款组合",),
    }
    return not any(token in normalized for token in exclusions.get(field or "", ()))


def _lexical_rank(pages: list[dict[str, Any]], aliases: tuple[str, ...], question: str, field: str | None) -> list[dict[str, Any]]:
    ranked = []
    normalized_question = _norm(question)
    asks_ratio = any(token in normalized_question for token in ("比例", "占", "%"))
    asks_period_end = any(token in normalized_question for token in ("年末", "期末", "12月31日"))
    asks_annual = "年度" in normalized_question or "年" in normalized_question
    for page in pages:
        score = 0.0
        best_excerpt = ""
        for index, line in enumerate(page["lines"]):
            normalized = _norm(line)
            hits = [alias for alias in aliases if _norm(alias) in normalized]
            if not hits or not _field_line_matches(line, field, aliases):
                continue
            line_score = max(len(_norm(alias)) for alias in hits) * 2.0 + 2.0 * len([match.group() for match in NUMBER_RE.finditer(normalized)])
            if "%" in normalized or "％" in normalized:
                line_score += 5.0 if asks_ratio else 1.0
            if _field_line_matches(line, field, aliases):
                line_score += 15.0
            if any(token in " ".join(page["lines"])[:2500] for token in ("单位：", "单位 ", "项 目", "项目", "合并利润表", "资产及负债状况")):
                line_score += 5.0
            nearby = " ".join(page["lines"][max(0, index - 2):index + 3])
            if asks_period_end and any(token in nearby for token in ("年末", "期末", "12月31日")):
                line_score += 6.0
            if asks_annual and "2025" in nearby:
                line_score += 2.0
            if "季度" in nearby and "季度" not in normalized_question:
                line_score -= 4.0
            for context_term in ("营业收入", "经营活动产生的现金流量净额", "现金及现金等价物"):
                if context_term in normalized_question and context_term in nearby:
                    line_score += 4.0
            if line_score > score:
                score = line_score
                best_excerpt = "\n".join(page["lines"][max(0, index - 2):index + 3])[:1200]
        ranked.append({**page, "lexical_score": score, "excerpt": best_excerpt})
    return sorted(ranked, key=lambda item: (-item["lexical_score"], item["metadata"]["page"]))


def _retrieve(question: str, ticker: str, pages: dict[str, list[dict[str, Any]]], model: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    field, aliases = _field(question)
    if field is None:
        return [], []
    ranked = _lexical_rank(pages[ticker], aliases, question, field)
    candidates = ranked[:CANDIDATE_BUDGET]
    head = candidates[:SEMANTIC_BUDGET]
    if not head:
        return [], candidates
    vectors = np.asarray(model.encode([question] + [item["excerpt"] for item in head], batch_size=32,
                                      normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False), dtype=np.float32)
    if vectors.shape[0] != len(head) + 1 or not np.isfinite(vectors).all():
        raise ValueError("invalid_semantic_vectors")
    similarities = vectors[1:] @ vectors[0]
    ordered = sorted(enumerate(head), key=lambda item: (-(item[1]["lexical_score"] + float(similarities[item[0]]) * 0.5), item[1]["metadata"]["page"]))
    return [item for _, item in ordered[:TOP_K]], candidates


def _extract_answer(sources: list[dict[str, Any]], field: str | None, aliases: tuple[str, ...], question: str) -> tuple[str | None, dict[str, Any] | None]:
    for source in sources:
        for index, line in enumerate(source["lines"]):
            normalized = _norm(line)
            if not _field_line_matches(line, field, aliases):
                continue
            numbers = [match.group() for match in NUMBER_RE.finditer(normalized)]
            if any(token in _norm(question) for token in ("比例", "占", "率")):
                percent_numbers = re.findall(r"[-+]?\d[\d,.]*[%％]", normalized)
                if percent_numbers:
                    numbers = percent_numbers
            if not numbers:
                continue
            value = numbers[0].replace(" ", "")
            excerpt = "\n".join(source["lines"][max(0, index - 1):index + 2])
            return value, {"metadata": source["metadata"], "excerpt": excerpt}
    return None, None


def _identity_match(source: dict[str, Any] | None, case: dict[str, Any]) -> bool:
    if not source:
        return False
    metadata = source["metadata"]
    return (metadata.get("ticker") == case["ticker"] and metadata.get("file_name") == case["file_name"]
            and metadata.get("source_sha256") == case["source_sha"] and metadata.get("page") in case["target_pages"])


def _run_case(case: dict[str, Any], pages: dict[str, list[dict[str, Any]]], model: Any) -> dict[str, Any]:
    field, aliases = _field(case["question"])
    refusal_reason = _should_refuse(case["question"], field)
    if refusal_reason:
        output = {"status": "refused", "answer": "当前证据边界不足，无法可靠回答该问题。", "reason": refusal_reason, "sources": []}
    else:
        sources, candidates = _retrieve(case["question"], case["ticker"], pages, model)
        value, evidence = _extract_answer(sources, field, aliases, case["question"])
        if value is None or evidence is None:
            output = {"status": "refused", "answer": "当前报告没有找到可核验的字段与数值证据。", "reason": "top4_evidence_insufficient", "sources": [s["metadata"] for s in sources]}
        else:
            output = {"status": "answered", "answer": f"{field}为 {value}。", "reason": None,
                      "sources": [s["metadata"] for s in sources], "evidence": evidence}
    expected = case["expected_status"]
    status_correct = output["status"] == expected
    value_correct = expected != "answered" or _numeric(case["answer_value"]) == _numeric((output.get("answer") or "").split("为 ")[-1].rstrip("。"))
    citation_correct = expected != "answered" or _identity_match(output.get("evidence"), case)
    return {"id": case["id"], "ticker": case["ticker"], "expected_status": expected,
            "actual_status": output["status"], "status_correct": status_correct,
            "value_correct": value_correct, "citation_correct": citation_correct,
            "grounded_answer": bool(status_correct and value_correct and citation_correct), "output": output}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    answerable = [row for row in rows if row["expected_status"] == "answered"]
    refusals = [row for row in rows if row["expected_status"] == "refused"]
    return {"cases": n, "answerable": len(answerable), "refusal_cases": len(refusals),
            "status_accuracy": round(sum(row["status_correct"] for row in rows) / n, 4),
            "answer_value_accuracy": round(sum(row["value_correct"] for row in answerable) / len(answerable), 4),
            "citation_accuracy": round(sum(row["citation_correct"] for row in answerable) / len(answerable), 4),
            "grounded_answer_rate": round(sum(row["grounded_answer"] for row in answerable) / len(answerable), 4),
            "correct_refusal_rate": round(sum(row["status_correct"] for row in refusals) / len(refusals), 4)}


def run() -> dict[str, Any]:
    dataset = _load_dataset()
    pages = _load_pages(dataset)
    if not MODEL_SNAPSHOT.is_dir():
        raise ValueError("model_snapshot_missing")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(str(MODEL_SNAPSHOT), local_files_only=True, device="cpu")
    rows = []
    for index, case in enumerate(dataset["cases"], 1):
        rows.append(_run_case(case, pages, model))
        print(f"E2E {index}/{len(dataset['cases'])} {case['id']}", flush=True)
    result = {"status": "ok", "evaluated_at": datetime.now(UTC).isoformat(),
              "dataset_sha256": sha(DATASET_PATH), "model": {"name": MODEL_NAME, "revision": MODEL_REVISION},
              "protocol": {"top_k": TOP_K, "candidate_budget": CANDIDATE_BUDGET, "semantic_budget": SEMANTIC_BUDGET,
                           "answer_policy": "answer only when a field-labelled numeric line is found; otherwise refuse",
                           "online_path_changed": False},
              "metrics": _summary(rows), "cases": rows,
              "limitations": ["17条控制留出，仍只有2025年三份中文年报", "回答抽取为确定性基线，不代表LLM生成质量", "期间/单位/列语义仍需独立人工门禁"]}
    return result


def render(result: dict[str, Any]) -> str:
    m = result["metrics"]
    return "\n".join(["# 中文投研问答端到端闭环评测", "", "## 结果", "",
        f"- 样本：{m['cases']}（可回答 {m['answerable']}，应拒答 {m['refusal_cases']}）",
        f"- 状态判断准确率：{m['status_accuracy']:.2%}",
        f"- 可回答题数值正确率：{m['answer_value_accuracy']:.2%}",
        f"- 可回答题引用身份正确率：{m['citation_accuracy']:.2%}",
        f"- 有据回答率：{m['grounded_answer_rate']:.2%}",
        f"- 正确拒答率：{m['correct_refusal_rate']:.2%}", "", "## 闭环定义", "",
        "问题 → 同标的候选召回 → 语义排序 → 证据行抽取 → 有据回答或拒答 → 数值/引用/拒答验收。", "",
        "本评测未修改默认线上路径，且不把字段数值共现等同于完整的期间、单位和列语义证明。", ""]) + "\n"


def main() -> int:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run()
    except Exception as exc:
        failure = {"status": "BLOCKED", "reason": f"{type(exc).__name__}:{exc}", "evaluated_at": datetime.now(UTC).isoformat()}
        RESULT_PATH.with_name("chinese_answer_e2e_v1_blocked.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("BLOCKED", failure["reason"], flush=True)
        return 1
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_PATH.write_text(render(result), encoding="utf-8")
    print(json.dumps(result["metrics"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
