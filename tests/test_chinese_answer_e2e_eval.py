"""中文问答闭环的数值抽取、拒答与引用边界回归。"""
from investment_assistant.chinese_answer_e2e_eval import _extract_answer, _field, _should_refuse


def test_financial_investment_field_is_not_treated_as_investment_advice():
    question = "五粮液2025年末长期股权投资是多少？"
    field, aliases = _field(question)
    assert field == "长期股权投资"
    assert _should_refuse(question, field) is None
    value, _ = _extract_answer(
        [{"lines": ["长期股权投资 2,233,514,411.45 1.18% 2,081,612,703.43"], "metadata": {"page": 18}}],
        field,
        aliases,
        question,
    )
    assert value == "2,233,514,411.45"


def test_ratio_question_prefers_percent_value_on_same_line():
    question = "平安银行2025年末拨备覆盖率是多少？"
    field, aliases = _field(question)
    value, _ = _extract_answer(
        [{"lines": ["生成率同比下降0.17个百分点；拨备覆盖率220.88%，持续保持在良好水平。"], "metadata": {"page": 7}}],
        field,
        aliases,
        question,
    )
    assert value == "220.88%"


def test_predictive_or_investment_advice_question_is_refused():
    question = "平安银行现在是否适合买入？"
    assert _should_refuse(question, None) is not None
