"""메인 Agent 흐름 (LangGraph)

START -> analyze -> plan -> execute -> judge -> (replan -> plan) -> enrich -> respond
작업이 없으면(인사 등) plan 에서 바로 respond 로 감

analyze: LLM 이 의도랑 조건 뽑기 (말 안 한 건 비워둠)
plan:    의도/조건 보고 코드가 Tool 순서 정함
execute: 계획대로 Tool 호출
judge:   만기 수령액, 필요 월 납입액 계산해서 조건 맞는지 판정
replan:  통과한 후보가 0개일 때만 전략 바꿔서 다시 (최대 3번)
enrich:  1순위 은행 영업점 찾기
respond: 숫자는 코드가 넣고 요약 문장만 LLM
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app import plans_store
from tools.finlife import STANDARD_TERMS, _normalize_term
from . import bonus as bonus_mod, calc, memory as mem_mod
from .executor import RunContext, run_task

REPLAN_CAP = 3
TOP_K = 4
DISCLAIMER = ("이 결과는 금융감독원 공시 금리로 계산한 예상치이며 실제 수령액은 은행 계산 방식·가입 시점에 따라 "
              "달라질 수 있습니다. 금융 전문가의 상담이나 특정 상품 가입 권유를 대신하지 않습니다.")
PROTECT_LIMIT = 100_000_000  # 예금자보호 한도 (금융회사별 1인당 원금+이자)


class State(TypedDict, total=False):
    user: str
    session_id: str
    memory: dict
    analysis: dict
    slots: dict
    changed: list
    missing: list
    intent: str
    tasks: list
    plan_version: int
    searched: dict
    detail_offset: int
    results: dict
    replan_count: int
    replans: list
    tried: list
    verdict: dict
    answer: str
    view: dict
    stop_reason: str
    search_task_ids: list
    verify: dict
    seen_findings: list
    all_tasks: list
    replan_trigger: str


def build_graph(ctx: RunContext, anomaly_check=None):
    emit = ctx.emit

    def traced(name, fn):
        def run(s):
            emit("step.enter", step=name)
            t = time.perf_counter()
            out = fn(s) or {}
            emit("step.exit", step=name, ms=round((time.perf_counter() - t) * 1000),
                 **({"output": out.get("_exit")} if out.get("_exit") else {}))
            out.pop("_exit", None)
            return out
        return run

    # 1. 요청 분석
    def analyze(s):
        memory = s.get("memory") or mem_mod.empty()
        a = ctx.llm.analyze(s["user"], memory)
        said = bonus_mod.from_text(s["user"])
        if said and not set(said) <= set(a.get("bonus_conds") or []):
            a["bonus_conds"] = sorted(set(a.get("bonus_conds") or []) | set(said))
            emit("decision", by="rule", kind="bonus_conds", value=a["bonus_conds"],
                 reason="말한 우대 조건을 LLM 이 다 못 뽑아서 규칙으로 보충")
            if a["intent"] in ("knowledge", "chitchat"):
                a["intent"] = "plan"
        if a.get("compare_months") and not a.get("months"):
            a["months"] = a["compare_months"][0]
        slots, changed = mem_mod.merge_slots(memory.get("slots", {}), a)
        intent = a["intent"]
        # 조건이 바뀌었는데 저장/등록/인사로 분류된 경우, 실제로 그런 말이 없으면 plan 으로 고침
        action_words = {"save_plan": r"(저장|확정|이걸로|이대로)", "calendar": r"(캘린더|달력|일정)"}
        if changed and intent in ("chitchat", "save_plan", "calendar") and \
                not re.search(action_words.get(intent, r"$^"), s["user"]):
            emit("decision", by="rule", kind="intent_correction", value="plan",
                 reason=f"LLM 은 {intent} 로 분류했지만 조건({', '.join(changed)})만 바꾸는 요청이라 계획을 다시 세운다")
            intent = "plan"
        missing = [k for k in ("goal_amount", "months") if not slots.get(k)] if intent == "plan" else []
        emit("decision", by="llm", kind="intent", value=intent, reason=a.get("reason"))
        return {"analysis": a, "slots": slots, "changed": changed, "intent": intent, "missing": missing,
                "replan_count": s.get("replan_count", 0), "replans": [], "tried": [], "results": {},
                "searched": {"saving": [], "deposit": []}, "detail_offset": 0, "plan_version": 0,
                "_exit": {"intent": intent, "reason": a.get("reason"), "conditions": mem_mod.grouped(slots),
                          "changed": changed, "missing": missing}}

    # 2. 작업 계획
    def plan(s):
        intent, slots, v = s["intent"], s["slots"], s.get("plan_version", 0) + 1
        tasks, why = [], ""
        searched = {k: list(x) for k, x in s.get("searched", {}).items()}
        if intent == "plan" and s["missing"]:
            why = "필수 조건이 비어 있어 Tool 을 부르기 전에 되묻는다"
        elif intent == "plan" and (s.get("tried") or [None])[-1] == "rebuild_verified":
            # 로깅 Agent 가 이상을 찾으면 검증된 작업만으로 다시 구성 (받은 결과는 캐시로 재사용)
            tasks = [t for t in s.get("all_tasks", []) if t["tool"] != "get_schedule"]
            why = "재계획: 로깅 Agent 검증에서 이상 감지 → 계획된 작업만으로 목록을 다시 구성(이미 받은 결과 재사용)"
        elif intent == "plan":
            term, _ = _normalize_term(slots["months"])
            strategy = (s.get("tried") or [None])[-1]
            groups = ["bank"] + (["savings_bank"] if slots.get("allow_savings_bank") is True else [])
            if strategy == "include_savings_bank":
                groups = ["savings_bank"]
            types = []
            need_monthly = slots["goal_amount"] > (slots.get("lump_sum") or 0)
            if slots.get("product_pref") != "deposit" and need_monthly:
                types.append("saving")
            if (slots.get("lump_sum") or 0) > 0 and slots.get("product_pref") != "saving":
                types.append("deposit")
            if strategy == "add_deposit":
                types = ["deposit"]
            if not types:
                types = ["deposit"] if slots.get("product_pref") == "deposit" else ["saving"]
            n = 0
            offset = s.get("detail_offset", 0)
            for pt in types:
                for g in groups:
                    if strategy == "more_candidates":
                        break
                    n += 1
                    tasks.append({"id": f"p{v}.t{n}", "tool": "search_savings_products",
                                  "args": {"product_type": pt, "term_months": term, "fin_group": g, "limit": 12},
                                  "fixed": ["product_type", "term_months", "fin_group"], "purpose": "후보 조회"})
                    searched.setdefault(pt, []).append(g)
            # 상세 조회 대상: 이번 검색 결과 + more_candidates 면 이전 검색의 다음 K개
            src = [t["id"] for t in tasks] if strategy != "more_candidates" else s.get("search_task_ids", [])
            for sid in src:
                n += 1
                tasks.append({"id": f"p{v}.t{n}", "tool": "get_product_detail", "expand": {"from": sid, "top": TOP_K,
                              "skip": offset if strategy == "more_candidates" else 0},
                              "args": {"term_months": term}, "fixed": ["term_months", "product_type"],
                              "purpose": "판단 속성 조회"})
            if v == 1:
                for ct in _compare_terms(s, term):  # 기간 비교: 다른 기간도 은행 적금 후보 + 상세
                    n += 1
                    sid = f"p{v}.t{n}"
                    tasks.append({"id": sid, "tool": "search_savings_products",
                                  "args": {"product_type": "saving", "term_months": ct, "fin_group": "bank", "limit": 12},
                                  "fixed": ["product_type", "term_months", "fin_group"], "purpose": f"{ct}개월 비교 후보"})
                    n += 1
                    tasks.append({"id": f"p{v}.t{n}", "tool": "get_product_detail", "expand": {"from": sid, "top": TOP_K, "skip": 0},
                                  "args": {"term_months": ct}, "fixed": ["term_months", "product_type"],
                                  "purpose": f"{ct}개월 비교 상세"})
                n += 1
                tasks.append({"id": f"p{v}.t{n}", "tool": "get_economic_indicators", "args": {},
                              "fixed": [], "purpose": "실질 수익률 판단", "required": False})
                n += 1
                tasks.append({"id": f"p{v}.t{n}", "tool": "get_schedule", "args": {}, "fixed": [],
                              "purpose": "이미 잡힌 월 납입액 확인", "required": False})
            why = {None: "조건으로 후보 조회 → 상세 → 물가 · 기존 일정 순서",
                   "include_savings_bank": "재계획: 저축은행 권역을 추가로 조회",
                   "add_deposit": "재계획: 목돈을 예금에 넣는 조합을 추가",
                   "more_candidates": "재계획: 이미 받은 후보 목록에서 다음 순위 상품의 상세를 조회"}.get(strategy, "")
        elif intent == "fact":
            tasks = [{"id": f"p{v}.t1", "tool": "get_economic_indicators", "args": {}, "fixed": [], "purpose": "물가 · 기준금리"},
                     {"id": f"p{v}.t2", "tool": "search_savings_products",
                      "args": {"product_type": "saving", "term_months": 12, "fin_group": "bank", "limit": 3},
                      "fixed": [], "purpose": "대표 적금 금리", "required": False}]
            why = "사실 조회: 물가 · 기준금리 + 12개월 적금 상위 금리"
        elif intent == "knowledge":
            tasks = [{"id": f"p{v}.t1", "tool": "rag_search", "args": {"query": s["user"]}, "fixed": ["query"],
                      "purpose": "제도 · 용어 안내 문서 검색"}]
            why = "외부 API 로 확인할 수 없는 제도 질문 → 안내 문서 검색"
        elif intent == "calendar":
            pid = s["analysis"].get("pick") or (s.get("memory") or {}).get("last_plan_id")
            if pid:
                tasks = [{"id": f"p{v}.t1", "tool": "register_plan_schedule", "args": {"plan_id": int(pid)},
                          "fixed": ["plan_id"], "purpose": "납입 · 만기 일정 등록"}]
                why = "저장한 계획 번호만 넘겨 일정 등록 (제목 · 날짜는 서버가 저장소에서 읽음)"
            else:
                why = "등록할 계획 번호가 없어 되묻는다"
        else:
            why = {"save_plan": "사용자가 고른 추천안을 저장 (모델 판단이 필요 없어 Tool 이 아님)",
                   "chitchat": "부를 Tool 이 없는 요청 — 계획 단계를 건너뜀"}.get(intent, "")
        emit("plan", version=v, intent=intent, reason=why,
             tasks=[{"id": t["id"], "tool": t["tool"], "args": t.get("args"), "expand": t.get("expand"),
                     "fixed": t.get("fixed", [])} for t in tasks])
        out = {"tasks": tasks, "plan_version": v, "searched": searched, "_exit": {"tasks": len(tasks), "reason": why},
               "all_tasks": s.get("all_tasks", []) + [t for t in tasks if t not in s.get("all_tasks", [])]}
        if intent == "plan" and tasks and (s.get("tried") or [None])[-1] not in ("more_candidates", "rebuild_verified"):
            out["search_task_ids"] = s.get("search_task_ids", []) + [t["id"] for t in tasks
                                                                     if t["tool"] == "search_savings_products"
                                                                     and "비교" not in t.get("purpose", "")]
        return out

    # 3. Tool 호출
    def execute(s):
        results = dict(s.get("results", {}))
        for task in s["tasks"]:
            if ctx.fatal:
                break
            if task.get("expand"):
                ex = task["expand"]
                src = results.get(ex["from"])
                if not src or not src.get("ok"):
                    results[task["id"]] = [{"ok": False, "error": {"code": "SKIPPED", "message": f"{ex['from']} 결과가 없어 건너뜀"}}]
                    continue
                d = src["data"]
                items = d["products"][ex["skip"]: ex["skip"] + ex["top"]]
                outs = []
                for it in items:
                    args = {"fin_prdt_cd": it["fin_prdt_cd"], "product_type": d["product_type"],
                            "term_months": d["term_months"], "fin_group": d["fin_group"]}
                    o = run_task(ctx, task, args)
                    o = {**o, "_meta": {"fin_group": d["fin_group"], "product": it["product"], "bank": it["bank"]}}
                    outs.append(o)
                results[task["id"]] = outs
            else:
                results[task["id"]] = run_task(ctx, task, dict(task.get("args", {})))
        ok_n = sum(1 for r in results.values() for x in (r if isinstance(r, list) else [r]) if x.get("ok"))
        return {"results": results, "_exit": {"ok_results": ok_n, "fatal": ctx.fatal, "cache_hits": ctx.cache_hits}}

    # 4. 판정
    def collect(results, tool_prefix_ok=True):
        savings, deposits, excluded = {}, {}, []
        econ, schedule = None, None
        for tid, r in results.items():
            for x in (r if isinstance(r, list) else [r]):
                if not x.get("ok"):
                    meta = x.get("_meta", {})
                    if meta:
                        excluded.append({"product": f"{_disp(meta['bank'])} {meta['product']}",
                                         "reason": x["error"]["message"], "code": x["error"]["code"]})
                    continue
                d = x["data"]
                if "fin_prdt_cd" in d and "base_rate" in d and "join_deny" in d:
                    (savings if d["product_type"] == "saving" else deposits)[d["fin_prdt_cd"]] = {
                        **d, "fin_group": x.get("_meta", {}).get("fin_group", "bank")}
                elif "inflation_yoy" in d:
                    econ = d
                elif "monthly_committed" in d:
                    schedule = d
        return savings, deposits, excluded, econ, schedule

    def judge(s):
        if s["intent"] != "plan" or ctx.fatal or ctx.ask_user:
            return {"verdict": {"kind": s["intent"]}, "_exit": {"skipped": True}}
        slots = s["slots"]
        goal, months = int(slots["goal_amount"]), int(slots["months"])
        term, changed_term = _normalize_term(months)
        budget, lump = slots.get("monthly_budget"), int(slots.get("lump_sum") or 0)
        savings_all, deposits, excluded, econ, schedule = collect(s["results"])
        savings = {k: d for k, d in savings_all.items() if d.get("term_months", term) == term}
        deposits = {k: d for k, d in deposits.items() if d.get("term_months", term) == term}
        existing = schedule["monthly_committed"] if schedule else 0
        infl = econ["inflation_yoy"] if econ else None
        conds = slots.get("bonus_conds") or []

        combos = []
        sav_list = list(savings.values()) or [None]
        dep_list = sorted(deposits.values(), key=lambda d: d["base_rate"], reverse=True)[:3] if lump > 0 else []
        dep_list = dep_list or [None]
        for sv0 in sav_list:
            sv, applied = sv0, []
            if sv0 and conds:  # 말한 우대 조건만 반영한 금리로 판정
                rate, applied = bonus_mod.apply(sv0, conds)
                sv = {**sv0, "base_rate": rate}
            for dp in dep_list:
                if sv is None and dp is None:
                    continue
                r = calc.judge_combo(goal, term, budget, lump, sv, dp, existing)
                r["bonus_applied"] = applied
                r["base_rate_raw"] = sv0["base_rate"] if sv0 else None
                label = " + ".join(x for x in [f"{_disp(sv['bank'])} {sv['product']}" if sv else None,
                                                f"{_disp(dp['bank'])} {dp['product']}(예금)" if dp else None] if x)
                warn = []
                for p, amt in ((sv, (r["saving"] or {}).get("maturity_amount", 0)),
                               (dp, (r["deposit"] or {}).get("maturity_amount", 0))):
                    if p and amt > PROTECT_LIMIT:
                        warn.append(f"{p['bank']} 예상 수령액 {amt:,}원이 예금자보호 한도 1억 원을 넘습니다")
                if sv and dp and sv["fin_co_no"] == dp["fin_co_no"] and r["expected_total"] > PROTECT_LIMIT:
                    warn.append(f"같은 금융회사({sv['bank']}) 합산이 예금자보호 한도 1억 원을 넘습니다")
                if sv and sv.get("reserve_type") == "F":
                    warn.append("자유적립식 상품이라 매달 같은 날 같은 금액을 넣는다고 가정하고 계산했어요")
                r.update(label=label, saving_product=_pinfo(sv0), deposit_product=_pinfo(dp), warnings=warn,
                         real_rate=calc.real_rate(sv["base_rate"] if sv else dp["base_rate"], infl))
                combos.append(r)
        # 필요 월 납입이 적은 순, 같으면 수령액 큰 순
        passed = sorted([c for c in combos if c["status"] == "pass"],
                        key=lambda c: (c["monthly_payment"], -c["expected_total"]))
        seen, recs = set(), []
        for c in passed:
            k = (c["saving_product"] or {}).get("fin_prdt_cd"), (c["deposit_product"] or {}).get("fin_prdt_cd")
            if k[0] in seen and k[0]:
                continue
            seen.add(k[0])
            recs.append(c)
            if len(recs) == 3:
                break
        conditional = [c for c in combos if c["status"] == "unjudged"][:2]
        failed = [c for c in combos if c["status"] == "fail"]
        for c in failed:
            excluded.append({"product": c["label"], "reason": "; ".join(c["reasons"]), "code": "CONSTRAINT"})
        adjust = _adjustments(goal, term, budget, lump, savings, deposits, existing, slots)
        comparison = _compare(goal, term, budget, existing, savings_all, conds, s) if _compare_terms(s, term) else []
        verdict = {"kind": "plan", "goal": goal, "months": months, "term": term, "term_changed": changed_term,
                   "budget": budget, "lump_sum": lump, "existing_monthly": existing,
                   "inflation": econ, "recommendations": recs, "conditional": conditional,
                   "excluded": _dedupe(excluded), "adjustments": adjust, "comparison": comparison, "bonus_conds": conds,
                   "counts": {"candidates": len(combos), "passed": len(passed), "failed": len(failed),
                              "unjudged": len(conditional)}}
        emit("judge", passed=len(passed), failed=len(failed), unjudged=len(conditional),
             best=(recs[0]["label"] if recs else None), best_total=(recs[0]["expected_total"] if recs else None),
             rule="세후 만기수령액 ≥ 목표 · 필요 월납입 ≤ 월예산(기존 납입 제외) · ≤ 상품 한도 · 가입제한 없음")
        out = {"verdict": verdict, "_exit": verdict["counts"]}
        if anomaly_check is not None and ctx.mode == "pw":  # pw 모드 검증은 로깅 Agent 가 함
            rep = anomaly_check(ctx.emit.local)
            seen = s.get("seen_findings", [])
            keys = [json.dumps(f, sort_keys=True, ensure_ascii=False) for f in rep["findings"]]
            new = [f for f, k in zip(rep["findings"], keys) if k not in seen]
            emit("logging.verify", verdict="anomaly" if new else "normal", findings=new,
                 previously_reported=len(rep["findings"]) - len(new))
            out["verify"] = {"verdict": "anomaly" if new else "normal", "findings": new}
            out["seen_findings"] = seen + [k for k in keys if k not in seen]
        return out

    def route_after_judge(s):
        if s["intent"] != "plan" or ctx.fatal or ctx.ask_user:
            return "respond"
        v = s["verdict"]
        if ctx.mode == "pw" and s.get("verify", {}).get("verdict") == "anomaly":
            kinds = {f["type"] for f in s["verify"]["findings"]}
            if kinds & {"repeat_call", "unplanned_call"} and s.get("replan_count", 0) < REPLAN_CAP:
                return "replan"
        no_room = v.get("budget") is not None and v["budget"] - v.get("existing_monthly", 0) <= 0
        if no_room and not v["recommendations"]:
            # 이미 잡힌 납입이 예산을 다 쓰면 상품을 더 찾아봤자 통과 못 함. 재계획 안 함
            emit("decision", by="rule", kind="skip_replan",
                 reason=f"이미 잡힌 월 납입 {v['existing_monthly']:,}원이 월 예산 {v['budget']:,}원을 다 써서 재계획해도 통과 불가")
        cap = 1 if _compare_terms(s, v.get("term")) else REPLAN_CAP  # 기간 비교 중이면 비교표가 답이라 1번만
        if not v["recommendations"] and not no_room and s.get("replan_count", 0) < cap and _next_strategy(s):
            return "replan"
        # Worker 가 계획 변경을 요청했으면 통과 후보가 있어도 다음 전략으로 한 번 더 찾아봄
        if ctx.mode == "pw" and ctx.worker_requests and not no_room \
                and s.get("replan_count", 0) < REPLAN_CAP and _next_strategy(s):
            return "replan"
        if s["slots"].get("region") and v["recommendations"]:
            return "enrich"
        return "respond"

    # 5. 재계획
    def replan(s):
        n = s.get("replan_count", 0) + 1
        by_verify = ctx.mode == "pw" and s.get("verify", {}).get("verdict") == "anomaly"
        strategy = "rebuild_verified" if by_verify else _next_strategy(s)
        if by_verify:
            reason = "로깅 Agent 검증에서 이상 감지: " + ", ".join(f["type"] for f in s["verify"]["findings"])
        elif s["verdict"]["recommendations"] and ctx.worker_requests:
            reason = "Worker 계획 변경 요청"
        else:
            reason = "제약을 모두 만족하는 후보가 0개"
        if ctx.worker_requests and reason != "Worker 계획 변경 요청":
            reason += f" · Worker 계획 변경 요청 {len(ctx.worker_requests)}건"
        offset = s.get("detail_offset", 0) + (TOP_K if strategy == "more_candidates" else 0)
        emit("replan", n=n, cap=REPLAN_CAP, strategy=strategy, reason=reason,
             worker_requests=list(ctx.worker_requests), by="rule")
        ctx.worker_requests.clear()
        return {"replan_count": n, "tried": s.get("tried", []) + [strategy], "detail_offset": offset,
                "replans": s.get("replans", []) + [{"n": n, "strategy": strategy, "reason": reason}],
                "_exit": {"n": n, "strategy": strategy}}

    def _next_strategy(s):
        slots, tried, searched = s["slots"], s.get("tried", []), s.get("searched", {})
        if "include_savings_bank" not in tried and slots.get("allow_savings_bank") is not False \
                and "savings_bank" not in (searched.get("saving", []) + searched.get("deposit", [])):
            return "include_savings_bank"
        if "add_deposit" not in tried and (slots.get("lump_sum") or 0) > 0 and not searched.get("deposit") \
                and slots.get("product_pref") != "saving":
            return "add_deposit"
        # 받은 후보 중 아직 상세 안 본 상품이 남아 있으면 (여러 번 가능)
        nxt = s.get("detail_offset", 0) + TOP_K
        for sid in s.get("search_task_ids", []):
            r = s.get("results", {}).get(sid)
            if r and r.get("ok") and len(r["data"]["products"]) > nxt:
                return "more_candidates"
        return None

    # 6. 영업점
    def enrich(s):
        v = dict(s["verdict"])
        top = v["recommendations"][0]
        banks = [p["bank"] for p in (top["saving_product"], top["deposit_product"]) if p]
        branches = {}
        tasks = [{"id": f"e.t{i + 1}", "tool": "find_bank_branches", "args": {"bank_name": b, "region": s["slots"]["region"]},
                  "fixed": ["region"], "required": False} for i, b in enumerate(dict.fromkeys(banks))]
        emit("plan", version=s["plan_version"] + 1, intent="enrich", reason="1순위 추천 은행의 영업점 (보조 정보)",
             tasks=[{"id": t["id"], "tool": t["tool"], "args": t["args"], "fixed": t["fixed"]} for t in tasks])
        for t in tasks:
            r = run_task(ctx, t, dict(t["args"]))
            branches[t["args"]["bank_name"]] = r
        v["branches"] = branches
        return {"verdict": v, "plan_version": s["plan_version"] + 1, "_exit": {"banks": list(branches)}}

    # 7. 최종 응답
    def respond(s):
        view, facts, kind = _render(s, ctx)
        summary = ctx.llm.compose(kind, facts) if kind in ("plan", "fact", "knowledge", "chitchat") else ""
        answer = "\n\n".join(x for x in [summary, view.get("body_text")] if x)
        view["summary"] = summary
        return {"answer": answer, "view": view, "_exit": {"kind": kind}}

    g = StateGraph(State)
    for name, fn in [("analyze", analyze), ("plan", plan), ("execute", execute), ("judge", judge),
                     ("replan", replan), ("enrich", enrich), ("respond", respond)]:
        g.add_node(name, traced(name, fn))
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "plan")
    g.add_conditional_edges("plan", lambda s: "execute" if s["tasks"] else "respond", ["execute", "respond"])
    g.add_edge("execute", "judge")
    g.add_conditional_edges("judge", route_after_judge, ["replan", "enrich", "respond"])
    g.add_edge("replan", "plan")
    g.add_edge("enrich", "respond")
    g.add_edge("respond", END)
    return g.compile()


# 헬퍼
def _won(v):
    return None if v is None else f"{int(v):,}원"


def _disp(bank: str) -> str:
    """화면용 은행 이름 ('주식회사' 떼기). Tool 에 넘길 때는 원문 그대로 씀"""
    return re.sub(r"^(주식회사|\(주\))\s*|\s*(주식회사|\(주\))$", "", bank or "")


def _pinfo(p):
    if not p:
        return None
    return {k: p.get(k) for k in ("fin_prdt_cd", "fin_co_no", "bank", "product", "product_type", "base_rate", "max_rate",
                                  "interest_type", "reserve_type", "max_limit", "join_way", "special_condition", "fin_group",
                                  "disclosure_month", "join_member")}


def _compare_terms(s, term):
    """비교해 달라고 한 다른 기간들 (공시 기간으로 맞춤, 주 기간은 뺌)"""
    out = []
    for m in (s.get("analysis") or {}).get("compare_months") or []:
        t, _ = _normalize_term(int(m))
        if t != term and t not in out:
            out.append(t)
    return out[:2]


def _compare(goal, term, budget, existing, savings_all, conds, s):
    """기간별로 가장 적게 넣어도 되는 적금 하나씩 골라서 비교"""
    rows = []
    room = None if budget is None else budget - existing
    for t in [term] + _compare_terms(s, term):
        best = None
        for d in savings_all.values():
            if d.get("term_months") != t or not calc.personal_ok(d) or d.get("join_deny") in ("2", "3"):
                continue
            rate = bonus_mod.apply(d, conds)[0] if conds else d["base_rate"]
            need = calc.required_monthly(goal, t, rate, d.get("interest_type") or "S")
            if d.get("max_limit") and need > d["max_limit"]:
                continue
            if best is None or need < best["required_monthly"]:
                best = {"months": t, "label": f"{_disp(d['bank'])} {d['product']}", "rate": rate, "required_monthly": need}
        if best:
            best["ok"] = room is None or best["required_monthly"] <= room
            rows.append(best)
    return rows


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        k = (i["product"], i["reason"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _adjustments(goal, term, budget, lump, savings, deposits, existing, slots):
    """통과한 후보가 없을 때 뭘 바꾸면 되는지 계산"""
    open_sav = [x for x in savings.values() if x.get("join_deny") not in ("2", "3")]
    if not open_sav:
        return None
    # 기준 상품: 월 한도 없거나 넉넉한 것 중 기본금리 높은 것
    def fits(x):
        need_x = calc.required_monthly(goal, term, x["base_rate"], x.get("interest_type") or "S", lump)
        return not x.get("max_limit") or x["max_limit"] >= need_x
    best = max(open_sav, key=lambda x: (fits(x), x["base_rate"]))
    dep_best = max(deposits.values(), key=lambda x: x["base_rate"]) if deposits and lump else None
    lump_after = (calc.project_deposit(lump, dep_best["base_rate"], term, dep_best.get("interest_type") or "S").maturity_amount
                  if dep_best else lump)
    need = calc.required_monthly(goal, term, best["base_rate"], best.get("interest_type") or "S", lump_after)
    avail = None if budget is None else max(0, budget - existing)
    cap = min([x for x in [avail, best.get("max_limit")] if x is not None], default=None)
    pay = need if cap is None else min(need, cap)
    reach = calc.project_saving(pay, best["base_rate"], term, best.get("interest_type") or "S").maturity_amount + lump_after \
        if pay else lump_after
    longer = None
    if avail:
        for t in [x for x in STANDARD_TERMS if x > term]:
            if calc.project_saving(min(avail, best.get("max_limit") or avail), best["base_rate"], t,
                                   best.get("interest_type") or "S").maturity_amount + lump_after >= goal:
                longer = t
                break
    return {"basis": f"{_disp(best['bank'])} {best['product']} 기본금리 {best['base_rate']}% 기준",
            "required_monthly": need, "reachable_total": reach, "longer_term": longer,
            "existing_monthly": existing, "budget": budget,
            "budget_needed": (need + existing) if budget is not None else None,
            "over_limit": bool(best.get("max_limit") and need > best["max_limit"]),
            "max_limit": best.get("max_limit"),
            "savings_bank_blocked": slots.get("allow_savings_bank") is False}


def _render(s, ctx):
    intent = s["intent"]
    slots = s.get("slots", {})
    if ctx.fatal:
        msg = (f"'{ctx.fatal['tool']}' 을(를) 부를 API 키가 없거나 올바르지 않아 계산을 진행하지 못했습니다. "
               "backend/.env 에 키를 넣은 뒤 서버를 다시 시작해 주세요. (README → API 키 설정)")
        return {"type": "error", "error_code": "INVALID_KEY", "body_text": msg}, {"text": msg}, "error"
    if ctx.ask_user:
        e = ctx.ask_user
        msg = f"확인이 필요합니다: {e['message']}" + (f" (후보: {', '.join(e.get('options', []))})" if e.get("options") else "")
        return {"type": "ask", "body_text": msg}, {"text": msg}, "ask"
    if intent == "plan" and s.get("missing"):
        lab = {"goal_amount": "모으려는 목표 금액", "months": "모으는 기간(예: 1년, 6개월)"}
        q = "계획을 세우려면 " + "과 ".join(lab[m] for m in s["missing"]) + "이 필요합니다. 알려 주시겠어요?"
        return {"type": "ask", "missing": s["missing"], "body_text": q}, {"text": q}, "ask"
    if intent == "plan":
        v = s["verdict"]
        recs = [{"rank": i + 1, "label": r["label"], "expected_total": r["expected_total"],
                 "monthly_payment": r["monthly_payment"], "required_monthly": r["required_monthly"],
                 "best_case_total": r["best_case_total"], "real_rate": r["real_rate"], "warnings": r["warnings"],
                 "saving": r["saving"], "deposit": r["deposit"], "saving_product": r["saving_product"],
                 "deposit_product": r["deposit_product"], "bonus_applied": r.get("bonus_applied") or [],
                 "rate_used": (r["saving"] or {}).get("rate_used"), "base_rate_raw": r.get("base_rate_raw")}
                for i, r in enumerate(v["recommendations"])]
        lines = []
        if v.get("term_changed") and v["term"] != v["months"]:
            lines.append(f"※ 요청 기간 {v['months']}개월은 공시 기간이 아니어서, 그 안에 끝나는 {v['term']}개월 상품으로 계산했습니다. "
                         f"남는 {v['months'] - v['term']}개월은 계산에 넣지 않았습니다.")
        if v.get("existing_monthly"):
            lines.append(f"※ 캘린더에 이미 매달 {v['existing_monthly']:,}원 납입이 있어 월 예산에서 뺐습니다.")
        if v.get("bonus_conds"):
            lines.append(f"※ 말씀하신 우대 조건({', '.join(v['bonus_conds'])})은 공시 우대조건 문구에서 찾은 만큼만 금리에 더했습니다. "
                         "실제 적용 여부는 가입 전에 은행에서 확인하세요.")
        if not recs and v.get("adjustments"):
            a = v["adjustments"]
            if a.get("existing_monthly") and a.get("budget_needed"):
                opts = [f"월 예산을 {a['budget_needed']:,}원으로 늘리기 (이미 잡힌 납입 {a['existing_monthly']:,}원 + "
                        f"새 계획 {a['required_monthly']:,}원, {a['basis']})"]
            else:
                opts = [f"월 납입을 {a['required_monthly']:,}원으로 늘리기 ({a['basis']})"]
            if a.get("over_limit"):
                opts[0] += f" — 단, 이 상품의 월 한도 {a['max_limit']:,}원을 넘으므로 상품을 나눠 가입해야 합니다"
            if a["reachable_total"] > 0:
                opts.append(f"목표를 {a['reachable_total']:,}원으로 낮추기 (지금 조건으로 모을 수 있는 최대)")
            else:
                opts.append("이미 잡힌 납입이 월 예산을 모두 쓰고 있어 지금 예산으로는 새 계획을 넣을 수 없습니다")
            if a.get("longer_term"):
                opts.append(f"기간을 {a['longer_term']}개월로 늘리기")
            if a.get("savings_bank_blocked"):
                opts.append("저축은행 상품도 허용하기")
            lines.append("조정 방안: " + " / ".join(opts))
        lines.append(DISCLAIMER)
        strat_ko = {"include_savings_bank": "저축은행 권역 추가", "add_deposit": "예금 조합 추가",
                    "more_candidates": "다음 순위 상품 조회", "rebuild_verified": "검증된 작업으로 다시 구성"}
        replans = s.get("replans") or []
        cond = {"목표 금액": _won(v["goal"]), "기간": f"{v['months']}개월", "월 예산 상한": _won(v["budget"]),
                "지금 가진 목돈": _won(v["lump_sum"] or None)}
        if v.get("existing_monthly") and v.get("budget") is not None:
            cond["캘린더에 이미 잡힌 월 납입"] = _won(v["existing_monthly"])
            cond["새 계획에 쓸 수 있는 월 예산"] = _won(max(0, v["budget"] - v["existing_monthly"]))
        facts = {"사용자 조건": cond,
                 "결과": (f"조건을 모두 만족하는 조합 {len(recs)}개" if recs else "조건을 모두 만족하는 조합 없음"),
                 "판단 기준": "기본금리로 계산한 세후 만기 수령액이 목표 이상이고, 필요 월 납입이 (이미 잡힌 납입을 뺀) 월 예산과 상품 월 한도 이하, 개인 가입 가능",
                 "추천안(순위순)": [{"상품": r["label"], "매달 넣을 돈": _won(r["monthly_payment"]),
                                  "세후 예상 수령액": _won(r["expected_total"]),
                                  "물가 뺀 실질 수익률": None if r["real_rate"] is None else f"{r['real_rate']}%p"}
                                 for r in recs],
                 "재계획": (f"{len(replans)}회 — " + " → ".join(strat_ko.get(x["strategy"], x["strategy"]) for x in replans)
                          + " (처음 조회한 은행권 상품으로는 조건을 못 맞춰서)") if replans else (
                     "안 함 — 이미 잡힌 납입이 월 예산을 다 써서 상품을 더 찾아도 통과할 수 없음"
                     if v.get("budget") is not None and v["budget"] - v.get("existing_monthly", 0) <= 0 else "없음"),
                 "제외한 후보 수": len(v["excluded"]),
                 "물가상승률(%)": (v.get("inflation") or {}).get("inflation_yoy")}
        if facts.get("재계획") == "없음":  # 없으면 요약에서 굳이 말하지 않게 뺌
            facts.pop("재계획")
        if v.get("bonus_conds"):
            facts["반영한 우대 조건"] = v["bonus_conds"]
            facts["판단 기준"] = facts["판단 기준"].replace("기본금리로", "기본금리 + 말한 조건의 우대금리로")
            for f, r in zip(facts["추천안(순위순)"], recs):
                f["적용 금리"] = f"{r['rate_used']}% (기본 {r['base_rate_raw']}% + 우대 {', '.join(b['cond'] for b in r['bonus_applied']) or '없음'})"
        if v.get("comparison"):
            facts["기간 비교"] = [{"기간": f"{c['months']}개월", "결과": "가능" if c["ok"] else "어려움",
                               "상품": c["label"], "필요 월 납입": _won(c["required_monthly"])} for c in v["comparison"]]
        if not recs and v.get("adjustments"):
            a = v["adjustments"]
            facts["조정 방안"] = {"필요한 월 납입": _won(a["required_monthly"]), "지금 조건으로 모을 수 있는 최대": _won(a["reachable_total"]),
                              "늘리면 되는 기간": f"{a['longer_term']}개월" if a.get("longer_term") else None, "기준 상품": a["basis"]}
        view = {"type": "plan", "conditions": mem_mod.grouped(slots), "recommendations": recs,
                "conditional": [{"label": c["label"], "unjudged": c["unjudged"], "expected_total": c["expected_total"]}
                                for c in v["conditional"]],
                "excluded": v["excluded"][:12], "adjustments": v.get("adjustments"), "inflation": v.get("inflation"),
                "branches": {k: (r["data"] if r["ok"] else {"error": r["error"]["message"]})
                             for k, r in (v.get("branches") or {}).items()},
                "replans": s.get("replans", []), "counts": v["counts"], "notes": lines,
                "comparison": v.get("comparison") or [],
                "criteria": "세후 만기수령액 ≥ 목표, 필요 월 납입 ≤ 월 예산 · 상품 한도, 가입 제한 없음 (기본금리로 판정) · 순위는 필요 월 납입이 적은 순",
                "disclaimer": DISCLAIMER, "body_text": "\n".join(lines)}
        return view, facts, "plan"
    if intent == "fact":
        econ = next((r["data"] for r in s["results"].values() if isinstance(r, dict) and r.get("ok")
                     and "inflation_yoy" in r["data"]), None)
        top = next((r["data"] for r in s["results"].values() if isinstance(r, dict) and r.get("ok")
                    and "products" in r["data"]), None)
        infl = (econ or {}).get("inflation_yoy")
        facts = {"질문": s["user"],
                 "소비자물가 상승률(전년 같은 달 대비)": f"{infl}% ({econ['cpi_month'][:4]}년 {int(econ['cpi_month'][4:])}월)" if econ else "확인 못 함",
                 "한국은행 기준금리": f"{econ['base_rate']}%" if econ and econ.get("base_rate") is not None else "확인 못 함",
                 "은행 12개월 적금 기본금리 상위 3개": [
                     {"상품": f"{_disp(p['bank'])} {p['product']}", "기본금리": f"{p['base_rate']}%",
                      "세후 금리에서 물가를 뺀 실질 수익률": (f"{calc.real_rate(p['base_rate'], infl)}%p" if infl is not None else "확인 못 함")}
                     for p in (top or {}).get("products", [])[:3]],
                 "주의": "사실만 전한다. 상품을 추천하거나 유리 · 불리를 단정하지 않는다."}
        lines = []
        if econ:
            lines.append(f"소비자물가 상승률 {econ['inflation_yoy']}% (계산: {econ['inflation_formula']}, {econ['note']})")
            if econ.get("base_rate") is not None:
                lines.append(f"한국은행 기준금리 {econ['base_rate']}% ({econ['base_rate_month']} 기준)")
        if top:
            lines.append("은행 12개월 적금 기본금리 상위: " + ", ".join(
                f"{_disp(p['bank'])} {p['product']} {p['base_rate']}%" for p in top["products"][:3]))
        lines.append(DISCLAIMER)
        return {"type": "fact", "econ": econ, "top": top, "body_text": "\n".join(lines)}, facts, "fact"
    if intent == "knowledge":
        r = next(iter(s["results"].values()), None)
        if r and r.get("ok"):
            hits = r["data"]["hits"]
            seen_src, src_lines = set(), []
            for h in hits[:3]:
                if h["url"] not in seen_src:
                    seen_src.add(h["url"])
                    src_lines.append(f"- 출처: {h['source']} ({h['url']}, 확인 {h['checked']})")
            src = "\n".join(src_lines)
            return ({"type": "knowledge", "hits": hits, "body_text": src}, {"hits": hits, "question": s["user"]}, "knowledge")
        msg = ("아직 안내 문서를 모으지 못했습니다(색인 없음)." if r and r["error"]["code"] == "NO_INDEX"
               else "관련 안내 문서를 찾지 못했습니다.")
        return {"type": "knowledge", "hits": [], "body_text": msg}, {"text": msg}, "notice"
    if intent == "save_plan":
        return _save_plan(s)
    if intent == "calendar":
        r = next(iter(s["results"].values()), None) if s.get("results") else None
        if r is None:
            msg = "캘린더에 넣을 계획이 없습니다. 먼저 추천안을 저장해 주세요(예: '1번으로 저장해줘')."
            return {"type": "ask", "body_text": msg}, {"text": msg}, "ask"
        if r["ok"]:
            ev = r["data"]["events"]
            msg = "캘린더에 등록했습니다: " + " / ".join(
                f"{e['title']} ({e['start_date']}부터 매달)" if e.get("kind") == "payment" else f"{e['title']} ({e['start_date']})"
                for e in ev)
            return {"type": "calendar", "events": ev, "body_text": msg}, {"text": msg}, "notice"
        return {"type": "calendar", "body_text": r["error"]["message"]}, {"text": r["error"]["message"]}, "notice"
    text = ("안녕하세요! 모아플랜은 목표 금액과 기간을 말하면 금감원 공시 금리로 예·적금 조합을 계산해 드려요. "
            "예: '1년 안에 1000만원 모으고 싶어, 월 70만원까지 넣을 수 있어'")
    return {"type": "chitchat", "body_text": ""}, {"text": text}, "chitchat"


def _save_plan(s):
    memory = s.get("memory") or {}
    recs = memory.get("last_recommendations") or []
    pick = s["analysis"].get("pick") or (1 if len(recs) == 1 else None)
    if not recs:
        msg = "저장할 추천안이 없습니다. 먼저 목표 금액과 기간으로 계획을 받아 보세요."
        return {"type": "ask", "body_text": msg}, {"text": msg}, "ask"
    if not pick or pick > len(recs):
        msg = f"몇 번 추천안을 저장할까요? (1~{len(recs)})"
        return {"type": "ask", "body_text": msg}, {"text": msg}, "ask"
    r = recs[pick - 1]
    start = s["analysis"].get("start_date") or date.today().isoformat()
    pay_day = s["analysis"].get("payment_day") or date.fromisoformat(start).day
    products = [p for p in (r.get("saving_product"), r.get("deposit_product")) if p]
    plan, overlaps = plans_store.create_plan({
        "title": r["label"][:60], "goal_amount": r["goal"], "start_date": start, "payment_day": pay_day,
        "monthly_amount": r["monthly_payment"], "lump_sum": r["lump_sum"], "term_months": r["term"],
        "products": products, "expected_amount": r["expected_total"], "session_id": s.get("session_id"),
        "memo": f"기본금리로 계산한 세후 예상액. 우대 충족 시 최대 {r.get('best_case_total') or r['expected_total']:,}원"})
    msg = (f"{plan['plan_id']}번 계획으로 저장했습니다: {plan['title']} · {plan['start_date']} ~ {plan['maturity_date']} · "
           f"매월 {plan['payment_day']}일 {plan['monthly_amount']:,}원")
    if not s["analysis"].get("payment_day"):
        msg += " (납입일을 말하지 않아 시작일 날짜로 정했습니다)"
    warn = plans_store.overlap_message(plan["monthly_amount"], overlaps)
    if warn:
        msg += "\n⚠ " + warn
    msg += "\n\"캘린더에 등록해줘\" 라고 하면 매달 납입일과 만기일을 일정으로 넣습니다."
    return ({"type": "saved", "plan": plan, "overlaps": overlaps, "body_text": msg, "saved_plan_id": plan["plan_id"]},
            {"text": msg}, "notice")
