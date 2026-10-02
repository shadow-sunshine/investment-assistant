"""只校验冻结 PDF 金标；不导入项目模块、不建立索引、不运行检索器。"""

import ast
import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data/retrieval_holdout_v2.json"
MANIFEST_PATH = ROOT / "data/evaluations/retrieval_holdout_v2_manifest.json"
EXPECTED_SOURCES = {
    "AAPL": ("Apple_2025_Form_10-K.pdf", "3eb270b22acb7d8d8e9c32a43dc221dec3345f8e4bca02755fdbc1ee16c823de", "en", 80),
    "MSFT": ("MSFT_SEC_10-K_2026-06-30_official-html.pdf", "803350171b5cbf667f0915a7de46b17d2e2c6351d398bf36e22016671997d109", "en", 139),
    "600519.SS": ("600519_2025_annual_report.pdf", "474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288", "zh", 143),
    "0700.HK": ("0700HK_annual_report_2025.pdf", "2a7547168077c3d9994af673125e77612e8656bc0f17ad189371d7e4088f4e98", "en", 282),
}
QUADRANTS = ("zh-zh", "en-en", "zh-en", "en-zh")
CATEGORIES = {"balance_sheet", "profit_loss", "cash_flow", "segment_expense_notes"}
REQUIRED = {"id", "quadrant", "ticker", "question", "question_lang", "doc_lang", "target_pages", "keywords", "evidence_snippet", "forbidden_tickers"}


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized(text):
    # 与旧数据去重时统一大小写、全半角及标点，不能仅比较原字符串。
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _compact(text):
    # 原文展示仍保留原始换行；只对标签/数字包含校验消除排版空白。
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _number_token(value):
    return value.strip().translate(str.maketrans("", "", "()+- \t\r\n"))


def _number_pattern(value):
    # 必须是完整的数字，不能把 523 误认成 1,523 或 523.5。
    return re.compile(r"(?<![0-9,.])" + re.escape(_number_token(value)) + r"(?![0-9,.])")


def _is_numeric_keyword(keyword):
    return bool(re.fullmatch(r"[+\-()]?[0-9][0-9,.()\s]*", keyword))


DATA = _read_json(DATA_PATH)
CASES = DATA["cases"]


@pytest.fixture(scope="session")
def pages():
    # 只访问约定四份材料，不读取其他知识库文件或检索缓存。
    result = {}
    for ticker, (file_name, expected_sha, _, expected_count) in EXPECTED_SOURCES.items():
        path = ROOT / "data/knowledge_base" / file_name
        assert _sha(path) == expected_sha, "冻结材料已变化"
        reader = PdfReader(path)
        assert len(reader.pages) == expected_count
        result[ticker] = [page.extract_text() or "" for page in reader.pages]
    return result


def _page(pages, ticker, page):
    assert type(page) is int and 1 <= page <= len(pages[ticker]), "无效物理页码"
    return pages[ticker][page - 1]


def _assert_source_excerpt(pages, ticker, binding):
    excerpt = binding["evidence_snippet"]
    assert isinstance(excerpt, str) and excerpt.strip(), "空证据"
    assert excerpt in _page(pages, ticker, binding["page"]), "证据不是目标页的逐字原文"


def _validate_case(case, pages):
    assert REQUIRED <= set(case)
    ticker = case["ticker"]
    file_name, source_sha, doc_lang, _ = EXPECTED_SOURCES[ticker]
    assert (case["file_name"], case["source_sha"]) == (file_name, source_sha), "源身份错误"
    assert case["quadrant"] == case["question_lang"] + "-" + case["doc_lang"]
    assert case["doc_lang"] == doc_lang
    assert set(case["forbidden_tickers"]) == set(EXPECTED_SOURCES) - {ticker}
    assert case["entity_scope"] == "consolidated"
    targets = case["target_pages"]
    assert targets and targets == sorted(set(targets))
    assert case["primary_page"] in targets
    primary_text = _page(pages, ticker, case["primary_page"])
    assert case["evidence_snippet"] in primary_text, "主证据不是逐字原文"
    keywords = case["keywords"]
    assert any(_is_numeric_keyword(k) for k in keywords), "缺少真实数字关键词"
    assert any(not _is_numeric_keyword(k) for k in keywords), "缺少指标标签关键词"
    assert len(keywords) == len(set(keywords))
    for keyword in keywords:
        assert _compact(keyword) in _compact(primary_text), "关键词不在原文页"
    assert _number_pattern(case["answer_value"]).search(case["evidence_snippet"])
    assert _compact(case["answer_display"]) in _compact(case["evidence_snippet"]), "正负号/括号不符"
    evidence = case["page_evidence"]
    assert [item["page"] for item in evidence] == targets
    for binding in evidence:
        _assert_source_excerpt(pages, ticker, binding)
        assert _number_pattern(case["answer_value"]).search(binding["evidence_snippet"])
        assert binding["verification"].strip()
        for context in binding["context_pages"]:
            _assert_source_excerpt(pages, ticker, context)
    excluded = case["excluded_same_number_pages"]
    assert not set(targets) & {item["page"] for item in excluded}
    for binding in excluded:
        _assert_source_excerpt(pages, ticker, binding)
        assert _number_pattern(case["answer_value"]).search(binding["evidence_snippet"])
        assert binding["reason"].strip()
    # 精确数值全文扫描只保证候选页枚举完整；同一事实的语义归属由人工复核。
    observed = {i + 1 for i, text in enumerate(pages[ticker]) if _number_pattern(case["answer_value"]).search(text)}
    assert observed == set(targets) | {item["page"] for item in excluded}, "未解释的同值页"
    validation = case["period_unit_validation"]
    assert validation["mode"] == "manual_pdf_text_header_and_row"
    assert validation["automated_period_column_validation"] is False
    expected_year = "2026" if ticker == "MSFT" else "2025"
    assert validation["expected_report_year"] == expected_year
    assert expected_year in case["period"] and expected_year in case["question"]
    for key in ("period_evidence", "unit_evidence"):
        _assert_source_excerpt(pages, ticker, validation[key])
    assert expected_year in validation["period_evidence"]["evidence_snippet"]
    expected_units = {"AAPL": {"USD million", "USD billion"}, "MSFT": {"USD million"}, "600519.SS": {"CNY"}, "0700.HK": {"CNY million"}}
    assert case["unit"] in expected_units[ticker], "单位错误"
    unit_text = _compact(validation["unit_evidence"]["evidence_snippet"])
    marker = "billion" if case["unit"] == "USD billion" else "million" if "million" in case["unit"] else "单位:元"
    assert marker in unit_text, "单位证据不符"
    assert validation["column_interpretation"].strip()
    # 问句允许来自报告的年份/日期，不允许复制答案数字或提示物理目标页。
    assert _normalized(case["answer_value"]) not in _normalized(case["question"]), "答案泄漏"
    assert not re.search(r"第\s*\d+\s*页|\bpage\s*\d+|\bp\.\s*\d+", case["question"], re.IGNORECASE)


def _validate_novelty(cases, old_cases):
    old_questions = {_normalized(case["question"]) for case in old_cases}
    old_signatures = {(case["ticker"], page, _normalized(keyword)) for case in old_cases for page in case["target_pages"] for keyword in case["keywords"]}
    old_values = {(case["ticker"], _normalized(keyword)) for case in old_cases for keyword in case["keywords"] if _is_numeric_keyword(keyword)}
    questions, signatures, values, facts, ids = set(), set(), set(), set(), set()
    for case in cases:
        question = _normalized(case["question"])
        assert question not in old_questions and question not in questions, "重复问句"
        questions.add(question)
        assert case["id"] not in ids
        ids.add(case["id"])
        assert case["fact_key"] not in facts, "跨语言复用事实"
        facts.add(case["fact_key"])
        value = (case["ticker"], _normalized(case["answer_value"]))
        assert value not in old_values and value not in values, "同标的重复金标数值"
        values.add(value)
        current = {(case["ticker"], page, _normalized(keyword)) for page in case["target_pages"] for keyword in case["keywords"]}
        assert not current & old_signatures and not current & signatures, "重复 ticker/page/keyword"
        signatures.update(current)


@pytest.mark.parametrize("ticker", list(EXPECTED_SOURCES))
def test_frozen_material_identity(ticker):
    file_name, source_sha, language, count = EXPECTED_SOURCES[ticker]
    material = DATA["materials"][ticker]
    assert (material["file_name"], material["sha256"], material["source_sha"], material["doc_lang"], material["page_count"]) == (file_name, source_sha, source_sha, language, count)
    assert _sha(ROOT / "data/knowledge_base" / file_name) == source_sha


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_gold_evidence_exists(case, pages):
    _validate_case(case, pages)


def test_quadrants_and_sampling_coverage():
    assert len(CASES) == 64
    assert Counter(case["quadrant"] for case in CASES) == Counter({q: 16 for q in QUADRANTS})
    expected_tickers = {"zh-zh": {"600519.SS": 16}, "en-zh": {"600519.SS": 16}, "en-en": {"AAPL": 6, "MSFT": 5, "0700.HK": 5}, "zh-en": {"AAPL": 5, "MSFT": 6, "0700.HK": 5}}
    for quadrant in QUADRANTS:
        selected = [case for case in CASES if case["quadrant"] == quadrant]
        categories = Counter(case["coverage_category"] for case in selected)
        assert set(categories) == CATEGORIES and min(categories.values()) >= 3
        assert Counter(case["ticker"] for case in selected) == Counter(expected_tickers[quadrant])
        coverage = DATA["sampling"]["coverage"][quadrant]
        assert coverage["categories"] == dict(categories)
        assert coverage["tickers"] == expected_tickers[quadrant]
        assert len({(case["ticker"], case["primary_page"]) for case in selected}) >= 8


def test_new_facts_and_questions_do_not_reuse_old_sets():
    old_cases = []
    for name in DATA["sampling"]["legacy_exclusion_files"]:
        old_cases.extend(_read_json(ROOT / name)["cases"])
    assert len(old_cases) == DATA["sampling"]["legacy_question_count"]
    _validate_novelty(CASES, old_cases)


@pytest.mark.parametrize("mutation", ["snippet", "source", "value", "display", "unit", "period", "page", "keyword"])
def test_gold_validator_rejects_corrupted_in_memory_cases(mutation, pages):
    # 只变异内存副本，绝不为了验证而改写金标文件。
    case = copy.deepcopy(CASES[0])
    if mutation == "snippet":
        case["evidence_snippet"] += "并不存在的原文"
    elif mutation == "source":
        case["source_sha"] = "0" * 64
    elif mutation == "value":
        case["answer_value"] = "9,999,999,999.99"
    elif mutation == "display":
        case["answer_display"] = "-" + case["answer_display"]
    elif mutation == "unit":
        case["unit"] = "USD billion"
    elif mutation == "period":
        case["period"] = "2040-12-31"
    elif mutation == "page":
        case["primary_page"] = 1
    else:
        case["keywords"] = [case["answer_value"]]
    with pytest.raises(AssertionError):
        _validate_case(case, pages)


def test_novelty_validator_rejects_translated_fact_reuse():
    duplicate = copy.deepcopy(CASES[0])
    duplicate["id"] += "_copy"
    duplicate["question"] = "A different English question about the same fact in 2025?"
    duplicate["question_lang"] = "en"
    duplicate["quadrant"] = "en-zh"
    with pytest.raises(AssertionError):
        _validate_novelty(CASES + [duplicate], [])


def test_validation_module_has_no_project_or_retriever_imports():
    allowed = {"ast", "copy", "hashlib", "json", "re", "unicodedata", "collections", "pathlib", "pytest", "pypdf"}
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module.split(".")[0] in allowed


def test_dataset_and_authoring_inputs_are_sha_locked():
    manifest = _read_json(MANIFEST_PATH)
    assert manifest["locked"] is True
    assert manifest["dataset_sha256"] == _sha(DATA_PATH)
    assert manifest["test_sha256"] == _sha(Path(__file__))
    assert manifest["case_count"] == 64
    assert manifest["quadrant_counts"] == {quadrant: 16 for quadrant in QUADRANTS}
    assert manifest["accepted_page_bindings"] == sum(len(case["target_pages"]) for case in CASES)
    assert manifest["author_isolation"] == DATA["author_isolation"]
    assert DATA["author_isolation"]["retriever_runs"] == 0
    assert DATA["author_isolation"]["implementation_files_read"] == []
    assert DATA["author_isolation"]["retrieval_results_used"] is False
    for binding in manifest["legacy_exclusion_inputs"]:
        assert _sha(ROOT / binding["path"]) == binding["sha256"]
    assert set(DATA["materials"]) == set(EXPECTED_SOURCES)
    for ticker in EXPECTED_SOURCES:
        assert manifest["materials"][ticker] == DATA["materials"][ticker]
