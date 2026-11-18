"""대화 API (/api/chat)"""
from __future__ import annotations

import json
import queue
import threading

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agent.service import new_session_id, run_chat
from events.bus import BUS
from ..schemas import ChatIn, ChatOut

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatOut, summary="메시지 한 건 처리 (응답 + 실행 이벤트)")
def chat(body: ChatIn):
    return run_chat(body.message, body.session_id, mode=body.mode, worker_llm=body.worker_llm)


@router.post("/stream", summary="처리 중 이벤트를 SSE 로 흘려보내고 마지막에 결과를 보낸다")
def chat_stream(body: ChatIn):
    sid = body.session_id or new_session_id()
    q: queue.Queue = queue.Queue()

    def on_event(msg):
        if msg["session_id"] == sid:
            q.put(("event", msg))

    unsubscribe = BUS.subscribe(on_event)

    def worker():
        try:
            res = run_chat(body.message, sid, mode=body.mode, worker_llm=body.worker_llm)
            q.put(("result", res))
        except Exception:
            q.put(("result", {"session_id": sid, "answer": "처리 중 문제가 생겼습니다.",
                              "view": {"type": "error"}, "events": [], "stats": {}}))
        finally:
            unsubscribe()

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            kind, data = q.get()
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            if kind == "result":
                break

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
