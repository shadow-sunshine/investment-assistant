from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge_base"
CHROMA_DIR = DATA_DIR / "chroma"
REPORT_DIR = DATA_DIR / "reports"

for directory in (KNOWLEDGE_DIR, CHROMA_DIR, REPORT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
