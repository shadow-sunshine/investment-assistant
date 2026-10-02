"""R5 证据与交付门禁：Claim/Evidence 统一模型、fail-closed 逐条校验与交付门禁。

设计边界（不破坏冻结区）
------------------------
* 不修改 ``rag.py`` / ``workflow.py`` / ``llm_generation.py`` / ``safety.py``；
* 不改变线上默认检索路径；本模块只消费已落盘报告的审计 JSON 与问答结构；
* fail-closed：来源冲突、资料缺失、版本漂移、无法确认一律不判为 ``supported``；
  "有引用" 不等于 "已被证据支持"；
* 现有报告 JSON 尚未接入自动 claim 抽取：审计 JSON 中没有机读 ``claims`` 时，
  门禁返回 ``needs_review`` 并显式说明，不伪造已完成全链路绑定；
* ``requested_by`` 不是认证或授权。
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
import math
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import DATA_DIR, KNOWLEDGE_DIR

# --- 状态常量 -------------------------------------------------------------------

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
CONFLICT = "conflict"
NEEDS_REVIEW = "needs_review"

SUPPORT_STATUSES = frozenset({SUPPORTED, PARTIAL, UNSUPPORTED, CONFLICT, NEEDS_REVIEW})

RELEASE_RELEASED = "released"
RELEASE_BLOCKED = "blocked"
RELEASE_NEEDS_REVIEW = "needs_review"
RELEASE_PARTIAL = "partial"

#: 未能通过校验、不得按正常交付放行的门禁状态。
GATE_BLOCKING_RELEASES = frozenset({RELEASE_BLOCKED, RELEASE_NEEDS_REVIEW})

ANCHOR_SOURCE = "source"
ANCHOR_SNAPSHOT = "snapshot"
ANCHOR_KINDS = frozenset({ANCHOR_SOURCE, ANCHOR_SNAPSHOT})

NO_CLAIMS_REASON = "报告审计 JSON 未包含可机读 claims（尚未接入自动 claim 抽取），关键结论无法判为已验证。"

#: 官方资料 canonical manifest；SHA 校验优先使用它，而不是审计 JSON 里的任意路径。
MANIFEST_PATH = DATA_DIR / "materials_manifest.json"

#: 允许读取并做 SHA 校验的资料目录；目录外路径一律不可信。
ALLOWED_MATERIAL_DIRS: tuple[Path, ...] = (KNOWLEDGE_DIR, DATA_DIR)

# --- 校验错误码与影响级别 ----------------------------------------------------------


class EvidenceErrorCode:
    # claim 级：直接 unsupported
    CLAIM_TICKER_MISMATCH = "claim_ticker_mismatch"
    REPORT_ID_MISMATCH = "report_id_mismatch"
    ANCHOR_MISSING = "anchor_missing"
    CLAIM_MALFORMED = "claim_malformed"
    CLAIM_ID_MISSING = "claim_id_missing"
    CLAIM_TEXT_MISSING = "claim_text_missing"
    ANCHORS_MALFORMED = "anchors_malformed"
    # anchor 级：资料缺失 / 锚点不一致 → unsupported
    SOURCE_IDENTITY_INCOMPLETE = "source_identity_incomplete"
    SOURCE_ID_NOT_RECORDED = "source_id_not_recorded"
    SOURCE_NOT_FOUND = "source_not_found"
    FIELD_NOT_FOUND = "field_not_found"
    PAGE_MISMATCH = "page_mismatch"
    EXCERPT_NOT_FOUND = "excerpt_not_found"
    VALUE_MISMATCH = "value_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    # anchor 级：来源冲突 / 版本漂移 → conflict
    SOURCE_ID_VERSION_CONFLICT = "source_id_version_conflict"
    SOURCE_SHA_MISMATCH = "source_sha_mismatch"
    # anchor 级：无法确认 → needs_review
    EXCERPT_UNVERIFIABLE = "excerpt_unverifiable"
    PERIOD_UNVERIFIABLE = "period_unverifiable"
    UNIT_UNVERIFIABLE = "unit_unverifiable"
    SOURCE_FILE_UNREADABLE = "source_file_unreadable"
    SOURCE_PATH_UNTRUSTED = "source_path_untrusted"
    SOURCE_MANIFEST_DRIFT = "source_manifest_drift"
    SNAPSHOT_FIELD_UNTRUSTED = "snapshot_field_untrusted"
    DATA_UNAVAILABLE = "data_unavailable"
    SOURCE_SHA_MISSING = "source_sha_missing"
    VALUE_UNVERIFIABLE = "value_unverifiable"
    DATA_UNAVAILABLE = "data_unavailable"


_ERROR_IMPACT: dict[str, str] = {
    EvidenceErrorCode.CLAIM_TICKER_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.REPORT_ID_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.ANCHOR_MISSING: UNSUPPORTED,
    EvidenceErrorCode.CLAIM_MALFORMED: NEEDS_REVIEW,
    EvidenceErrorCode.CLAIM_ID_MISSING: NEEDS_REVIEW,
    EvidenceErrorCode.CLAIM_TEXT_MISSING: NEEDS_REVIEW,
    EvidenceErrorCode.ANCHORS_MALFORMED: NEEDS_REVIEW,
    EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE: UNSUPPORTED,
    EvidenceErrorCode.SOURCE_ID_NOT_RECORDED: CONFLICT,
    EvidenceErrorCode.SOURCE_NOT_FOUND: UNSUPPORTED,
    EvidenceErrorCode.FIELD_NOT_FOUND: UNSUPPORTED,
    EvidenceErrorCode.PAGE_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.EXCERPT_NOT_FOUND: UNSUPPORTED,
    EvidenceErrorCode.VALUE_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.PERIOD_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.UNIT_MISMATCH: UNSUPPORTED,
    EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT: CONFLICT,
    EvidenceErrorCode.SOURCE_SHA_MISMATCH: CONFLICT,
    EvidenceErrorCode.EXCERPT_UNVERIFIABLE: NEEDS_REVIEW,
    EvidenceErrorCode.PERIOD_UNVERIFIABLE: NEEDS_REVIEW,
    EvidenceErrorCode.UNIT_UNVERIFIABLE: NEEDS_REVIEW,
    EvidenceErrorCode.SOURCE_FILE_UNREADABLE: NEEDS_REVIEW,
    EvidenceErrorCode.SOURCE_PATH_UNTRUSTED: NEEDS_REVIEW,
    EvidenceErrorCode.SOURCE_MANIFEST_DRIFT: NEEDS_REVIEW,
    EvidenceErrorCode.DATA_UNAVAILABLE: UNSUPPORTED,
    EvidenceErrorCode.SNAPSHOT_FIELD_UNTRUSTED: UNSUPPORTED,
    EvidenceErrorCode.SOURCE_SHA_MISSING: NEEDS_REVIEW,
    EvidenceErrorCode.VALUE_UNVERIFIABLE: NEEDS_REVIEW,
    EvidenceErrorCode.DATA_UNAVAILABLE: UNSUPPORTED,
    EvidenceErrorCode.SNAPSHOT_FIELD_UNTRUSTED: UNSUPPORTED,
}


def error_impact(code: str) -> str:
    return _ERROR_IMPACT.get(code, UNSUPPORTED)


# --- 数据模型 -------------------------------------------------------------------


def _norm_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class EvidenceAnchor:
    """一个证据锚点：资料身份 + 页码/字段路径 + 期间/单位/数值/片段。"""

    kind: str
    citation: str | None = None
    source_id: str | None = None
    file_name: str | None = None
    page: str | None = None
    field_path: str | None = None
    evidence_excerpt: str | None = None
    value: Any = None
    period: str | None = None
    unit: str | None = None
    source_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "citation": self.citation,
            "source_id": self.source_id,
            "file_name": self.file_name,
            "page": self.page,
            "field_path": self.field_path,
            "evidence_excerpt": self.evidence_excerpt,
            "value": self.value,
            "period": self.period,
            "unit": self.unit,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceAnchor:
        return cls(
            kind=str(raw.get("kind") or ""),
            citation=raw.get("citation"),
            source_id=raw.get("source_id"),
            file_name=raw.get("file_name"),
            page=None if raw.get("page") is None else str(raw.get("page")),
            field_path=raw.get("field_path"),
            evidence_excerpt=raw.get("evidence_excerpt"),
            value=raw.get("value"),
            period=None if raw.get("period") is None else str(raw.get("period")),
            unit=raw.get("unit"),
            source_sha256=raw.get("source_sha256"),
        )


@dataclass(frozen=True)
class Claim:
    """一条对外关键结论及其证据绑定。``support_status`` 在校验前默认 needs_review（fail-closed）。"""

    claim_id: str
    claim_text: str
    ticker: str
    report_id: str
    anchors: tuple[EvidenceAnchor, ...] = ()
    support_status: str = NEEDS_REVIEW
    validation_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "ticker": self.ticker,
            "report_id": self.report_id,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "support_status": self.support_status,
            "validation_errors": list(self.validation_errors),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Claim:
        raw_anchors = raw.get("anchors")
        anchors = tuple(
            EvidenceAnchor.from_dict(item)
            for item in raw_anchors
            if isinstance(item, dict)
        ) if isinstance(raw_anchors, list) else ()
        return cls(
            claim_id=str(raw.get("claim_id") or ""),
            claim_text=str(raw.get("claim_text") or ""),
            ticker=str(raw.get("ticker") or "").upper().strip(),
            report_id=str(raw.get("report_id") or ""),
            anchors=anchors,
            support_status=str(raw.get("support_status") or NEEDS_REVIEW),
            validation_errors=tuple(str(item) for item in raw.get("validation_errors") or []),
        )


# --- 报告上下文 ---------------------------------------------------------------


class ReportContextError(ValueError):
    pass


@dataclass(frozen=True)
class ReportContext:
    """门禁校验上下文：同一标的、同一报告版本的审计 JSON 与报告正文。

    ``material_index`` 是 ``file_name → {sha256, path}`` 的 canonical 资料索引
    （来自 ``materials_manifest.json``）；SHA 校验优先用它，不信任审计 JSON 的任意路径。
    """

    report_id: str
    ticker: str
    audit: dict[str, Any]
    material_index: dict[str, dict[str, str]] | None = None


def load_material_index(manifest_path: Path | None = None) -> dict[str, dict[str, str]]:
    """从 ``materials_manifest.json`` 构造 ``file_name → {sha256, path}`` 索引。

    manifest 缺失/损坏时返回空索引（fail-closed：随后走允许目录检查，目录外路径 needs_review）。
    """
    path = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    index: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return index
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        file_name = str(entry.get("file_name") or "").strip()
        sha256 = str((entry.get("validation") or {}).get("sha256") or "").strip()
        if not file_name or not sha256:
            continue
        explicit_path = str(entry.get("path") or "").strip()
        material_path = explicit_path or str(KNOWLEDGE_DIR / file_name)
        if Path(file_name).name != file_name or Path(material_path).name != file_name:
            continue
        if not _path_in_allowed_material_dirs(material_path):
            continue
        index[file_name] = {"sha256": sha256, "path": material_path}
    return index


def load_report_context(report_dir: Path, report_id: str, manifest_path: Path | None = None) -> ReportContext:
    """加载报告审计 JSON 构造门禁上下文；文件缺失或不可解析时显式报错，不静默降级。"""
    # 延迟导入复用 qa 的加载与校验，避免重复实现两套报告读取。
    from .qa import ReportEvidenceError, load_report_evidence

    try:
        evidence = load_report_evidence(Path(report_dir), report_id)
    except ReportEvidenceError as exc:
        raise ReportContextError("报告审计 JSON 无法读取。") from exc
    return ReportContext(
        report_id=evidence.report_id,
        ticker=evidence.ticker,
        audit=evidence.audit,
        material_index=load_material_index(manifest_path),
    )


def _lookup(data: Any, path: str) -> Any:
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


_CURRENCY_FIELDS = frozenset({"revenue", "net_income", "free_cash_flow", "latest_close"})


def _expected_unit(field_path: str, audit: dict[str, Any]) -> str | None:
    if field_path.endswith("_pct"):
        return "%"
    group, _, field = field_path.partition(".")
    if field in _CURRENCY_FIELDS and isinstance(audit.get(group), dict):
        return str(audit[group].get("currency") or "").strip() or None
    return None


def _source_entries(context: ReportContext) -> list[dict[str, Any]]:
    """当前报告审计记录中属于同一标的的来源条目；ticker 不一致的来源不能作为证据命中。"""
    entries = []
    raw_sources = context.audit.get("sources")
    for source in raw_sources if isinstance(raw_sources, list) else []:
        if not isinstance(source, dict):
            continue
        metadata = source.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if str(metadata.get("ticker") or "").strip().upper() != context.ticker:
            continue
        entries.append({"source": source, "metadata": metadata})
    return entries


def _sha256_of_file(path_text: str) -> str | None:
    try:
        path = Path(path_text)
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _path_in_allowed_material_dirs(path_text: str) -> bool:
    """只有位于允许的资料目录（KNOWLEDGE_DIR / DATA_DIR）内的路径才可用于 SHA 校验。"""
    try:
        resolved = Path(path_text).resolve()
    except (OSError, ValueError):
        return False
    return any(resolved.is_relative_to(Path(directory).resolve()) for directory in ALLOWED_MATERIAL_DIRS)


def _verify_source_version(expected_sha: str, metadata: dict[str, Any], context: ReportContext) -> list[tuple[str, str]]:
    """按 R5 复核要求核验资料版本：优先 manifest canonical SHA，不信任审计 JSON 里的任意路径。"""
    file_name = str(metadata.get("file_name") or "").strip()
    entry = (context.material_index or {}).get(file_name)
    if entry is not None:
        digest = _sha256_of_file(entry["path"])
        if digest is None:
            return [(EvidenceErrorCode.SOURCE_FILE_UNREADABLE, "manifest 记录的资料文件不可读，无法核验版本。")]
        if digest != entry["sha256"]:
            return [(EvidenceErrorCode.SOURCE_MANIFEST_DRIFT, "资料文件实际内容与 materials_manifest 记录的 SHA 不一致。")]
        if str(expected_sha) != entry["sha256"]:
            return [(EvidenceErrorCode.SOURCE_SHA_MISMATCH, "claim 声明的资料 SHA 与 materials_manifest 记录不一致，版本漂移。")]
        return []
    # 没有 manifest 记录：审计 JSON 的 metadata.path 不可信，只有位于允许资料目录内才可使用。
    path_text = str(metadata.get("path") or "").strip()
    if not path_text or not _path_in_allowed_material_dirs(path_text):
        return [
            (
                EvidenceErrorCode.SOURCE_PATH_UNTRUSTED,
                "资料不在 materials_manifest 中，且审计路径缺失或在允许的资料目录之外，不能据此核验 SHA。",
            )
        ]
    digest = _sha256_of_file(path_text)
    if digest is None:
        return [(EvidenceErrorCode.SOURCE_FILE_UNREADABLE, "资料文件不可读，无法核验版本 SHA。")]
    if digest != str(expected_sha):
        return [(EvidenceErrorCode.SOURCE_SHA_MISMATCH, "资料文件 SHA256 与 claim 声明不一致，版本漂移。")]
    return []


# --- Claim/Evidence 逐条校验（fail-closed） -----------------------------------------


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return _norm_ws(left) == _norm_ws(right)


def _source_contains_value(content: str, value: Any) -> bool:
    """按完整数值 token 匹配，避免把 10 当成 100 的证据。"""
    if isinstance(value, bool) or value is None:
        return _norm_ws(value) in _norm_ws(content)
    if isinstance(value, (int, float)):
        tokens = re.findall(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?%?", content)
        target = float(value)
        for token in tokens:
            try:
                if math.isclose(float(token.rstrip("% ").replace(",", "")), target, rel_tol=1e-9, abs_tol=1e-9):
                    return True
            except ValueError:
                continue
        return False
    return _norm_ws(value) in _norm_ws(content)


def _original_page_text(anchor: EvidenceAnchor, context: ReportContext, expected_sha: str) -> str | None:
    """从当前可信资料文件重读原始页；审计 JSON 的 content 不能自证为原文。"""
    material = (context.material_index or {}).get(str(anchor.file_name))
    if material is None:
        return None
    path = Path(material.get("path") or "")
    if path.name != str(anchor.file_name) or not path.is_file():
        return None
    try:
        original_bytes = path.read_bytes()
        digest = hashlib.sha256(original_bytes).hexdigest()
        if digest != expected_sha or digest != material.get("sha256"):
            return None
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            page_number = int(str(anchor.page))
            if page_number < 1:
                return None
            reader = PdfReader(BytesIO(original_bytes))
            if page_number > len(reader.pages):
                return None
            return reader.pages[page_number - 1].extract_text(extraction_mode="layout") or ""
        if path.suffix.lower() in {".txt", ".md"}:
            return original_bytes.decode("utf-8")
    except Exception:
        return None
    return None


def _validate_source_anchor(anchor: EvidenceAnchor, context: ReportContext) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    # 来源身份必须完整：source_id + file_name + page 三者齐全，不能只凭 citation 判定 supported。
    if not anchor.source_id or not anchor.file_name or anchor.page is None:
        if not anchor.citation and not anchor.source_id:
            return [(EvidenceErrorCode.ANCHOR_MISSING, "source 锚点必须提供 citation 或 source_id 之一。")]
        return [
            (
                EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE,
                "source 锚点必须同时提供 source_id、file_name、page 才能完整判定来源身份。",
            )
        ]

    entries = _source_entries(context)
    entry: dict[str, Any] | None = None
    if anchor.citation:
        citation = str(anchor.citation).strip().upper()
        matches = [item for item in entries if str(item["source"].get("citation") or "").strip().upper() == citation]
        if len(matches) > 1:
            return [(EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, f"引用 {citation} 在当前报告中不唯一，无法确定资料身份。")]
        entry = matches[0] if matches else None
        if entry is None:
            return [(EvidenceErrorCode.SOURCE_NOT_FOUND, f"引用 {citation} 在当前报告中不存在或标的不一致。")]
        recorded = str(entry["metadata"].get("source_id") or "")
        if recorded and recorded != str(anchor.source_id):
            return [(EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, f"引用 {citation} 的来源身份与报告记录不一致。")]
    else:
        matches = [item for item in entries if str(item["metadata"].get("source_id") or "") == str(anchor.source_id)]
        if len(matches) > 1:
            return [(EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, "source_id 在当前报告中不唯一，不能确定资料页。")]
        entry = matches[0] if matches else None
        if entry is None:
            # 不允许只用相同 page 号判定命中：先确认是否存在同文件同页但不同 source_id 的版本漂移。
            same_spot = [
                item
                for item in entries
                if str(item["metadata"].get("file_name") or "") == str(anchor.file_name)
                and str(item["metadata"].get("page") or "") == str(anchor.page)
            ]
            if same_spot:
                return [(EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, "存在同文件同页但来源身份（source_id）不同的资料，疑似版本漂移。")]
            return [(EvidenceErrorCode.SOURCE_NOT_FOUND, "来源身份（source_id）在当前报告审计记录中不存在。")]

    metadata = entry["metadata"]
    if str(metadata.get("file_name") or "") != str(anchor.file_name):
        errors.append((EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, "来源文件名与报告记录不一致，疑似来源冲突。"))
    if str(metadata.get("page") or "") != str(anchor.page):
        errors.append((EvidenceErrorCode.PAGE_MISMATCH, f"页码 {anchor.page} 与来源记录页码 {metadata.get('page')} 不一致。"))

    # 来源记录本身必须提供 source_id；不能让 citation 命中后再接受 claim 自填的任意身份。
    recorded_source_id = str(metadata.get("source_id") or "").strip()
    if not recorded_source_id:
        errors.append((EvidenceErrorCode.SOURCE_IDENTITY_INCOMPLETE, "当前报告来源记录缺少 source_id，无法绑定资料身份。"))
    elif recorded_source_id != str(anchor.source_id):
        errors.append((EvidenceErrorCode.SOURCE_ID_VERSION_CONFLICT, "source_id 与当前报告来源记录不一致。"))

    content = str(entry["source"].get("content") or "")
    if not anchor.evidence_excerpt or not str(anchor.evidence_excerpt).strip():
        # 缺少 evidence_excerpt：无法核验原文，fail-closed 进入 needs_review，不判 supported。
        errors.append((EvidenceErrorCode.EXCERPT_UNVERIFIABLE, "source 锚点未提供 evidence_excerpt，无法核验原文内容。"))
    elif not content.strip():
        errors.append((EvidenceErrorCode.EXCERPT_UNVERIFIABLE, "来源内容为空，无法确认证据片段。"))
    elif _norm_ws(anchor.evidence_excerpt) not in _norm_ws(content):
        errors.append((EvidenceErrorCode.EXCERPT_NOT_FOUND, "证据片段在来源内容中不存在。"))
    if anchor.value is not None and not _source_contains_value(str(anchor.evidence_excerpt or ""), anchor.value):
        errors.append((EvidenceErrorCode.VALUE_MISMATCH, f"数值锚点 {anchor.value} 未以完整数值出现在来源内容中。"))
    if anchor.period is not None:
        recorded_period = metadata.get("period") or metadata.get("period_end") or metadata.get("fiscal_period")
        if recorded_period is None:
            errors.append((EvidenceErrorCode.PERIOD_UNVERIFIABLE, "来源记录没有可验证的期间字段。"))
        elif not _values_equal(anchor.period, recorded_period):
            errors.append((EvidenceErrorCode.PERIOD_MISMATCH, f"期间 {anchor.period} 与来源记录 {recorded_period} 不一致。"))
    if anchor.unit is not None:
        recorded_unit = metadata.get("unit")
        if recorded_unit is None:
            errors.append((EvidenceErrorCode.UNIT_UNVERIFIABLE, "来源记录没有可验证的单位字段。"))
        elif _norm_ws(anchor.unit) != _norm_ws(recorded_unit):
            errors.append((EvidenceErrorCode.UNIT_MISMATCH, f"单位 {anchor.unit} 与来源记录 {recorded_unit} 不一致。"))

    report_sha = str(metadata.get("source_sha256") or "").strip()
    claim_sha = str(anchor.source_sha256 or "").strip()
    if not report_sha:
        errors.append((EvidenceErrorCode.SOURCE_MANIFEST_DRIFT, "报告来源记录缺少生成时的 SHA，不能用当前资料替代报告版本。"))
    if claim_sha and report_sha and claim_sha != report_sha:
        errors.append((EvidenceErrorCode.SOURCE_SHA_MISMATCH, "claim 的资料 SHA 与报告记录不一致。"))
    if report_sha:
        errors.extend(_verify_source_version(report_sha, metadata, context))
    if not any(code in {EvidenceErrorCode.SOURCE_SHA_MISMATCH, EvidenceErrorCode.SOURCE_MANIFEST_DRIFT, EvidenceErrorCode.SOURCE_FILE_UNREADABLE} for code, _ in errors):
        original = _original_page_text(anchor, context, report_sha)
        if original is None:
            errors.append((EvidenceErrorCode.EXCERPT_UNVERIFIABLE, "无法读取同文件同页原文，审计 JSON 不能作为原文的唯一证明。"))
        elif anchor.evidence_excerpt and _norm_ws(anchor.evidence_excerpt) not in _norm_ws(original):
            errors.append((EvidenceErrorCode.EXCERPT_NOT_FOUND, "证据片段不在资料文件对应的原始页面中。"))
        if original and anchor.value is not None and not _source_contains_value(original, anchor.value):
            errors.append((EvidenceErrorCode.VALUE_MISMATCH, "数值锚点不在资料文件对应的原始页面中。"))
        if original and anchor.period is not None and _norm_ws(anchor.period) not in _norm_ws(original):
            errors.append((EvidenceErrorCode.PERIOD_UNVERIFIABLE, "声明期间未在资料原文页找到相同锚点。"))
        if original and anchor.unit is not None and _norm_ws(anchor.unit) not in _norm_ws(original):
            errors.append((EvidenceErrorCode.UNIT_UNVERIFIABLE, "声明单位未在资料原文页找到相同锚点。"))
    return errors


def _validate_snapshot_anchor(anchor: EvidenceAnchor, context: ReportContext) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not anchor.field_path:
        return [(EvidenceErrorCode.ANCHOR_MISSING, "snapshot 锚点必须提供 field_path。")]
    group_name = str(anchor.field_path).split(".", 1)[0]
    if group_name not in {"financial_snapshot", "market_snapshot", "llm_result", "evaluation", "risk_flags"}:
        return [(EvidenceErrorCode.SNAPSHOT_FIELD_UNTRUSTED, "该字段不属于允许的报告审计快照，不能作为自身的证据。")]
    group = context.audit.get(group_name)
    if isinstance(group, dict) and group.get("data_available") is False:
        return [(EvidenceErrorCode.DATA_UNAVAILABLE, f"结构化数据组 {group_name} 标记为不可用，不能使用残留字段值。")]
    value_at = _lookup(context.audit, anchor.field_path)
    if value_at is None:
        return [(EvidenceErrorCode.FIELD_NOT_FOUND, f"结构化字段 {anchor.field_path} 在报告审计 JSON 中不存在或不可用。")]
    if anchor.value is None:
        errors.append((EvidenceErrorCode.EXCERPT_UNVERIFIABLE, f"snapshot 锚点 {anchor.field_path} 未提供 value，无法确认 claim 声明的具体数值。"))
    elif not _values_equal(anchor.value, value_at):
        errors.append((EvidenceErrorCode.VALUE_MISMATCH, f"字段 {anchor.field_path} 的值 {value_at} 与 claim 声明 {anchor.value} 不一致。"))
    if anchor.period is not None:
        period_at = _lookup(context.audit, f"{anchor.field_path}_period_end")
        if period_at is None:
            errors.append((EvidenceErrorCode.PERIOD_UNVERIFIABLE, f"报告审计 JSON 没有 {anchor.field_path}_period_end，无法核验期间。"))
        elif str(period_at) != str(anchor.period):
            errors.append((EvidenceErrorCode.PERIOD_MISMATCH, f"期间 {anchor.period} 与来源记录 {period_at} 不一致。"))
    if anchor.unit is not None:
        expected = _expected_unit(str(anchor.field_path), context.audit)
        if expected is None:
            # 字段没有登记的 canonical 单位：无法核验单位一致性，fail-closed 进入 needs_review。
            errors.append(
                (
                    EvidenceErrorCode.UNIT_UNVERIFIABLE,
                    f"字段 {anchor.field_path} 没有登记的 canonical 单位，无法核验单位 {anchor.unit}。",
                )
            )
        elif str(anchor.unit) != expected:
            errors.append((EvidenceErrorCode.UNIT_MISMATCH, f"单位 {anchor.unit} 与字段 {anchor.field_path} 的预期单位 {expected} 不一致。"))
    return errors


def validate_claim(claim: Claim, context: ReportContext) -> Claim:
    """对单条 Claim 做逐条 fail-closed 校验；返回带 support_status 与 validation_errors 的新 Claim。"""
    errors: list[tuple[str, str]] = []
    if claim.validation_errors:
        errors.append((EvidenceErrorCode.CLAIM_MALFORMED, "claim 解析不完整，不能据此交付。"))
    if not str(claim.claim_id or "").strip():
        errors.append((EvidenceErrorCode.CLAIM_MALFORMED, "claim_id 不能为空。"))
    if not str(claim.claim_text or "").strip():
        errors.append((EvidenceErrorCode.CLAIM_MALFORMED, "claim_text 不能为空。"))
    if str(claim.report_id or "").strip() != context.report_id:
        errors.append((EvidenceErrorCode.REPORT_ID_MISMATCH, f"claim 的报告版本 {claim.report_id or '(空)'} 与当前报告 {context.report_id} 不一致。"))
    if str(claim.ticker or "").strip().upper() != context.ticker:
        errors.append((EvidenceErrorCode.CLAIM_TICKER_MISMATCH, f"claim 的标的 {claim.ticker or '(空)'} 与报告标的 {context.ticker} 不一致。"))
    if not claim.anchors:
        errors.append((EvidenceErrorCode.ANCHOR_MISSING, "claim 没有任何证据锚点。"))

    claim_level = [error for error in errors if error[0] in (EvidenceErrorCode.REPORT_ID_MISMATCH, EvidenceErrorCode.CLAIM_TICKER_MISMATCH, EvidenceErrorCode.ANCHOR_MISSING, EvidenceErrorCode.CLAIM_MALFORMED)]
    all_errors = list(errors)
    anchor_statuses: list[str] = []
    if not claim_level:
        for anchor in claim.anchors:
            if anchor.kind == ANCHOR_SOURCE:
                anchor_errors = _validate_source_anchor(anchor, context)
            elif anchor.kind == ANCHOR_SNAPSHOT:
                anchor_errors = _validate_snapshot_anchor(anchor, context)
            else:
                anchor_errors = [(EvidenceErrorCode.ANCHOR_MISSING, f"未知的锚点类型 {anchor.kind or '(空)'}。")]
            all_errors.extend(anchor_errors)
            impacts = {error_impact(code) for code, _ in anchor_errors}
            if CONFLICT in impacts:
                anchor_statuses.append(CONFLICT)
            elif UNSUPPORTED in impacts:
                anchor_statuses.append(UNSUPPORTED)
            elif NEEDS_REVIEW in impacts:
                anchor_statuses.append(NEEDS_REVIEW)
            else:
                anchor_statuses.append(SUPPORTED)

    if claim_level:
        status = UNSUPPORTED
    elif any(item == CONFLICT for item in anchor_statuses):
        status = CONFLICT
    elif any(item == UNSUPPORTED for item in anchor_statuses):
        status = UNSUPPORTED
    elif any(item == NEEDS_REVIEW for item in anchor_statuses):
        status = NEEDS_REVIEW
    else:
        status = SUPPORTED
    return replace(
        claim,
        support_status=status,
        validation_errors=tuple(f"{code}: {message}" for code, message in all_errors),
    )


# --- Claim 构造入口 ---------------------------------------------------------------


def claims_from_audit(audit: dict[str, Any], report_id: str) -> list[Claim]:
    """从报告审计 JSON 的机读 ``claims`` 数组构造 Claim；缺失时返回空列表（由门禁显式说明）。"""
    claims: list[Claim] = []
    raw_claims = audit.get("claims")
    if raw_claims is not None and not isinstance(raw_claims, list):
        return [Claim(claim_id="claim-malformed", claim_text="", ticker=str(audit.get("ticker") or "").upper().strip(), report_id=report_id, validation_errors=(f"{EvidenceErrorCode.CLAIM_MALFORMED}: claims 必须是数组。",))]
    for index, raw in enumerate(raw_claims or []):
        if not isinstance(raw, dict):
            claims.append(
                Claim(
                    claim_id=f"claim-{index}",
                    claim_text="",
                    ticker=str(audit.get("ticker") or "").upper().strip(),
                    report_id=report_id,
                    support_status=NEEDS_REVIEW,
                    validation_errors=(f"{EvidenceErrorCode.CLAIM_MALFORMED}: claims[{index}] 不是对象。",),
                )
            )
            continue
        try:
            raw_anchors = raw.get("anchors")
            if raw_anchors is not None and not isinstance(raw_anchors, list):
                raise ValueError("anchors 必须是数组")
            if isinstance(raw_anchors, list) and any(not isinstance(item, dict) for item in raw_anchors):
                raise ValueError("anchors 包含非对象")
            claim = Claim.from_dict(
                {
                    **raw,
                    "report_id": str(raw.get("report_id") or report_id),
                    "ticker": str(raw.get("ticker") or audit.get("ticker") or ""),
                }
            )
        except (TypeError, ValueError):
            claim = Claim(
                claim_id=f"claim-{index}",
                claim_text="",
                ticker=str(audit.get("ticker") or "").upper().strip(),
                report_id=report_id,
                support_status=NEEDS_REVIEW,
                validation_errors=(f"{EvidenceErrorCode.CLAIM_MALFORMED}: claims[{index}] 无法解析。",),
            )
        if not claim.claim_id:
            claim = replace(claim, claim_id=f"claim-{index}")
        claims.append(claim)
    return claims


def claims_from_answer(answer: Any, context: ReportContext | None = None) -> list[Claim]:
    """把 ``qa.Answer`` 转成 Claim，并用当前报告上下文补齐来源锚点。

    只有 citation 能在当前报告、当前 ticker 的来源集合中找到时，才补齐
    ``source_id``、文件、页码和原文片段；找不到时保留已有字段，让下游门禁
    按不完整来源身份 fail-closed。这里不根据文件名、页码或 citation 猜测
    source_id，也不伪造 source_sha256。
    """
    claims: list[Claim] = []
    for index, item in enumerate(getattr(answer, "claims", []) or []):
        anchors = []
        for ref in item.evidence_refs:
            if ref.kind == "snapshot":
                anchors.append(EvidenceAnchor(kind=ANCHOR_SNAPSHOT, field_path=ref.ref, value=ref.value))
            elif ref.kind == "source":
                source_id = None
                file_name = ref.file_name or None
                page = ref.page
                evidence_excerpt = None
                source_sha256 = None
                if context is not None:
                    citation = str(ref.ref or "").strip().upper()
                    matches = [
                        entry
                        for entry in _source_entries(context)
                        if citation and str(entry["source"].get("citation") or "").strip().upper() == citation
                    ]
                    # 重复 citation 无法唯一定位，不任意挑一个来源放行。
                    entry = matches[0] if len(matches) == 1 else None
                    if entry is not None:
                        metadata = entry["metadata"]
                        source_id = str(metadata.get("source_id") or "").strip() or None
                        # 已有文件/页码声明必须保留，不能把不一致的回答修饰成匹配。
                        file_name = file_name or str(metadata.get("file_name") or "").strip() or None
                        page_value = metadata.get("page")
                        if page is None and page_value is not None:
                            page = str(page_value)
                        content = str(entry["source"].get("content") or "").strip()
                        # QA 已提供片段时优先核验该片段；仅缺失时从审计原文补齐。
                        ref_content = ref.value.get("content") if isinstance(ref.value, dict) else None
                        evidence_excerpt = str(ref_content) if ref_content is not None else content or None
                        # 只沿用报告生成时记录的版本，不拿当前 manifest 反填历史快照。
                        source_sha256 = str(metadata.get("source_sha256") or "").strip() or None
                anchors.append(
                    EvidenceAnchor(
                        kind=ANCHOR_SOURCE,
                        citation=ref.ref,
                        source_id=source_id,
                        file_name=file_name,
                        page=page,
                        evidence_excerpt=evidence_excerpt,
                        source_sha256=source_sha256,
                    )
                )
        claims.append(
            Claim(
                claim_id=f"{answer.report_id}:answer-claim-{index}",
                claim_text=item.text,
                ticker=str(getattr(answer, "ticker", "") or "").upper().strip(),
                report_id=str(getattr(answer, "report_id", "") or ""),
                anchors=tuple(anchors),
            )
        )
    return claims


def build_claim_fixture() -> tuple[Claim, Claim]:
    """确定性离线 claim fixture（不联网、不依赖真实报告生成）。

    第一条为结构化字段 claim（supported 形态）；第二条为故意指向不存在来源的
    claim（unsupported 形态），用于演示门禁会阻断而非放行。
    """
    supported = Claim(
        claim_id="fixture-supported-revenue",
        claim_text="AAPL 最近财年营收为 416161000000 USD。",
        ticker="AAPL",
        report_id="AAPL_fixture",
        anchors=(
            EvidenceAnchor(
                kind=ANCHOR_SNAPSHOT,
                field_path="financial_snapshot.revenue",
                value=416161000000.0,
                period="2025-09-30",
                unit="USD",
            ),
        ),
    )
    unsupported = Claim(
        claim_id="fixture-unsupported-missing-source",
        claim_text="该结论依赖一个当前报告中不存在的来源。",
        ticker="AAPL",
        report_id="AAPL_fixture",
        anchors=(
            EvidenceAnchor(kind=ANCHOR_SOURCE, source_id="pdf-does-not-exist", file_name="missing.pdf", page="1"),
        ),
    )
    return supported, unsupported


# --- 交付门禁 -------------------------------------------------------------------


def evaluate_delivery(
    claims: list[Claim],
    context: ReportContext,
    extra_degradation_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """对报告的全部 claims 做门禁评估，输出稳定 release_status 与 reasons。

    * 任一 claim ``unsupported``/``conflict`` → ``blocked``；
    * 任一 claim ``needs_review``（且无阻断）→ ``needs_review``；
    * 任一 claim ``partial``（且无阻断/待审）→ ``partial``，显式带缺口原因；
    * 没有 claims → ``needs_review``，明确"尚未接入自动 claim 抽取"；
    * 其余 → ``released``。
    """
    results = [validate_claim(claim, context) for claim in claims]
    blocking_reasons: list[str] = []
    degradation_reasons: list[str] = list(extra_degradation_reasons or [])
    counts = {status: 0 for status in SUPPORT_STATUSES}
    for result in results:
        counts[result.support_status] += 1
        if not result.validation_errors:
            continue
        reason = f"{result.claim_id} {result.support_status}：{'；'.join(result.validation_errors)}"
        if result.support_status in (UNSUPPORTED, CONFLICT, NEEDS_REVIEW):
            blocking_reasons.append(reason)
        elif result.support_status == PARTIAL:
            degradation_reasons.append(reason)
    if any(result.support_status in (UNSUPPORTED, CONFLICT) for result in results):
        release_status = RELEASE_BLOCKED
    elif any(result.support_status == NEEDS_REVIEW for result in results):
        release_status = RELEASE_NEEDS_REVIEW
    elif any(result.support_status == PARTIAL for result in results):
        release_status = RELEASE_PARTIAL
    elif not results:
        release_status = RELEASE_NEEDS_REVIEW
        blocking_reasons.append(NO_CLAIMS_REASON)
    elif degradation_reasons:
        release_status = RELEASE_PARTIAL
    else:
        release_status = RELEASE_RELEASED
    return {
        "release_status": release_status,
        "claim_results": [result.to_dict() for result in results],
        "blocking_reasons": blocking_reasons,
        "degradation_reasons": degradation_reasons,
        "report_version": context.report_id,
        "ticker": context.ticker,
        "claims_total": len(results),
        "claim_status_counts": {status: count for status, count in counts.items() if count},
    }


def render_delivery(delivery: dict[str, Any]) -> str:
    """把门禁结果渲染成确定性 Markdown，便于报告尾部或任务记录直接引用。"""
    lines = [
        f"# 交付门禁结果（{delivery.get('report_version', '')}）",
        "",
        f"- 标的：{delivery.get('ticker', '')}",
        f"- 交付状态：**{delivery.get('release_status', '')}**",
        f"- claims 总数：{delivery.get('claims_total', 0)}",
    ]
    counts = delivery.get("claim_status_counts") or {}
    if counts:
        lines.append("- 状态分布：" + "，".join(f"{status} {count}" for status, count in sorted(counts.items())))
    for title, key in (("阻断原因", "blocking_reasons"), ("降级/缺口", "degradation_reasons")):
        reasons = delivery.get(key) or []
        if reasons:
            lines.append("")
            lines.append(f"## {title}")
            lines.extend(f"- {reason}" for reason in reasons)
    claim_results = delivery.get("claim_results") or []
    if claim_results:
        lines.append("")
        lines.append("## Claim 校验明细")
        for item in claim_results:
            lines.append(f"- [{item.get('support_status')}] {item.get('claim_id')}: {item.get('claim_text')}")
            for error in item.get("validation_errors") or []:
                lines.append(f"  - {error}")
    return "\n".join(lines) + "\n"
