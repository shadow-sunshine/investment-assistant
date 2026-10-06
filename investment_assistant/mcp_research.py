"""面向聊天的外部结构化数据编排。

MCP/结构化行情不是官方年报证据，但可以用于最新行情、历史市场观察和第三方财务候选。
本模块只负责把工具返回转换成可读且带来源/时间/限制的结果，不把候选提升为核验事实。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_MARKET_QUERY = re.compile(r"最新|行情|股价|价格|收盘|涨跌|走势|历史价格|波动|成交量|交易日|snapshot|price|trend", re.I)


def is_market_query(text: str) -> bool:
    return bool(_MARKET_QUERY.search(str(text or "")))


def _first(row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _observation_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for observation in payload.get("observations") or []:
        if not isinstance(observation, Mapping):
            continue
        for row in observation.get("data") or []:
            if isinstance(row, Mapping):
                rows.append({"source": observation.get("source"),
                             "retrieved_at": observation.get("retrieved_at"),
                             "provenance": observation.get("provenance") or {},
                             "row": dict(row)})
    return rows


def answer_market_snapshot(payload: Mapping[str, Any], ticker: str, question: str) -> dict[str, Any]:
    """将行情 MCP 结果组织为第三方候选答案。"""
    rows = _observation_rows(payload)
    if not payload.get("data_available") or not rows:
        errors = payload.get("error_codes") or ["MCP_MARKET_DATA_UNAVAILABLE"]
        return {"status": "refused", "error_code": str(errors[0]),
                "answer": f"暂时没有取得 {ticker} 的外部行情数据；请检查 MCP 状态后重试。",
                "sources": [], "evidence_level": "third_party_market_data_unavailable",
                "market_data": payload}
    row = rows[0]["row"]
    close = _first(row, ("最新价", "最新收盘", "收盘", "close", "Close", "收盘价"))
    change = _first(row, ("涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg", "涨幅"))
    volume = _first(row, ("成交量", "volume", "Volume"))
    trade_date = _first(row, ("日期", "交易日期", "date", "trade_date", "REPORT_DATE"))
    source = str(rows[0].get("source") or "MCP")
    retrieved = str(rows[0].get("retrieved_at") or "未知")
    parts = []
    if close is not None: parts.append(f"最新价/收盘价：{close}")
    if change is not None: parts.append(f"涨跌幅：{change}")
    if volume is not None: parts.append(f"成交量：{volume}")
    if trade_date is not None: parts.append(f"数据日期：{trade_date}")
    if not parts:
        parts.append("已取得外部行情记录，但当前字段无法映射为可读摘要")
    answer = (f"{ticker} 外部行情候选：" + "；".join(parts) + "。\n\n"
              f"来源：{source}；抓取时间：{retrieved}。"
              "这是第三方结构化数据，不是官方年报核验结论；币种、复权口径和实时性请以来源字段为准。")
    return {"status": "candidate", "answer": answer, "verified": False,
            "evidence_level": "third_party_market_data", "sources": [{
                "citation": "MCP1", "label": source,
                "identity": {"ticker": ticker, "retrieved_at": retrieved,
                             "provenance": rows[0].get("provenance")},
                "excerpt": str(row)[:1200],
            }], "market_data": payload, "question": question}
