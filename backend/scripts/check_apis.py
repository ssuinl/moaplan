"""외부 API 3개 + LLM 을 실제 키로 한 번씩 호출해보기

backend 폴더에서: python -m scripts.check_apis
정상/실패 호출 결과랑 판정에 필요한 값이 응답에 있는지 확인.
결과는 docs/evaluation/api_check.md (키는 안 적음)
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
os.environ.setdefault("MOAPLAN_DB", str(ROOT / "data" / "check.db"))

from app import db  # noqa: E402
db.init_db()
from tools import REGISTRY  # noqa: E402

lines = [f"# 외부 API 호출 확인 ({datetime.now():%Y-%m-%d %H:%M})", ""]


def show(title, r, need=()):
    ok = r["ok"]
    missing = [k for k in need if ok and r["data"].get(k) in (None, "")]
    lines.append(f"## {title}\n- 결과: {'성공' if ok else '실패 ' + r['error']['code'] + ' — ' + r['error']['message']}")
    if ok:
        lines.append("- 응답(앞부분): `" + json.dumps(r["data"], ensure_ascii=False)[:300] + "`")
    if need:
        lines.append(f"- 판단 속성 {', '.join(need)}: " + ("모두 있음" if ok and not missing else f"비어 있음 {missing}"))
    lines.append("")
    print(title, "→", "OK" if ok else r["error"]["code"])


s = REGISTRY.run("search_savings_products", product_type="saving", term_months=12)
show("금감원 적금 후보 (12개월 · 은행)", s)
if s["ok"]:
    p = s["data"]["products"][0]
    show(f"금감원 상품 상세 ({p['product']})",
         REGISTRY.run("get_product_detail", fin_prdt_cd=p["fin_prdt_cd"], product_type="saving", term_months=12),
         need=("base_rate", "interest_type", "join_deny"))
    show("금감원 상세 — 없는 상품코드", REGISTRY.run("get_product_detail", fin_prdt_cd="NOPE", product_type="saving", term_months=12))
show("금감원 예금 후보 (12개월 · 저축은행)", REGISTRY.run("search_savings_products", product_type="deposit", term_months=12, fin_group="savings_bank"))
show("ECOS 물가 · 기준금리", REGISTRY.run("get_economic_indicators"), need=("inflation_yoy", "base_rate"))
bank = s["data"]["products"][0]["bank"] if s["ok"] else "신한은행"
show(f"카카오 영업점 ({bank} @ 강남)", REGISTRY.run("find_bank_branches", bank_name=bank, region="강남"))
show("카카오 영업점 — 결과 없음", REGISTRY.run("find_bank_branches", bank_name="없는은행이름", region="강남"))

if os.getenv("GMS_API_KEY"):
    from agent.llm import GMSLLM
    from tools import REGISTRY as R
    llm = GMSLLM()
    a = llm.analyze("1년 안에 1000만원 모으고 싶어. 월 90만원까지", {"slots": {}})
    lines += ["## LLM (GMS) 요청 분석", "- 결과: `" + json.dumps(a, ensure_ascii=False) + "`", ""]
    chooser = llm.chat.bind_tools([{"type": "function", "function": c} for c in R.catalog("external")])
    msg = chooser.invoke("1년 동안 적금 들 만한 은행 상품 후보 좀 찾아줘")
    lines += ["## LLM 이 Tool 명세를 보고 Tool 을 고르는지", "- 고른 Tool: `" +
              json.dumps([c["name"] for c in (msg.tool_calls or [])], ensure_ascii=False) + "`", ""]
    print("GMS → OK")
else:
    lines += ["## LLM (GMS)", "- GMS_API_KEY 가 없어 건너뜀", ""]

out = ROOT.parent / "docs" / "evaluation" / "api_check.md"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(lines), encoding="utf-8")
print("→", out)
