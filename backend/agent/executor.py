"""Tool 실행기. single 모드랑 pw 모드(Worker)가 같이 씀

- 계획(tasks) 순서대로 부르고, 앞 Tool 결과를 다음 Tool 입력으로 넘긴다 (expand)
- 실패해도 사유만 남기고 다음 작업으로 넘어감
- 같은 Tool + 같은 인자는 캐시 사용 (재계획할 때 이미 받은 결과 다시 씀)
- INVALID_KEY 는 다시 해도 똑같아서 치명적 실패로 보고 재계획 안 함

pw 모드 Worker
- 평소엔 LLM 없이 목록대로 부름
- 인자를 바꾸면 결과가 달라질 수 있는 실패(NO_RESULT, NO_DATA, NOT_FOUND, BAD_ARGUMENT)일 때만
  LLM 한테 재시도/건너뛰기/계획 변경 중에 고르게 함
- 재시도는 같은 Tool 로만, MAX_RETRY 안에서. Planner 가 고정한 인자는 못 바꿈
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from tools import REGISTRY
from tools.base import is_fixable

MAX_RETRY = 2
FATAL = {"INVALID_KEY"}
ASK_USER = {"MISSING_ARGUMENT", "AMBIGUOUS"}
NO_CACHE = {"get_schedule", "register_plan_schedule"}  # 호출할 때마다 상태가 바뀌는 로컬 Tool


@dataclass
class RunContext:
    emit: object
    llm: object
    mode: str = "single"  # single / pw
    worker_llm: bool = True  # pw 에서 Worker 가 LLM 쓸지 (비교 실험용)
    cache: dict = field(default_factory=dict)
    fatal: dict | None = None
    ask_user: dict | None = None
    worker_requests: list = field(default_factory=list)
    tool_calls: int = 0
    cache_hits: int = 0
    faults: set = field(default_factory=set)  # 테스트용 고장 주입 (dup_call, rogue_call)


def _key(tool: str, args: dict) -> str:
    return tool + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _summary(tool: str, out: dict) -> str:
    if not out["ok"]:
        return f"{out['error']['code']}: {out['error']['message']}"
    d = out["data"]
    if tool == "search_savings_products":
        return f"{d['count']}개 상품 ({d['term_months']}개월, {d['fin_group']})" + (" · 기준 변경" if d.get("is_estimated") else "")
    if tool == "get_product_detail":
        bank = re.sub(r"^(주식회사|\(주\))\s*|\s*(주식회사|\(주\))$", "", d["bank"] or "")  # 화면에 보일 은행 이름
        return f"{bank} {d['product']} 기본 {d['base_rate']}% / 최고 {d['max_rate']}%"
    if tool == "get_economic_indicators":
        return f"물가 {d['inflation_yoy']}% ({d['cpi_month']}), 기준금리 {d.get('base_rate')}%"
    if tool == "find_bank_branches":
        return f"영업점 {d['count']}곳"
    if tool == "rag_search":
        return f"문서 {len(d['hits'])}건"
    if tool == "get_schedule":
        return f"일정 {len(d['events'])}건, 월 납입 {d['monthly_committed']:,}원"
    if tool == "register_plan_schedule":
        return f"{len(d['events'])}개 일정 등록 ({d['calendar']})"
    return "ok"


def call_tool(ctx: RunContext, task_id: str, tool: str, args: dict, retry_of: str | None = None) -> dict:
    key = _key(tool, args)
    if key in ctx.cache:
        ctx.cache_hits += 1
        ctx.emit("tool.cache_hit", task=task_id, tool=tool, args=args)
        return ctx.cache[key]
    ctx.tool_calls += 1
    ctx.emit("tool.call", task=task_id, tool=tool, args=args, retry_of=retry_of,
             kind=(REGISTRY.get(tool).kind if tool in REGISTRY else "unknown"))
    t = time.perf_counter()
    out = REGISTRY.run(tool, **args)
    ms = round((time.perf_counter() - t) * 1000)
    ctx.emit("tool.result", task=task_id, tool=tool, ok=out["ok"], ms=ms,
             code=None if out["ok"] else out["error"]["code"], summary=_summary(tool, out))
    if tool not in NO_CACHE and (out["ok"] or out["error"]["code"] not in ("UPSTREAM_ERROR", "RATE_LIMITED")):
        ctx.cache[key] = out  # 일시적 오류나 상태가 바뀌는 로컬 Tool 은 캐시 안 함
    if "dup_call" in ctx.faults:  # 테스트용: 같은 입력으로 한 번 더 호출
        ctx.faults.discard("dup_call")
        ctx.emit("tool.call", task=task_id, tool=tool, args=args, retry_of=None, kind="fault")
        REGISTRY.run(tool, **args)
        ctx.emit("tool.result", task=task_id, tool=tool, ok=True, ms=0, code=None, summary="(중복 호출)")
    return out


def run_task(ctx: RunContext, task: dict, args: dict) -> dict:
    """작업 하나 실행"""
    tool = task["tool"]
    if "rogue_call" in ctx.faults:  # 테스트용: 계획에 없는 Tool 하나 끼워 넣기
        ctx.faults.discard("rogue_call")
        call_tool(ctx, "rogue", "find_bank_branches", {"bank_name": "가온은행", "region": "강남"})
    out = call_tool(ctx, task["id"], tool, args)
    if out["ok"]:
        return out
    code = out["error"]["code"]
    if code in FATAL:
        ctx.fatal = {"tool": tool, **out["error"]}
        return out
    if code in ASK_USER and task.get("required", True):
        ctx.ask_user = {"tool": tool, **out["error"]}
    if ctx.mode != "pw" or not ctx.worker_llm or not is_fixable(out):
        return out

    # Worker: 고칠 수 있는 실패일 때만 LLM 호출
    fixed = task.get("fixed", [])
    cur_args, last = dict(args), out
    for attempt in range(1, MAX_RETRY + 1):
        try:
            d = ctx.llm.worker_fix(tool, cur_args, fixed, last["error"])
        except Exception as e:  # LLM 이 실패해도 멈추지 않고 건너뜀
            d = {"action": "skip", "args": {}, "reason": f"Worker LLM 판단 실패({type(e).__name__}) — 건너뜀"}
        proposed = d.get("args") or {}
        blocked = {k: v for k, v in proposed.items() if k in fixed}  # 고정 인자는 여기서 막음
        d["args"] = {k: v for k, v in proposed.items() if k not in fixed and k in cur_args}
        action = d["action"]
        if action == "retry" and not d["args"]:
            action = "skip"  # 바꿀 수 있는 인자가 없으면 재시도 안 함
        ctx.emit("worker.decision", task=task["id"], tool=tool, attempt=attempt, action=action,
                 llm_action=d["action"], reason=d["reason"], changed_args=d["args"], blocked_args=blocked,
                 fixed_args=fixed, by="llm")
        d["action"] = action
        if d["action"] == "retry" and d["args"]:
            cur_args = {**cur_args, **d["args"]}
            last = call_tool(ctx, task["id"], tool, cur_args, retry_of=task["id"])
            if last["ok"]:
                return last
            if not is_fixable(last):
                return last
            continue
        if d["action"] == "replan":
            ctx.worker_requests.append({"task": task["id"], "tool": tool, "code": last["error"]["code"],
                                        "reason": d["reason"]})
        return last
    ctx.worker_requests.append({"task": task["id"], "tool": tool, "code": last["error"]["code"],
                                "reason": f"재시도 {MAX_RETRY}회 후에도 실패"})
    return last
