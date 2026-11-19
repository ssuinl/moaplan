"""요청 한 번 처리: 이벤트 발행기 만들고 그래프 돌리고 대화 맥락 저장

mode
  single: 메인 Agent 하나가 다 함
  pw:     Planner / Worker / 로깅 Agent 로 나눔
"""
from __future__ import annotations

import time
import uuid

from events.anomaly import evaluate
from events.bus import BUS, Emitter
from . import memory as mem_mod
from .executor import RunContext
from .graph import build_graph
from .llm import make_llm


# 같은 대화에서 10분 동안 Tool 결과 재사용 (memory 랑은 별개)
_SESSION_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600


def new_session_id() -> str:
    return uuid.uuid4().hex[:10]


def run_chat(user: str, session_id: str | None = None, mode: str = "single", worker_llm: bool = True,
             bus=BUS, llm=None, faults: set | None = None) -> dict:
    session_id = session_id or new_session_id()
    run_id = uuid.uuid4().hex[:8]
    emit = Emitter(bus, session_id, run_id)
    llm = llm or make_llm()
    llm.emit = emit
    ctx = RunContext(emit=emit, llm=llm, mode=mode, worker_llm=worker_llm, faults=set(faults or ()))
    memory = mem_mod.load(session_id)
    hit = _SESSION_CACHE.get(session_id)
    if hit and time.time() - hit[0] < CACHE_TTL:
        ctx.cache = hit[1]

    t0 = time.perf_counter()
    emit("session.start", user=user, mode=mode, worker_llm=worker_llm, llm=llm.name)
    try:
        app = build_graph(ctx, anomaly_check=(lambda ev: evaluate(ev, final=False)) if mode == "pw" else None)
        out = app.invoke({"user": user, "session_id": session_id, "memory": memory},
                         {"recursion_limit": 60})
    except Exception as e:  # 무슨 일이 있어도 응답은 돌려준다
        emit("error", where="graph", type=type(e).__name__)
        out = {"answer": "처리 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요.",
               "view": {"type": "error", "error_code": "INTERNAL"}, "slots": memory.get("slots", {})}
    ms = round((time.perf_counter() - t0) * 1000)
    _SESSION_CACHE[session_id] = (time.time(), ctx.cache)

    view = out.get("view", {})
    # 대화 맥락 갱신 (Tool 원본은 안 넣음)
    memory["slots"] = out.get("slots", memory.get("slots", {}))
    if view.get("type") == "plan":
        v = out.get("verdict", {})
        memory["last_recommendations"] = [{
            "label": r["label"], "expected_total": r["expected_total"], "monthly_payment": r["monthly_payment"],
            "best_case_total": r.get("best_case_total"), "goal": v.get("goal"), "lump_sum": v.get("lump_sum"),
            "term": v.get("term"), "saving_product": r.get("saving_product"),
            "deposit_product": r.get("deposit_product")} for r in view.get("recommendations", [])]
    if view.get("saved_plan_id"):
        memory["last_plan_id"] = view["saved_plan_id"]
    mem_mod.add_turn(memory, user, out.get("answer", ""))
    mem_mod.save(session_id, memory)

    stats = {"ms": ms, "llm_calls": llm.calls, "tool_calls": ctx.tool_calls, "cache_hits": ctx.cache_hits,
             "replans": out.get("replan_count", 0)}
    emit("session.end", answer=(out.get("answer") or "")[:120], **stats)
    return {"session_id": session_id, "run_id": run_id, "mode": mode, "answer": out.get("answer", ""),
            "view": view, "events": emit.local, "stats": stats, "memory_slots": memory["slots"]}
