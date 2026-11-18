"""RAG 색인 만들기: python -m scripts.build_index"""
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from tools.rag_tool import build_index  # noqa: E402

if __name__ == "__main__":
    print(f"색인한 문단 {build_index()}개")
