"""Start the local API and Streamlit demo, then open the browser."""

from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def main() -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(PYTHON), "-m", "uvicorn", "investment_assistant.api:app", "--host", "127.0.0.1", "--port", "8000"], cwd=ROOT, creationflags=creationflags)
    subprocess.Popen([str(PYTHON), "-m", "streamlit", "run", "investment_assistant/web_app.py", "--server.address", "127.0.0.1", "--server.port", "8501", "--browser.gatherUsageStats", "false"], cwd=ROOT, creationflags=creationflags)
    time.sleep(3)
    webbrowser.open("http://127.0.0.1:8501")


if __name__ == "__main__":
    main()
