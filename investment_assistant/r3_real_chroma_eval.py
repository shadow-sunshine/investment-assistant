"""R3 独立留出集上的真实 Chroma 召回对照：验证冻结 query-only 词面扩展在真实检索链路上的增益。

设计要点
--------
- **真实 Chroma（本地 hash 模式）**：继承 `rag.LocalResearchRAG`，索引到**临时隔离目录**，绝不触碰默认
  `data/chroma` 用户索引，也不修改 `rag.py` 检索/排序逻辑与线上默认路径。
- **两臂 A/B（唯一变量 = 冻结词面扩展）**：
  - baseline：原始用户 `question`；
  - treatment：原始 `question` + 冻结 `financial_term_map.json` 的词面扩展（`rag.expand_financial_query`，
    已冻结，未为 R3 临时补词）。
- **硬防泄漏**：检索输入仅含 `question` 与冻结术语扩展；`keywords` / 答案数值 / `target_pages` /
  `evidence_snippet` 仅供结果核验，**绝不传入查询扩展或打分器**（见 CURRENT_TASK.md 防泄漏规则 #3）。
- **统一相关性判定**：全部复用 `bilingual_eval` 的 `_is_target_source`（ticker + 页码双校验）、`_keyword_match`、
  `_aggregate`、`_failure_reason`，避免自造口径。
- **fail-closed 护栏**：holdout SHA / 术语扩展字典 SHA / 资料快照 sha256 任一漂移，均在**建索引前** BLOCKED，
  不产出任何可比指标。
- **不静默 fallback**：强制 `RAG_EMBEDDING_MODE=hash`、`RAG_RERANKER_MODE=disabled`；semantic / cross-encoder
  需要本地模型缓存，本次不作为独立实验臂运行（记录实际模式与降级原因，不把 hash 成绩记为 semantic）。

用法
----
    python -m investment_assistant.r3_real_chroma_eval --top-k 4
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bilingual_eval import (
    QUADRANT_LABEL,
    QUADRANT_ORDER,
    MaterialDriftError,
    _aggregate,
    _failure_reason,
    _is_target_source,
    _keyword_match,
    _page_of,
    assert_materials_comparable,
    verify_materials,
)
from .config import DATA_DIR, KNOWLEDGE_DIR
from .rag import (
    CROSS_ENCODER_CANDIDATE_LIMIT,
    LocalResearchRAG,
    _anchor_score,
    _lexical_overlap,
    expand_financial_query,
)

RESULT_DIR = DATA_DIR / "evaluations"
# v2 独立留出集：与 R0 在 (ticker,question) 与 (ticker,target_page,normalized keywords) 两层均无重复。
# 旧的 r3_holdout_eval_set.json（v1）保留不覆盖，仅作为 exploratory_blocked_non_independent 历史证据。
HOLDOUT_PATH = DATA_DIR / "r3_holdout_eval_set_v3.json"
TERM_MAP_PATH = DATA_DIR / "financial_term_map.json"
# 历史默认位置（data/evaluations/r3_chroma_tmp）。本修复后默认运行不再使用该固定目录，
# 而是每次生成一次性临时目录（tempfile.TemporaryDirectory）并自动清理；该常量仅保留作兼容性/参考，
# 且本实验永不无条件删除它或任何用户已有目录。
DEFAULT_TEMP_CHROMA = RESULT_DIR / "r3_chroma_tmp"
R3_TEMP_PREFIX = "r3_"
TOP_K_FROZEN = 4
ARMS = ("baseline", "treatment")

# 冻结的 R3 holdout / 术语扩展字典 SHA256 —— 由本里程碑首次固化，建索引前逐项校验。
# 来源：本次新建 `data/r3_holdout_eval_set_v3.json`，先逐页标注 gold page 并保存，再计算 SHA 并冻结。
# v1/v2 SHA 仅作为历史结果证据，不得继续当新实验基线。
FROZEN_R3_HOLDOUT_SHA256 = "8da7a9c10b473c17984c954790c1c94b875d75c77eee1303f34267751665a85a"
# 来源：data/financial_term_map.json（R0 起沿用，R3 未改动；若改动即视为策略漂移，须 BLOCKED）。
FROZEN_TERM_MAP_SHA256 = "66a0ce85220d200bc71979365916fba0194c108d7e856a3ea0c494a741c1dfb6"


class TempChromaDirError(RuntimeError):
    """临时 Chroma 目录安全校验失败（路径穿越 / 前缀不符 / 非空现存目录）。fail-closed，绝不删除用户目录。"""


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def _holdout_fact_keys(case: dict[str, Any]) -> set[tuple[Any, Any, str]]:
    """把一条样本展开为 (ticker, target_page, normalized_keyword) 事实键集合，用于双层独立性比对。"""
    keys: set[tuple[Any, Any, str]] = set()
    for page in case.get("target_pages", []):
        for kw in case.get("keywords", []):
            keys.add((str(case["ticker"]).upper(), int(page), _norm(kw)))
    return keys


def check_independence_vs_r0(
    holdout: dict[str, Any], r0_path: Path = DATA_DIR / "bilingual_eval_set.json"
) -> dict[str, Any]:
    """与 R0 双层独立性校验（fail-closed 的前置判定）。

    返回 {level1_overlaps, level2_overlaps, ok, total, per_quadrant}：
    - level1：(ticker, question) 规范化后与 R0 完全重复 → 列出重复 id；
    - level2：(ticker, target_page, normalized keywords) 复用同一事实 → 列出 (id, fact)；
    - ok：两层均零重复、总数 32、四象限各 8。
    """
    r0 = json.loads(Path(r0_path).read_text(encoding="utf-8-sig"))
    r0_questions = {(str(c["ticker"]).upper(), _norm(c["question"])) for c in r0["cases"]}
    r0_facts: set[tuple[Any, Any, str]] = set()
    for c in r0["cases"]:
        r0_facts |= _holdout_fact_keys(c)

    cases = holdout["cases"]
    level1: list[str] = []
    for c in cases:
        if (str(c["ticker"]).upper(), _norm(c["question"])) in r0_questions:
            level1.append(c["id"])
    level2: list[tuple[str, tuple[Any, Any, str]]] = []
    for c in cases:
        for fact in _holdout_fact_keys(c):
            if fact in r0_facts:
                level2.append((c["id"], fact))

    # R0 独立不等于 holdout 内部独立：跨语言象限不能用同一页同一事实
    # 仅改写问题语言来凑样本，否则会高估样本量并污染四象限对照。
    fact_to_cases: dict[tuple[Any, Any, str], list[str]] = {}
    for c in cases:
        for fact in _holdout_fact_keys(c):
            fact_to_cases.setdefault(fact, []).append(c["id"])
    within_holdout_overlaps = [
        {"fact": list(fact), "case_ids": ids}
        for fact, ids in fact_to_cases.items()
        if len(ids) > 1
    ]

    per_quadrant: dict[str, int] = {}
    for c in cases:
        per_quadrant[c["quadrant"]] = per_quadrant.get(c["quadrant"], 0) + 1
    ok = (
        not level1
        and not level2
        and not within_holdout_overlaps
        and len(cases) == 32
        and per_quadrant == {"zh-zh": 8, "en-en": 8, "zh-en": 8, "en-zh": 8}
    )
    return {
        "level1_overlaps": level1,
        "level2_overlaps": level2,
        "within_holdout_overlaps": within_holdout_overlaps,
        "ok": ok,
        "total": len(cases),
        "per_quadrant": per_quadrant,
    }


def _resolve_temp_chroma_dir(temp_chroma_dir: Path | None) -> Path | None:
    """安全解析临时 Chroma 目录。

    - None → 返回 None（调用方改用 tempfile.TemporaryDirectory 生成唯一目录并自动清理）；
    - 传入路径 → 必须严格位于 RESULT_DIR(data/evaluations) 内且以 'r3_' 开头（禁止路径穿越 /
      项目根 / data/chroma / 其他共享索引目录；否则抛 TempChromaDirError）；若已存在且非空 →
      抛 TempChromaDirError（fail-closed，绝不无条件 rmtree 用户已有文件）；否则返回该路径。
    """
    if temp_chroma_dir is None:
        return None
    p = Path(temp_chroma_dir).resolve()
    result_root = RESULT_DIR.resolve()
    if p != result_root and result_root not in p.parents:
        raise TempChromaDirError(
            f"临时 Chroma 目录 {p} 不在允许的 data/evaluations 之下，拒绝使用（防路径穿越）"
        )
    if not p.name.startswith(R3_TEMP_PREFIX):
        raise TempChromaDirError(f"临时 Chroma 目录必须以 '{R3_TEMP_PREFIX}' 前缀命名：{p}")
    if p.exists() and any(p.iterdir()):
        # 非空现存目录：fail-closed，绝不删除用户文件
        raise TempChromaDirError(f"临时 Chroma 目录 {p} 已存在且非空，拒绝覆盖/删除；请改用唯一目录或清空后重试")
    return p


def build_query(question: str, expand: bool) -> str:
    """检索实际使用的查询串；expansion 只来自冻结术语字典，绝不读 gold keywords/答案值。"""
    return question if not expand else expand_financial_query(question)


class R3ResearchRAG(LocalResearchRAG):
    """在真实 Chroma 上运行检索；唯一扩展点是 `expand` 开关（控制是否施加冻结词面扩展）。

    除 `expanded_query` 一行随 `expand` 变化外，向量召回 / 混合重排 / 页级去重完全复用父类（rag.py）逻辑，
    保证与线上默认路径口径一致；`search` 不读取评测集的 keywords / target_pages / evidence 字段。
    """

    def search(self, query: str, limit: int = 4, ticker: str | None = None, expand: bool = True) -> list[dict[str, Any]]:
        normalized_ticker = ticker.upper().strip() if ticker else None
        if self.collection.count() == 0:
            return []
        where = {"ticker": normalized_ticker} if normalized_ticker else None
        matching_count = (
            self.collection.count()
            if where is None
            else len(self.collection.get(where=where, include=[]).get("ids", []))
        )
        if matching_count == 0:
            return []
        expanded_query = build_query(query, expand)
        cross_encoder_active = self.reranker.mode == "cross_encoder"
        candidate_limit = min(
            CROSS_ENCODER_CANDIDATE_LIMIT if cross_encoder_active else max(limit * 12, 48), matching_count
        )
        response = self.collection.query(
            query_embeddings=self.provider.embed([expanded_query]),
            n_results=min(candidate_limit, matching_count),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        documents = response.get("documents", [[]])[0]
        metadata = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]
        candidates = []
        for index, document in enumerate(documents):
            distance = float(distances[index]) if index < len(distances) else 1.0
            vector_score = 1.0 / (1.0 + max(distance, 0.0))
            lexical_score = _lexical_overlap(expanded_query, document)
            rerank_score = round(vector_score * 0.7 + lexical_score * 0.3, 6)
            candidates.append(
                {
                    "content": document,
                    "metadata": metadata[index],
                    "distance": distance,
                    "vector_score": round(vector_score, 6),
                    "lexical_score": round(lexical_score, 6),
                    "rerank_score": rerank_score,
                    "anchor_score": _anchor_score(expanded_query, document),
                }
            )
        cross_scores = self.reranker.score(expanded_query, [item["content"] for item in candidates])
        if cross_scores is not None:
            for item, score in zip(candidates, cross_scores):
                item["cross_encoder_score"] = round(score, 6)
                item["rerank_score"] = round(score, 6)
        page_candidates: dict[tuple[Any, Any], dict[str, Any]] = {}
        for item in candidates:
            item_metadata = item["metadata"]
            source_key = (
                (item_metadata.get("source_id"), item_metadata.get("page"))
                if item_metadata.get("source_type") == "pdf"
                else (item_metadata.get("source_id"), item_metadata.get("chunk_index"))
            )
            existing = page_candidates.get(source_key)
            if existing is None:
                page_candidates[source_key] = {"display": item, "page_score": item["rerank_score"]}
                continue
            existing["page_score"] = max(existing["page_score"], item["rerank_score"])
            display = existing["display"]
            if (item["anchor_score"], item["rerank_score"]) > (display["anchor_score"], display["rerank_score"]):
                existing["display"] = item
        deduplicated = [
            entry["display"]
            for entry in sorted(page_candidates.values(), key=lambda entry: entry["page_score"], reverse=True)[:limit]
        ]
        return [{**item, "citation": f"S{index + 1}"} for index, item in enumerate(deduplicated)]


def run_case(rag: R3ResearchRAG, case: dict[str, Any], top_k: int, expand: bool) -> dict[str, Any]:
    scoped = rag.search(case["question"], limit=top_k, ticker=case["ticker"], expand=expand)
    unscoped = rag.search(case["question"], limit=top_k, expand=expand)
    relevant = [s for s in scoped if _is_target_source(s, case)]
    forbidden = set(case.get("forbidden_tickers", []))
    unscoped_tickers = [str(s["metadata"].get("ticker", "")) for s in unscoped]
    cross = [t for t in unscoped_tickers if t in forbidden]
    result: dict[str, Any] = {
        "id": case["id"],
        "quadrant": case["quadrant"],
        "ticker": case["ticker"],
        "question": case["question"],
        "target_pages": case["target_pages"],
        "keywords": case["keywords"],
        "retrieved_pages": [_page_of(s) for s in scoped],
        "page_hit_at_k": bool(relevant),
        "keyword_verified_hit_at_k": any(_keyword_match(s["content"], case["keywords"]) for s in relevant),
        "citation_page_relevance": round(len(relevant) / top_k, 4),
        "top_result_page": _page_of(scoped[0]) if scoped else None,
        "top_result_is_relevant": bool(scoped and _is_target_source(scoped[0], case)),
        "retrieved_files": [s["metadata"].get("file_name") for s in scoped],
        "unscoped_tickers": unscoped_tickers,
        "unscoped_pages": [_page_of(s) for s in unscoped],
        "cross_ticker_source_count_unscoped": len(cross),
    }
    result["failure_reason"] = _failure_reason(result, case)
    return result


def _run_arm(rag: R3ResearchRAG, cases: list[dict[str, Any]], top_k: int, expand: bool) -> dict[str, Any]:
    run = [run_case(rag, case, top_k, expand) for case in cases]
    by_quadrant = {q: _aggregate([c for c in run if c["quadrant"] == q]) for q in QUADRANT_ORDER}
    reasons: dict[str, int] = {}
    for c in run:
        reasons[c["failure_reason"]] = reasons.get(c["failure_reason"], 0) + 1
    return {
        "arm": "treatment" if expand else "baseline",
        "expand": expand,
        "top_k": top_k,
        "metrics": _aggregate(run),
        "by_quadrant": by_quadrant,
        "failure_reason_counts": reasons,
        "cases": run,
    }


def _release_chroma_handles(temp_path: Path) -> None:
    """释放 chromadb.PersistentClient 在进程内持有的 OS 文件句柄（Windows 上 sqlite/hnsw mmap 锁）。

    chromadb 把 System 按 persist_directory 缓存在 `SharedSystemClient._identifier_to_system`，
    进程内持续持有 sqlite + hnsw 文件锁；仅 drop 引用 + `gc.collect()` 不足以释放（句柄在 Rust 层），
    `clear_system_cache()` 也只清 dict、不调用 `system.stop()`。必须在删除目录前显式 `system.stop()`
    关闭句柄，否则 Windows 上 `TemporaryDirectory.cleanup()` / `shutil.rmtree` 抛
    `PermissionError [WinError 32]`。本函数 best-effort：任何异常都被吞掉，真正的删除在调用方
    finally 中兜底。
    """
    try:
        from chromadb.api import shared_system_client as ssc

        systems = getattr(ssc.SharedSystemClient, "_identifier_to_system", None)
        if not systems:
            return
        key = str(temp_path)
        target = Path(temp_path).resolve()
        for k, system in list(systems.items()):
            try:
                matched = (k == key) or (Path(k).resolve() == target)
            except Exception:
                matched = k == key
            if matched:
                try:
                    system.stop()
                except Exception:
                    pass
        try:
            ssc.SharedSystemClient.clear_system_cache()
        except Exception:
            pass
        # 释放被停止 System 的 Python 引用，帮助 Rust Drop 尽快回收文件句柄
        gc.collect()
    except Exception:
        pass


def _retry_rmtree(path: Path, attempts: int = 6, delay: float = 0.3) -> bool:
    """Windows 上 chroma 文件锁可能延迟释放：直接重试 `shutil.rmtree`（绕过 TemporaryDirectory
    的 _closed 守卫），给 OS 一点时间释放句柄。返回是否最终删除成功。"""
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except (PermissionError, OSError):
            time.sleep(delay)
    return False


def run_experiment(top_k: int = TOP_K_FROZEN, holdout_path: Path = HOLDOUT_PATH, temp_chroma_dir: Path | None = None) -> dict[str, Any]:
    if top_k != TOP_K_FROZEN:
        raise ValueError(f"R3 实验 Top-K 冻结为 {TOP_K_FROZEN}，与 R0/R2 一致")
    # 强制本地 hash 模式 + 关闭重排器，杜绝静默 fallback / 非确定性
    os.environ["RAG_EMBEDDING_MODE"] = "hash"
    os.environ["RAG_RERANKER_MODE"] = "disabled"

    holdout = json.loads(holdout_path.read_text(encoding="utf-8-sig"))
    current_holdout_sha = sha256_of_file(holdout_path)
    current_term_sha = sha256_of_file(TERM_MAP_PATH)
    material_report = verify_materials(holdout["materials"])
    drift = [t for t, v in material_report.items() if v.get("status") != "match"]
    evaluated_at = datetime.now(UTC).isoformat()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    # 护栏 1：holdout SHA 与冻结基线不一致 → 建索引前 BLOCKED
    if current_holdout_sha != FROZEN_R3_HOLDOUT_SHA256:
        return _blocked_payload(
            "holdout_sha_mismatch", current_holdout_sha, current_term_sha, material_report, drift,
            evaluated_at, top_k, "holdout SHA 与冻结基线不一致，拒绝给出可比结论",
        )
    # 护栏 2：术语扩展字典（策略）漂移 → 建索引前 BLOCKED（视为泄漏风险）
    if current_term_sha != FROZEN_TERM_MAP_SHA256:
        return _blocked_payload(
            "strategy_drift", current_holdout_sha, current_term_sha, material_report, drift,
            evaluated_at, top_k, "financial_term_map.json SHA 与冻结策略不一致，拒绝运行",
        )
    # 护栏 3：资料快照漂移 / 缺失 → 建索引前 BLOCKED
    try:
        assert_materials_comparable(material_report)
    except MaterialDriftError:
        return _blocked_payload(
            "material_drift", current_holdout_sha, current_term_sha, material_report, drift,
            evaluated_at, top_k, "资料快照与 holdout 记录不一致，拒绝给出可比结论",
        )
    # 护栏 4：与 R0 双层独立性校验（建索引前 fail-closed；重复即拒绝，不产出可比指标）
    independence = check_independence_vs_r0(holdout)
    if not independence["ok"]:
        return _blocked_payload(
            "independence_failure", current_holdout_sha, current_term_sha, material_report, drift,
            evaluated_at, top_k,
            f"独立性校验失败：R0 level1={len(independence['level1_overlaps'])}，"
            f"R0 level2={len(independence['level2_overlaps'])}，"
            f"holdout 内部={len(independence['within_holdout_overlaps'])} 重复；拒绝给出可比结论",
        )

    # 临时 Chroma 目录安全解析：None → 一次性唯一临时目录（自动清理）；传入路径 → 严格受限且非空即拒绝（绝不删除）
    try:
        provided = _resolve_temp_chroma_dir(temp_chroma_dir)
    except TempChromaDirError as exc:
        return _blocked_payload(
            "temp_chroma_dir_rejected", current_holdout_sha, current_term_sha, material_report, drift,
            evaluated_at, top_k, f"临时 Chroma 目录安全校验未通过：{exc}",
        )
    if provided is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="r3_chroma_")
        temp_path = Path(tmp_ctx.name)
        own_ctx = tmp_ctx
    else:
        temp_path = provided
        own_ctx = None
        temp_path.mkdir(parents=True, exist_ok=True)

    rag: R3ResearchRAG | None = None
    try:
        # 索引到临时隔离目录（不触碰默认 CHROMA_DIR，也不删除用户已有目录）
        rag = R3ResearchRAG(path=temp_path)
        status = rag.retrieval_status()
        indexed: dict[str, int] = {}
        for ticker, spec in holdout["materials"].items():
            pdf_path = KNOWLEDGE_DIR / spec["file_name"]
            if not pdf_path.exists():
                return _blocked_payload(
                    "material_missing", current_holdout_sha, current_term_sha, material_report, drift,
                    evaluated_at, top_k, f"资料缺失：{pdf_path}",
                )
            indexed[ticker] = rag.index_pdf(pdf_path)

        numeric_query_cases = sum(1 for c in holdout["cases"] if any(ch.isdigit() for ch in c["question"]))
        results: dict[str, Any] = {}
        timing: dict[str, float] = {}
        for arm in ARMS:
            expand = arm == "treatment"
            t0 = time.perf_counter()
            results[arm] = _run_arm(rag, holdout["cases"], top_k, expand)
            timing[arm] = round(time.perf_counter() - t0, 4)

        # 线上 A/B 决策：增益必须明确，且任何跨标的污染指标都不得恶化。
        # 不能只看“Top-4 全错”这一项；平均污染和出现污染的样本数同样是安全指标。
        b_metrics = results["baseline"]["metrics"]
        t_metrics = results["treatment"]["metrics"]
        b_recall = b_metrics["page_recall_at_k"]
        t_recall = t_metrics["page_recall_at_k"]
        gain = round(t_recall - b_recall, 4)
        b_cross_avg = b_metrics["cross_ticker_sources_per_case_unscoped"]
        t_cross_avg = t_metrics["cross_ticker_sources_per_case_unscoped"]
        b_cross_cases = sum(1 for c in results["baseline"]["cases"] if c["cross_ticker_source_count_unscoped"] > 0)
        t_cross_cases = sum(1 for c in results["treatment"]["cases"] if c["cross_ticker_source_count_unscoped"] > 0)
        b_all_wrong = sum(1 for c in results["baseline"]["cases"] if c["cross_ticker_source_count_unscoped"] >= 4)
        t_all_wrong = sum(1 for c in results["treatment"]["cases"] if c["cross_ticker_source_count_unscoped"] >= 4)
        pollution_worsened = t_cross_avg > b_cross_avg or t_cross_cases > b_cross_cases or t_all_wrong > b_all_wrong
        if gain <= 0 or pollution_worsened:
            decision = "no_online_ab"
            decision_reason = (
                f"treatment 相对 baseline 的 Page Recall@4 增益为 {gain}；"
                f"跨标的污染平均值 baseline={b_cross_avg}/treatment={t_cross_avg}，"
                f"出现污染的样本数 baseline={b_cross_cases}/treatment={t_cross_cases}，"
                f"Top-4 全错 baseline={b_all_wrong}/treatment={t_all_wrong}；"
                "增益不足或至少一项污染指标恶化，不进入线上 A/B。"
            )
        else:
            decision = "conditional_online_ab"
            decision_reason = (
                f"treatment 相对 baseline 的 Page Recall@4 增益为 {gain}，且跨标的污染指标未恶化 "
                f"（平均值 {b_cross_avg}->{t_cross_avg}，出现污染样本 {b_cross_cases}->{t_cross_cases}，"
                f"Top-4 全错 {b_all_wrong}->{t_all_wrong}）；"
                "仍需更大样本与切流计划，不能直接全量上线。"
            )

        payload = {
            "status": "ok",
            "experiment": "R3_real_chroma_holdout_v3",
            "holdout": str(holdout_path),
            "holdout_sha256": current_holdout_sha,
            "frozen_r3_holdout_sha256": FROZEN_R3_HOLDOUT_SHA256,
            "holdout_matches_frozen": current_holdout_sha == FROZEN_R3_HOLDOUT_SHA256,
            "term_map": str(TERM_MAP_PATH),
            "term_map_sha256": current_term_sha,
            "frozen_term_map_sha256": FROZEN_TERM_MAP_SHA256,
            "term_map_matches_frozen": current_term_sha == FROZEN_TERM_MAP_SHA256,
            "top_k": top_k,
            "evaluated_at": evaluated_at,
            "embedding_mode": status["embedding_mode"],
            "reranker_mode": status["reranker_mode"],
            "embedding_fallback_reason": status["fallback_reason"],
            "reranker_fallback_reason": status["reranker_fallback_reason"],
            "indexed_chunks": indexed,
            "material_verification": material_report,
            "material_drift": drift,
            "arms": list(ARMS),
            "numeric_query_cases": numeric_query_cases,
            "numeric_query_case_total": len(holdout["cases"]),
            "independence_check": independence,
            "online_ab_decision": decision,
            "online_ab_reason": decision_reason,
            "online_ab_guard_metrics": {
                "page_recall_gain": gain,
                "cross_ticker_sources_per_case_unscoped": {"baseline": b_cross_avg, "treatment": t_cross_avg},
                "cross_ticker_polluted_cases": {"baseline": b_cross_cases, "treatment": t_cross_cases},
                "top_k_all_wrong_ticker_cases": {"baseline": b_all_wrong, "treatment": t_all_wrong},
            },
            "timing_seconds": timing,
            "results": results,
        }
        _write_payload(payload)
        return payload
    finally:
        # 先释放 chroma 在进程内持有的文件句柄（Windows sqlite/hnsw 锁），否则删除目录会抛 PermissionError。
        # 顺序很关键：先丢弃 rag（连同 PersistentClient/System 的 Python 引用）并 gc，再 stop 系统，
        # 否则 client 仍持有 Rust 句柄，stop 无法彻底释放。
        rag = None
        gc.collect()
        if temp_path is not None:
            _release_chroma_handles(temp_path)
        if own_ctx is not None:
            try:
                own_ctx.cleanup()  # 仅默认临时目录自动清理；调用方提供的目录一律不动
            except (PermissionError, OSError) as exc:
                # TemporaryDirectory.cleanup() 失败后会置 _closed=True，二次调用为 no-op；
                # 直接在兜底中重试 rmtree（Windows 锁偶发延迟释放）。
                removed = _retry_rmtree(temp_path) if temp_path is not None else False
                if not removed:
                    # best-effort：句柄已尽量释放，若仍被占用则跳过自动清理，绝不因此让进程以非零退出。
                    print(
                        f"[warn] 临时 Chroma 目录自动清理失败（可能仍被占用），已跳过："
                        f"{getattr(own_ctx, 'name', own_ctx)} ({type(exc).__name__})"
                    )


def _blocked_payload(
    reason: str,
    holdout_sha: str,
    term_sha: str,
    material_report: dict[str, Any],
    drift: list[str],
    evaluated_at: str,
    top_k: int,
    note: str,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blocked_reason": reason,
        "experiment": "R3_real_chroma_holdout",
        "holdout": str(HOLDOUT_PATH),
        "holdout_sha256": holdout_sha,
        "frozen_r3_holdout_sha256": FROZEN_R3_HOLDOUT_SHA256,
        "holdout_matches_frozen": holdout_sha == FROZEN_R3_HOLDOUT_SHA256,
        "term_map_sha256": term_sha,
        "frozen_term_map_sha256": FROZEN_TERM_MAP_SHA256,
        "term_map_matches_frozen": term_sha == FROZEN_TERM_MAP_SHA256,
        "top_k": top_k,
        "evaluated_at": evaluated_at,
        "material_verification": material_report,
        "material_drift": drift,
        "note": note,
        "results": {},
    }


def _write_payload(payload: dict[str, Any]) -> None:
    # v3 为通过 R0 + holdout 内部双重独立性检查后的结果；v1/v2 历史产物保留不覆盖。
    (RESULT_DIR / "r3_real_chroma_v3.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / "r3_real_chroma_v3.md").write_text(render_report(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_report(payload: dict[str, Any]) -> str:
    if payload.get("status") == "blocked":
        return _render_blocked_report(payload)
    results = payload["results"]
    lines = [
        "# R3 独立留出集 · 真实 Chroma(hash) 召回对照",
        "",
        f"- 评测时间：{payload['evaluated_at']}",
        f"- holdout：`{HOLDOUT_PATH.name}`（SHA `{payload['holdout_sha256'][:12]}…`，"
        f"与冻结基线 {'一致' if payload['holdout_matches_frozen'] else '不一致'}；共 32 条，四象限各 8 条）",
        f"- 术语扩展策略：`data/financial_term_map.json`（SHA `{payload['term_map_sha256'][:12]}…`，"
        f"与冻结策略 {'一致' if payload['term_map_matches_frozen'] else '不一致'}）",
        f"- 实际 embedding 模式：`{payload['embedding_mode']}`；reranker 模式：`{payload['reranker_mode']}`"
        + (f"；降级原因：{payload['embedding_fallback_reason']}" if payload.get("embedding_fallback_reason") else ""),
        f"- Top-K 冻结为 {payload['top_k']}；两臂 = baseline(原始问题) vs treatment(问题+冻结词面扩展)；"
        "唯一变量为冻结术语扩展，检索不读取任何 gold 标签",
        f"- 含数值/年份的查询：{payload['numeric_query_cases']}/{payload['numeric_query_case_total']}"
        "（数值锚点仅来自问题文本，绝不取答案值）",
        f"- 索引块数：{payload['indexed_chunks']}",
        "",
        "## 1. 总体四象限指标（Page Recall@4 / 关键词-数值核验 Recall@4 / Top-1）",
        "",
        "| 指标 | baseline | treatment |",
        "|---|---:|---:|",
    ]
    for key, label in [
        ("page_recall_at_k", "Page Recall@4"),
        ("keyword_verified_recall_at_k", "关键词/数值核验 Recall@4"),
        ("top1_page_relevance", "Top-1 页面相关性"),
        ("citation_page_relevance", "引用页面相关率@4"),
        ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条（unscoped）"),
    ]:
        lines.append(f"| {label} | {results['baseline']['metrics'].get(key)} | {results['treatment']['metrics'].get(key)} |")

    lines.extend(["", "## 2. 按象限拆分（Page Recall@4 / 核验 Recall@4 / Top-1）", ""])
    for quad in QUADRANT_ORDER:
        lines.append(f"### {QUADRANT_LABEL[quad]}")
        lines.append("| 指标 | baseline | treatment |")
        lines.append("|---|---:|---:|")
        for key, label in [
            ("page_recall_at_k", "Page Recall@4"),
            ("keyword_verified_recall_at_k", "核验 Recall@4"),
            ("top1_page_relevance", "Top-1"),
            ("cross_ticker_sources_per_case_unscoped", "跨标的来源/条"),
        ]:
            lines.append(f"| {label} | {results['baseline']['by_quadrant'][quad].get(key)} | {results['treatment']['by_quadrant'][quad].get(key)} |")
        lines.append("")

    lines.extend(["", "## 3. 失败归因分布", ""])
    all_reasons = set(results["baseline"].get("failure_reason_counts", {})) | set(results["treatment"].get("failure_reason_counts", {}))
    reason_labels = {
        "none": "命中（无失败）",
        "cross_language_miss": "跨语言未召回",
        "candidate_miss": "同语言候选未召回",
        "chunk_or_value_boundary": "页命中但数值不在块内",
        "ranking": "已召回但排序未进 Top-1",
    }
    lines.append("| 归因 | baseline | treatment |")
    lines.append("|---|---:|---:|")
    for reason in sorted(all_reasons, key=lambda r: list(reason_labels).index(r) if r in reason_labels else 99):
        lines.append(
            f"| {reason_labels.get(reason, reason)} | {results['baseline'].get('failure_reason_counts', {}).get(reason, 0)}"
            f" | {results['treatment'].get('failure_reason_counts', {}).get(reason, 0)} |"
        )

    lines.extend(["", "## 4. 跨标的污染（unscoped 臂）", ""])
    for label, selector in [
        ("出现跨标的来源的样本数", lambda c: c["cross_ticker_source_count_unscoped"] > 0),
        ("Top-K 全来自错误标的的样本数", lambda c: len(c["unscoped_tickers"]) >= 4 and c["cross_ticker_source_count_unscoped"] >= 4),
    ]:
        b = sum(1 for c in results["baseline"]["cases"] if selector(c))
        t = sum(1 for c in results["treatment"]["cases"] if selector(c))
        lines.append(f"| {label}（共 32 条） | {b} | {t} |")

    lines.extend(["", "## 5. 耗时（秒，索引 + 两臂检索）", ""])
    for arm in ARMS:
        lines.append(f"- {arm}: {payload['timing_seconds'].get(arm)}")

    lines.extend(
        [
            "",
            "## 6. 适用范围、未验证项与是否进入线上 A/B",
            "",
        "- **适用范围**：本实验在真实 Chroma(hash) 索引上，用冻结 query-only 词面扩展对照 baseline，"
        "量化扩展对四象限「金标准页进入 Top-4/Top-1」的增量；gold page 先于检索固化，检索不读取任何 gold 标签。",
        "- **独立性验收**：R0 level1(ticker,question) 重复 "
        f"{len(payload.get('independence_check', {}).get('level1_overlaps', []))} 条，"
        f"R0 level2(ticker,page,keyword) 重复 {len(payload.get('independence_check', {}).get('level2_overlaps', []))} 条，"
        f"holdout 内部事实重复 {len(payload.get('independence_check', {}).get('within_holdout_overlaps', []))} 条 "
        "—— 三项均为 0，独立留出集验收通过；v1/v2 历史产物均不作为线上结论。",
        "- **未验证项**：① semantic / cross-encoder 需本地模型缓存，本次不作为独立实验臂（不静默 fallback）；"
        "② 仅 32 条小样本，不做统计显著性外推；③ 仅覆盖四份冻结 PDF，未含新闻/网页等动态语料。",
        "- **剩余风险**：pypdf 对 CFF 字体 PDF 抽取存在已知噪音（fontTools 缺失告警），可能影响个别英文页字面分；"
        "中文页抽取经验证完整。冻结术语扩展为单向（中→英），en-zh 象限无对应扩展方向，属已知结构限制。",
        "- **是否进入线上 A/B**：见第 7 节结论。",
            "",
            "## 7. 结论（R2 → R3 对照）",
            "",
        f"- **线上 A/B 决策：`{payload.get('online_ab_decision', 'n/a')}`** —— {payload.get('online_ab_reason', '')}",
        "- 若 treatment 仅在 zh-en 象限相对 baseline 提升、且其它象限两臂持平（en-en/en-zh 无扩展方向、zh-zh 同语言已工作），"
        "则增益被**限定在中文→英文扩展方向**，不能直接外推为全局线上改动；",
        "- 若任一象限出现 treatment < baseline（扩展引入噪音），应保守，不进入线上；",
        "- 任何结论均不构成直接修改 `rag.py` 的依据；线上改动须另立切流计划与更大样本验证。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_blocked_report(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# R3 真实 Chroma 召回对照 —— BLOCKED",
            "",
            f"- 评测时间：{payload['evaluated_at']}",
            f"- **状态：BLOCKED（{payload['blocked_reason']}）—— 未建索引、未跑检索、未产出任何可比指标**",
            f"- 说明：{payload.get('note', '')}",
            "",
            "| 标的 | 状态 | 预期 sha256 | 实际 sha256 |",
            "|---|---|---|---|",
        ]
        + [
            f"| {t} | {item.get('status', '未校验')} | {item.get('expected_sha256', '-')} | {item.get('actual_sha256', '-')} |"
            for t, item in payload.get("material_verification", {}).items()
        ]
        + [
            "",
            "> holdout SHA / 术语扩展字典 SHA / 资料快照任一漂移即拒绝评测（fail-closed），不把不可比结果当作结论。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="R3 独立留出集真实 Chroma 召回对照")
    parser.add_argument("--top-k", type=int, default=TOP_K_FROZEN)
    parser.add_argument("--holdout", type=Path, default=HOLDOUT_PATH)
    parser.add_argument(
        "--temp-chroma", type=Path, default=None,
        help="可选：限定临时 Chroma 目录（须位于 data/evaluations 下且以 r3_ 开头，非空现存目录会被拒绝）；"
        "留空则用一次性临时目录并自动清理",
    )
    args = parser.parse_args()
    payload = run_experiment(args.top_k, args.holdout, args.temp_chroma)
    if payload.get("status") == "blocked":
        print(f"BLOCKED：{payload['blocked_reason']}；未产出任何指标。")
        return 1
    for arm in ARMS:
        print(arm, json.dumps(payload["results"][arm]["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
