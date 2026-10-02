"""固定候选 Cross-Encoder 精排边界：只接受注入评分器，不加载模型或回退。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from numbers import Real
from typing import Any, Callable

MAX_BUDGET = 48
MAX_LIMIT = 4
Pairs = tuple[tuple[str, str], ...]
Scorer = Callable[[Pairs], Any]


def _canonical_json(value: Any) -> bytes:
    """严格 JSON 保留类型边界，禁止非字符串键和 NaN 等不稳定输入。"""
    def validate(item: Any) -> None:
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("invalid_json_key")
                validate(child)
        elif type(item) is list:
            for child in item:
                validate(child)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("invalid_json_type")

    try:
        validate(value)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid_candidate_json") from exc


def _candidate_list(candidates: Any) -> list[dict[str, Any]]:
    if type(candidates) not in (list, tuple):
        raise ValueError("invalid_candidates_shape")
    if any(type(source) is not dict for source in candidates):
        raise ValueError("invalid_source_shape")
    return list(candidates)


def candidate_fingerprint(candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """完整 source 的有序 canonical JSON SHA256，绑定正文、元数据及审计字段。"""
    return hashlib.sha256(_canonical_json(_candidate_list(candidates))).hexdigest()


def _nonblank(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _positive_int(value: Any, name: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError(f"invalid_{name}")
    return value


def _validate_material(material_spec: Any, ticker: str) -> tuple[str, str, int | None]:
    if type(material_spec) is not dict:
        raise ValueError("invalid_material_spec")
    _canonical_json(material_spec)
    file_name = material_spec.get("file_name")
    sha256 = material_spec.get("sha256")
    if not _nonblank(file_name):
        raise ValueError("invalid_material_file_name")
    if type(sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValueError("invalid_material_sha256")
    if "ticker" in material_spec and material_spec["ticker"] != ticker:
        raise ValueError("material_ticker_mismatch")
    if "source_sha" in material_spec and material_spec["source_sha"] != sha256:
        raise ValueError("material_sha256_mismatch")
    page_count = material_spec.get("page_count")
    if "page_count" in material_spec:
        _positive_int(page_count, "material_page_count")
    return file_name, sha256, page_count


def _validate_sources(
    sources: list[dict[str, Any]], ticker: str,
    file_name: str, sha256: str, page_count: int | None,
) -> None:
    seen_pages: set[int] = set()
    for source in sources:
        if not _nonblank(source.get("content")):
            raise ValueError("invalid_source_content")
        metadata = source.get("metadata")
        if type(metadata) is not dict:
            raise ValueError("invalid_source_metadata")
        if metadata.get("ticker") != ticker:
            raise ValueError("ticker_scope_violation")
        if metadata.get("file_name") != file_name:
            raise ValueError("file_scope_violation")
        if "source_sha256" in metadata and metadata["source_sha256"] != sha256:
            raise ValueError("material_version_violation")
        if not _nonblank(metadata.get("source_id")):
            raise ValueError("invalid_source_id")
        page = metadata.get("page")
        if type(page) is str and re.fullmatch(r"[1-9][0-9]*", page):
            try:
                page = int(page)
            except ValueError as exc:
                raise ValueError("invalid_source_page") from exc
        page = _positive_int(page, "source_page", page_count)
        # 同页换 source_id 仍是重复页，不能靠伪造块身份挤占精排预算。
        if page in seen_pages:
            raise ValueError("duplicate_candidate_page")
        seen_pages.add(page)


def _validate_scores(raw_scores: Any, count: int) -> list[float]:
    # 数组只按一维 shape/to-list 协议接收，不导入 NumPy/SDK，也不隐式展平。
    if type(raw_scores) not in (list, tuple):
        try:
            shape = getattr(raw_scores, "shape", None)
            if type(shape) is not tuple or len(shape) != 1 or type(shape[0]) is not int:
                raise ValueError("invalid_score_shape")
            if shape[0] != count:
                raise ValueError("score_count_mismatch")
            raw_scores = raw_scores.tolist()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("invalid_score_shape") from exc
        if type(raw_scores) not in (list, tuple):
            raise ValueError("invalid_score_shape")
    if len(raw_scores) != count:
        raise ValueError("score_count_mismatch")
    scores = []
    for score in raw_scores:
        if isinstance(score, bool) or not isinstance(score, Real):
            raise ValueError("invalid_score_value")
        try:
            number = float(score)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_score_value") from exc
        if not math.isfinite(number):
            raise ValueError("nonfinite_score")
        scores.append(number)
    return scores


def _assert_unchanged(
    candidates: Any, material_spec: Any, input_bytes: bytes, material_bytes: bytes,
) -> None:
    try:
        unchanged = (
            _canonical_json(_candidate_list(candidates)) == input_bytes
            and _canonical_json(material_spec) == material_bytes
        )
    except ValueError as exc:
        raise ValueError("scorer_modified_inputs") from exc
    if not unchanged:
        raise ValueError("scorer_modified_inputs")


def rank_candidates(
    query: str,
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    scorer: Scorer,
    ticker: str,
    material_spec: dict[str, Any],
    limit: int = MAX_LIMIT,
    budget: int = MAX_BUDGET,
) -> dict[str, Any]:
    """只在固定原序前缀中降序精排；同分保留原序，异常一律 ValueError。

    material_spec 为单标的资料声明，必含 file_name、sha256，可含 page_count。
    所有传入候选须是纯 JSON source，且归属同一文件、页级唯一；预算仅限制
    评分数量，不豁免尾部候选的校验。页码可为正整数或无前导零的十进制字符串。
    source_sha256 键可缺省，存在则必须匹配；真实资料 SHA 由外层冻结协议核验。
    本函数不会读取资料文件，也不声称缺省 SHA 的元数据本身携带版本证明。
    scorer 仅收到不可变的 ((原始问题, 原始正文), ...)；返回一维 list/tuple 或
    shape=(候选数,) 且支持 tolist() 的数组，
    每个值须是有限 Real（不接受 bool）。不会传入元数据、金标或扩展后的问题。
    sources 为脱离输入的完整 source 副本；scores 与返回 source 一一对应，
    candidate_scores 按原候选序排列，ranked_indices 给出预算内完整降序索引。
    指纹检测评分返回时的输入变化，不是对任意 Python 代码的执行沙箱。
    """
    if not _nonblank(query):
        raise ValueError("invalid_query")
    if not _nonblank(ticker):
        raise ValueError("invalid_ticker")
    _positive_int(limit, "limit", MAX_LIMIT)
    _positive_int(budget, "budget", MAX_BUDGET)
    if limit > budget:
        raise ValueError("limit_exceeds_budget")
    if not callable(scorer):
        raise ValueError("invalid_scorer")

    supplied = _candidate_list(candidates)
    input_bytes = _canonical_json(supplied)
    file_name, sha256, page_count = _validate_material(material_spec, ticker)
    material_bytes = _canonical_json(material_spec)
    _validate_sources(supplied, ticker, file_name, sha256, page_count)
    # 在调用外部评分器前脱离输入，不改写引用编号或原规则排序审计字段。
    selected = deepcopy(supplied[:budget])
    pairs = tuple((query, source["content"]) for source in selected)
    try:
        if pairs:
            try:
                raw_scores = scorer(pairs)
            except Exception as exc:
                raise ValueError("scorer_failed") from exc
            scores = _validate_scores(raw_scores, len(selected))
        else:
            scores = []
    finally:
        # 闭包可以绕过 pairs 修改原始池；即使评分抛错，也必须核对完整输入。
        _assert_unchanged(candidates, material_spec, input_bytes, material_bytes)

    ranked_indices = sorted(range(len(selected)), key=lambda index: (-scores[index], index))
    top_indices = ranked_indices[:limit]
    return {
        "sources": [selected[index] for index in top_indices],
        "scores": [scores[index] for index in top_indices],
        "candidate_scores": scores,
        "ranked_indices": ranked_indices,
        "candidate_fingerprint": candidate_fingerprint(selected),
        "input_fingerprint": hashlib.sha256(input_bytes).hexdigest(),
        "input_candidate_count": len(supplied),
        "candidate_count": len(selected),
        "returned_count": len(top_indices),
        "budget": budget,
        "limit": limit,
    }
