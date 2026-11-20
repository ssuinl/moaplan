"""요구사항 체크를 코드로 확인

결과는 통과 / 실패 / 미확인 세 가지. 실행 못 해본 건 통과로 안 적음

python -m scripts.check --fake   키 없이
python -m scripts.check          .env 키로 (키 없는 항목은 미확인)
"""
from __future__ import annotations

import os
import sys
import tempfile

RESULTS: list[tuple[str, str, str]] = []


def check(no: str, name: str, cond: bool):
    RESULTS.append((no, name, "통과" if cond else "실패"))
    print(f"[{'통과' if cond else '실패'}] {no:<7} {name}")


def skip(no: str, name: str, why: str):
    RESULTS.append((no, name, "미확인"))
    print(f"[미확인] {no:<7} {name} — {why}")


def main():
    fake = "--fake" in sys.argv
    if fake:
        os.environ["MOAPLAN_FAKE"] = "1"
        os.environ["MOAPLAN_LLM"] = "fake"
    else:
        from dotenv import load_dotenv
        load_dotenv()
    os.environ["MOAPLAN_DB"] = os.path.join(tempfile.mkdtemp(), "check.db")  # 실제 DB 는 안 건드림

    from app import db
    db.init_db()
    from agent import calc
    from agent.service import run_chat
    from events.anomaly import evaluate
    from tools.base import REGISTRY, scrub

    # Tool
    tool = REGISTRY.get("search_savings_products")
    if fake or os.getenv("FINLIFE_API_KEY"):
        good = tool.run(product_type="saving", term_months=12)
        check("F3001", "정상 호출은 ok: True", good["ok"] is True)
    else:
        skip("F3001", "정상 호출은 ok: True", "FINLIFE_API_KEY 없음")
    bad = [tool.run(product_type="stock"), tool.run(foo=1), REGISTRY.run("없는_tool")]
    check("F3001", "잘못된 입력도 예외 없이 ok: False", all(b["ok"] is False for b in bad))
    check("F3001", "실패 응답은 code · message 가 있다", all(b["error"].get("code") and b["error"].get("message") for b in bad))
    check("NF3002", "오류 문구에 키가 남지 않는다", "abcd1234" not in scrub("...?auth=abcd1234&x=1 KakaoAK abcd1234"))

    # Agent 흐름
    if fake or os.getenv("GMS_API_KEY"):
        r = run_chat("1년 안에 1000만원 모으고 싶어. 월 90만원까지 넣을 수 있어")
        ev = [e["event"] for e in r["events"]]
        check("F3006", "계획 → Tool 호출 → 판단 → 응답 순서", ev.index("plan") < ev.index("tool.call") < ev.index("judge"))
        check("F3008", "추천안은 판단 기준을 만족한다",
              all(x["expected_total"] >= 10_000_000 and x["monthly_payment"] <= 900_000 for x in r["view"]["recommendations"]))
        check("F3008", "개인 가입 불가 상품은 추천하지 않는다",
              all(calc.personal_ok(x["saving_product"]) for x in r["view"]["recommendations"]))
        hi = run_chat("안녕, 뭐 할 수 있어?")
        check("F3005", "인사에는 Tool 을 부르지 않는다", hi["stats"]["tool_calls"] == 0)
        ask = run_chat("돈 좀 모으고 싶어")
        check("F3005", "조건이 모자라면 Tool 전에 되묻는다", ask["view"]["type"] == "ask" and ask["stats"]["tool_calls"] == 0)
        cap = run_chat("1년 안에 5000만원 모으고 싶어. 월 100만원까지")
        check("NF3004", "재계획은 상한 3회를 넘지 않는다", cap["stats"]["replans"] <= 3)
        follow = run_chat("월 85만원으로 바꿔줘", r["session_id"])
        check("F3010", "후속 요청은 직전 결과를 재사용한다", follow["stats"]["cache_hits"] > 0)
    else:
        skip("F3005~11", "Agent 흐름", "GMS_API_KEY 없음 — --fake 로 다시 실행")

    # 로깅, 이상 징후
    check("F3020", "기록이 없으면 '이상 없음'이 아니라 판정 불가", evaluate([])["verdict"] == "undecidable")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        sid = c.post("/api/chat", json={"message": "안녕"}).json()["session_id"]
        sid = sid if c.get(f"/api/logs/{sid}").status_code == 200 else None
        if sid:
            check("F3014", "기록은 고칠 수 없다 (PUT → 405)", c.put(f"/api/logs/{sid}", json={}).status_code == 405)
        else:
            skip("F3014", "기록은 고칠 수 없다", "저장된 기록 없음")
    from events.bus import EventBus
    alone = run_chat("안녕", bus=EventBus())  # 로깅 Agent 구독 안 한 버스
    check("F3012", "구독 줄을 빼도 Agent 가 돈다 (3일차 체크 15)", bool(alone["answer"]))

    n = {k: sum(1 for *_, v in RESULTS if v == k) for k in ("통과", "실패", "미확인")}
    print(f"\n통과 {n['통과']} · 실패 {n['실패']} · 미확인 {n['미확인']}")
    sys.exit(1 if n["실패"] else 0)


if __name__ == "__main__":
    main()
