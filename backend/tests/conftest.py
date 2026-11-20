"""테스트는 전부 키, 네트워크 없이 fixture + 규칙 LLM 으로 돎"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MOAPLAN_FAKE", "1")
    monkeypatch.setenv("MOAPLAN_LLM", "fake")
    monkeypatch.setenv("MOAPLAN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MOAPLAN_CHROMA", str(tmp_path / "chroma"))
    for k in ("FINLIFE_API_KEY", "ECOS_API_KEY", "KAKAO_REST_API_KEY", "GMS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from app import db
    db.init_db()
    from agent import service
    service._SESSION_CACHE.clear()
    from tools import finlife, ecos
    finlife._CACHE.clear()
    ecos._CACHE.clear()
    yield


@pytest.fixture
def logging_agent():
    from agent.llm import FakeLLM
    from events import anomaly
    from events.bus import BUS
    from events.logging_agent import LoggingAgent
    diag = FakeLLM()
    agent = LoggingAgent(BUS, on_run_end=lambda s, r: anomaly.run_and_store(s, r, diag)).start()
    agent.diag_llm = diag
    yield agent
    agent.stop()


def events_of(res, name):
    return [e for e in res["events"] if e["event"] == name]
