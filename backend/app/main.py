"""모아플랜 백엔드 (FastAPI)

backend 폴더에서 실행:
    uvicorn app.main:app --reload --port 8000
화면은 http://localhost:8000 , API 문서는 /docs
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db  # noqa: E402
from .api import chat, logs, plans, system  # noqa: E402


def _start_logging_agent():
    from agent.llm import make_llm
    from events import anomaly
    from events.bus import BUS
    from events.logging_agent import LoggingAgent
    diag_llm = make_llm()
    return LoggingAgent(BUS, on_run_end=lambda s, r: anomaly.run_and_store(s, r, diag_llm)).start()


def _ensure_index():
    from tools.rag_tool import build_index, rag_search
    if os.getenv("MOAPLAN_AUTO_INDEX", "1") == "1" and not rag_search("예금자보호")["ok"]:
        try:
            build_index()
        except Exception:
            pass  # 색인 실패하면 rag_search 가 NO_INDEX 반환


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    agent = _start_logging_agent() if os.getenv("MOAPLAN_LOGGING", "1") == "1" else None
    _ensure_index()
    yield
    if agent:
        agent.stop()


app = FastAPI(title="모아플랜 MoaPlan — 목표 저축 플래너 Agent API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):  # 오류 상세는 화면에 안 보여줌
    return JSONResponse(status_code=500, content={"detail": "서버에서 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."})


app.include_router(chat.router)
app.include_router(plans.router)
app.include_router(logs.router)
app.include_router(system.router)

FRONT = Path(__file__).resolve().parents[2] / "frontend"
if FRONT.exists() and os.getenv("MOAPLAN_SERVE_FRONT", "1") == "1":
    app.mount("/", StaticFiles(directory=FRONT, html=True), name="frontend")
