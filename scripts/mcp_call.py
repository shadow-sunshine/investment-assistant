"""调用已安装数据源 MCP 的最小跨版本客户端。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if hasattr(value, "dict"):
        return _jsonable(value.dict())
    return str(value)


def _result_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _jsonable(structured)
    content = getattr(result, "content", None) or []
    texts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(str(text))
        elif isinstance(item, Mapping) and item.get("text") is not None:
            texts.append(str(item["text"]))
    return "\n".join(texts) if texts else _jsonable(content)


async def _call(args: argparse.Namespace) -> dict[str, Any]:
    if args.transport == "http":
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(args.url) as streams:
            read_stream, write_stream = streams[:2]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(args.tool, arguments=json.loads(args.arguments_json))
                return {"ok": True, "result": _result_payload(result)}

    params = StdioServerParameters(command=args.command, args=args.server_args)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(args.tool, arguments=json.loads(args.arguments_json))
            return {"ok": True, "result": _result_payload(result)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "http"), required=True)
    parser.add_argument("--command")
    parser.add_argument("--server-arg", action="append", dest="server_args", default=[])
    parser.add_argument("--url")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments-json", required=True)
    args = parser.parse_args()
    try:
        payload = asyncio.run(_call(args))
    except Exception as exc:  # noqa: BLE001 - 跨进程边界必须序列化失败
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
