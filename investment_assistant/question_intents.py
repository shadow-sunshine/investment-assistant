"""问题任务契约：把资料目录、市场澄清和财务字段分开，不让旧槽位抢占新意图。

这是有边界的规则解析，不宣称通用语义理解；未知表达留给澄清，不猜金融事实。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LATEST = re.compile(r"最新|最近|当前|现在|截至目前|截至今天|最新披露|latest", re.I)
YEAR_REPLY = re.compile(r"^(?:20\d{2}年?|今年|去年|前年)(?:呢)?[？?。！!]*$")
SELECTION_ALL = re.compile(r"^(?:都要|全部|都看|全都要|全要|都想看|都想了解|这几个都要)[？?。！!]*$")
_MARKET = re.compile(r"港股|美股|a\s*股|沪深|香港市场", re.I)
_DOCUMENT = re.compile(r"官方资料|官方文件|公告|披露|年报|财报|报告|filings?", re.I)
_LIST = re.compile(r"有哪些|有哪|列表|清单|目录|资料范围|已接入|已收录|覆盖范围|查找|查阅|查看|查询|看看|了解|给我|list|show", re.I)
_HELP = re.compile(r"^(?:请问|请|我想知道|帮我)?(?:如何|怎么|怎样|在哪|哪里|为什么).*(?:查询|查看|查找|使用|接入|检索|保存|记忆|刷新|切换|支持)")

# 先匹配更具体字段，并标记占用范围，避免把“净利润”重复识别成“利润”。
_METRICS = (
    ("营业收入", ("营业总收入", "营业收入", "总收入", "营业额", "营收", "收入", "revenue")),
    ("归母净利润", ("归属于上市公司股东的净利润", "归母净利润")),
    ("净利润", ("净利润", "利润", "profit", "net income")),
    ("经营现金流", ("经营活动产生的现金流量净额", "经营活动现金流", "经营现金流", "现金流")),
    ("毛利率", ("毛利率",)), ("毛利", ("毛利",)),
    ("研发费用", ("研发费用", "研发开支")),
    ("资产", ("总资产", "资产")), ("负债", ("总负债", "负债")),
    ("利息收入", ("利息收入",)), ("投资收入", ("投资收入",)),
    ("净收入", ("净收入",)), ("服务收入", ("服务收入",)),
    ("现金及现金等价物", ("现金及现金等价物", "现金及现金等价物余额", "货币资金")),
)


def requested_metrics(text: str) -> list[str]:
    matches = []
    used = []
    aliases = [(name, alias) for name, words in _METRICS for alias in words]
    for name, alias in sorted(aliases, key=lambda pair: len(pair[1]), reverse=True):
        for match in re.finditer(re.escape(alias), text, re.I):
            if any(match.start() < end and match.end() > start for start, end in used):
                continue
            used.append((match.start(), match.end()))
            prefix = text[max(0, match.start() - 5):match.start()]
            if re.search(r"不要|不看|排除|不需要", prefix):
                continue
            matches.append((match.start(), name))
    return list(dict.fromkeys(name for _, name in sorted(matches)))


@dataclass(frozen=True)
class QuestionIntent:
    kind: str
    metrics: tuple[str, ...] = ()
    latest: bool = False
    market: str | None = None


def classify_question(text: str, *, has_explicit_company: bool = False) -> QuestionIntent:
    metrics = tuple(requested_metrics(text))
    market_match = _MARKET.search(text)
    market = None
    if market_match:
        word = market_match.group().lower()
        market = "HK" if "港" in word else "US" if "美" in word else "A"
    latest = bool(LATEST.search(text))
    if _HELP.search(text) and not metrics:
        return QuestionIntent("product_help", latest=latest, market=market)
    if metrics:
        return QuestionIntent("financial_question", metrics, latest, market)
    if (_DOCUMENT.search(text) and not re.search(r"摘要|总结|概览|生成|做一份|分析|研究", text)
            and (re.search(r"资料|公告|披露|清单|目录|文件|有哪些|有哪|列表|范围|覆盖", text) or latest)) :
        return QuestionIntent("document_inventory", latest=latest, market=market)
    if market and not has_explicit_company:
        return QuestionIntent("market_exploration", latest=latest, market=market)
    if YEAR_REPLY.fullmatch(text.strip()):
        return QuestionIntent("year_selection")
    if SELECTION_ALL.fullmatch(text.strip()):
        return QuestionIntent("metric_selection")
    return QuestionIntent("other", latest=latest, market=market)
