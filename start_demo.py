"""启动本机演示服务；启动前验证身份配置和端口，避免误连旧进程。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from investment_assistant.access_control import TokenAuthService, Unauthorized, _parse_token_config

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
API_URL = "http://127.0.0.1:8000"
WEB_URL = "http://127.0.0.1:8501"
MCP_AK_URL = "http://127.0.0.1:28888/mcp"
MCP_AK_PORT = 28888
MCP_AK_PYTHON = Path.home() / ".codex" / "mcp-runtimes" / "investment-assistant-akshare" / "Scripts" / "python.exe"
MCP_BAOSTOCK_PYTHON = Path.home() / ".codex" / "mcp-runtimes" / "investment-assistant-ashare" / "Scripts" / "python.exe"
MCP_AK_CONFIG = ROOT / "config" / "mcp_akshare_config.py"


def _mcp_environment() -> dict[str, str]:
    if not MCP_AK_PYTHON.is_file() or not MCP_BAOSTOCK_PYTHON.is_file() or not MCP_AK_CONFIG.is_file():
        raise RuntimeError("两个数据源 MCP 尚未完成安装或配置；请检查 .codex/mcp-runtimes。")
    env = os.environ.copy()
    env.update({
        "IA_MCP_ENABLED": "1",
        "IA_AKSHARE_MCP_PYTHON": str(MCP_AK_PYTHON),
        "IA_AKSHARE_MCP_URL": MCP_AK_URL,
        "IA_BAOSTOCK_MCP_PYTHON": str(MCP_BAOSTOCK_PYTHON),
    })
    return env


def _preflight(ports: tuple[int, ...] = (8000, 8501)) -> str:
    if not PYTHON.is_file():
        raise RuntimeError("找不到项目 .venv；请先按 README 安装依赖并选择项目解释器。")
    tokens = _parse_token_config(os.environ.get("IA_AUTH_TOKENS"))
    if tokens is None:
        raise RuntimeError("IA_AUTH_TOKENS 未配置或格式无效；请在 PyCharm 运行配置的环境变量中填写身份映射 JSON。")
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(
                    f"127.0.0.1:{port} 已被占用；请先停止旧服务，再从 PyCharm 重新运行。"
                ) from exc
    # 凭证仅用于就绪检查，不写入日志、URL 或磁盘。
    token = next(iter(tokens))
    try:
        TokenAuthService(lambda: os.environ.get("IA_AUTH_TOKENS")).authenticate(token)
    except Unauthorized as exc:
        raise RuntimeError("IA_AUTH_TOKENS 中的首个凭证不可用或已过期。") from exc
    return token


def _get(url: str, token: str | None = None) -> bool:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _wait_ready(processes: list[subprocess.Popen[bytes]], token: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"服务进程 {process.pid} 提前退出（exit={process.returncode}）；请查看 PyCharm 控制台错误。")
        if (_get(f"{API_URL}/api/health") and _get(f"{API_URL}/api/me", token)
                and _get(WEB_URL)):
            return
        time.sleep(0.25)
    raise RuntimeError("启动超时：API 身份检查或 Streamlit 未就绪；请查看 PyCharm 控制台错误。")


def main() -> int:
    processes: list[subprocess.Popen[bytes]] = []
    try:
        env = _mcp_environment()
        token = _preflight((8000, 8501, MCP_AK_PORT))
        processes.append(subprocess.Popen(
            [str(MCP_AK_PYTHON), "-m", "akshare_mcp", "--format", "json", "--transport", "streamable-http",
             "--host", "127.0.0.1", "--port", str(MCP_AK_PORT), "--config", str(MCP_AK_CONFIG)],
            cwd=ROOT, env=env,
        ))
        processes.append(subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "investment_assistant.api:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=ROOT, env=env,
        ))
        processes.append(subprocess.Popen(
            [str(PYTHON), "-m", "streamlit", "run", "investment_assistant/web_app.py",
             "--server.address", "127.0.0.1", "--server.port", "8501", "--browser.gatherUsageStats", "false"],
            cwd=ROOT, env=env,
        ))
        _wait_ready(processes, token)
        print(f"服务已就绪：{WEB_URL}；在网页输入 IA_AUTH_TOKENS 中配置的 token。", flush=True)
        webbrowser.open(WEB_URL)
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"服务进程 {process.pid} 已退出（exit={process.returncode}）。")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("正在停止演示服务。", flush=True)
        return 0
    except RuntimeError as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
