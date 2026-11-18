"""금감원 금융상품 한눈에 API (예금, 적금)

https://finlife.fss.or.kr
  예금: /finlifeapi/depositProductsSearch.json
  적금: /finlifeapi/savingProductsSearch.json
  baseList(상품 정보) + optionList(기간별 금리), fin_co_no + fin_prdt_cd 로 연결
  err_cd: 000 정상, 010~013 인증키, 020 일일 한도, 100/101 파라미터, 900 서버

search_savings_products: 후보 목록
get_product_detail: 후보 상세 (판정에 필요한 값)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .base import REGISTRY, Tool, fail, fake_mode, ok, scrub

BASE = "https://finlife.fss.or.kr/finlifeapi"
SOURCE = "금융감독원 금융상품 한눈에 오픈API (finlife.fss.or.kr)"
FIXTURES = Path(__file__).parent / "fixtures"

PRODUCT_PATH = {"deposit": "depositProductsSearch", "saving": "savingProductsSearch"}
FIN_GROUP = {"bank": "020000", "savings_bank": "030300"}
FIN_GROUP_NAME = {"bank": "은행", "savings_bank": "저축은행"}
STANDARD_TERMS = [1, 3, 6, 12, 24, 36]  # 공시되는 저축 기간(개월)
DEFAULT_TERM = 12  # 기간 말 안 했을 때 기본값
JOIN_DENY = {"1": "제한없음", "2": "서민전용", "3": "일부제한"}

_ERR = {
    "010": ("INVALID_KEY", "금감원 인증키가 등록되지 않았습니다."),
    "011": ("INVALID_KEY", "금감원 인증키가 중지되었습니다."),
    "012": ("INVALID_KEY", "금감원 인증키가 삭제되었습니다."),
    "013": ("INVALID_KEY", "샘플 인증키로는 조회할 수 없습니다."),
    "020": ("RATE_LIMITED", "금감원 API 일일 조회 한도를 넘었습니다."),
    "021": ("INVALID_KEY", "허용되지 않은 IP 입니다."),
    "100": ("BAD_ARGUMENT", "필수 요청변수가 누락되었습니다."),
    "101": ("BAD_ARGUMENT", "요청변수 값이 올바르지 않습니다."),
    "900": ("UPSTREAM_ERROR", "금감원 API 서버 오류입니다."),
}

# 호출 한도 때문에 10분 캐시
_CACHE: dict[tuple, tuple[float, dict]] = {}
_TTL = 600
CALL_COUNTER = {"http": 0}


def _fetch_all(product_type: str, fin_group: str) -> tuple[dict | None, dict | None]:
    """권역 하나의 상품 목록 전체(모든 페이지). (본문, 실패) 반환"""
    key = (product_type, fin_group, fake_mode())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1], None

    group_code = FIN_GROUP[fin_group]
    if fake_mode():
        path = FIXTURES / f"finlife_{product_type}_{group_code}.json"
        body = json.loads(path.read_text(encoding="utf-8"))["result"]
        _CACHE[key] = (time.time(), body)
        return body, None

    auth = os.getenv("FINLIFE_API_KEY")
    if not auth:
        return None, fail("INVALID_KEY", "FINLIFE_API_KEY 가 설정되지 않았습니다.")

    merged = {"baseList": [], "optionList": []}
    page, max_page = 1, 1
    while page <= max_page:
        try:
            CALL_COUNTER["http"] += 1
            res = requests.get(f"{BASE}/{PRODUCT_PATH[product_type]}.json",
                               params={"auth": auth, "topFinGrpNo": group_code, "pageNo": page},
                               timeout=10)
            res.raise_for_status()
            body = res.json().get("result", {})
        except (requests.RequestException, ValueError) as e:
            print("[finlife]", scrub(e))
            return None, fail("UPSTREAM_ERROR", "금융상품 정보를 가져오지 못했습니다.")
        code = str(body.get("err_cd", "900"))
        if code != "000":
            c, m = _ERR.get(code, ("UPSTREAM_ERROR", body.get("err_msg", "알 수 없는 오류")))
            return None, fail(c, m)
        merged["baseList"] += body.get("baseList", []) or []
        merged["optionList"] += body.get("optionList", []) or []
        merged["dcls_month"] = body.get("baseList", [{}])[0].get("dcls_month") if body.get("baseList") else None
        max_page = int(body.get("max_page_no") or 1)
        page += 1
    _CACHE[key] = (time.time(), merged)
    return merged, None


def _options_by_product(body: dict) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for o in body.get("optionList", []):
        out.setdefault((o["fin_co_no"], o["fin_prdt_cd"]), []).append(o)
    return out


def _pick_option(options: list[dict], term: int, rsrv_type: str | None = None) -> dict | None:
    """요청 기간이랑 같은 기간 금리. 없으면 None (다른 기간 금리를 가져오지 않음)"""
    cands = [o for o in options if str(o.get("save_trm")) == str(term)]
    if rsrv_type:
        same = [o for o in cands if o.get("rsrv_type") == rsrv_type]
        cands = same or cands
    if not cands:
        return None
    # 같은 기간에 여러 개면 기본금리 높은 것
    return max(cands, key=lambda o: float(o.get("intr_rate") or 0))


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_term(term_months) -> tuple[int, bool]:
    """공시 기간이 아니면 그보다 짧은 것 중 가장 긴 기간으로 맞춤. (기간, 바뀌었는지) 반환"""
    if term_months in (None, "", 0):
        return DEFAULT_TERM, True
    t = int(term_months)
    if t in STANDARD_TERMS:
        return t, False
    shorter = [s for s in STANDARD_TERMS if s <= t]
    return (max(shorter) if shorter else STANDARD_TERMS[0]), True


# 후보 조회 Tool
def search_savings_products(product_type: str, term_months: int | None = None,
                            fin_group: str = "bank", limit: int = 8) -> dict:
    if product_type not in PRODUCT_PATH:
        return fail("BAD_ARGUMENT", "product_type 은 deposit(예금) 또는 saving(적금) 입니다.")
    if fin_group not in FIN_GROUP:
        return fail("BAD_ARGUMENT", "fin_group 은 bank 또는 savings_bank 입니다.")
    term, changed = _normalize_term(term_months)

    body, err = _fetch_all(product_type, fin_group)
    if err:
        return err
    opts = _options_by_product(body)
    items = []
    for b in body.get("baseList", []):
        o = _pick_option(opts.get((b["fin_co_no"], b["fin_prdt_cd"]), []), term)
        if o is None:
            continue  # 이 기간 금리 없는 상품은 뺌
        items.append({
            "fin_co_no": b["fin_co_no"], "fin_prdt_cd": b["fin_prdt_cd"],
            "bank": b["kor_co_nm"], "product": b["fin_prdt_nm"],
            "base_rate": _to_float(o.get("intr_rate")), "max_rate": _to_float(o.get("intr_rate2")),
            "interest_type": o.get("intr_rate_type"),
        })
    if not items:
        return fail("NO_DATA", f"{FIN_GROUP_NAME[fin_group]} {term}개월 {_kind(product_type)} 상품이 없습니다.")
    items.sort(key=lambda x: (x["base_rate"] or 0), reverse=True)
    note = None
    if changed:
        note = (f"요청 기간 {term_months}개월은 공시 기간이 아니라 {term}개월 기준으로 조회했습니다."
                if term_months else f"기간을 말하지 않아 기본값 {term}개월로 조회했습니다.")
    return ok({
        "product_type": product_type, "fin_group": fin_group,
        "requested_term": term_months, "term_months": term, "is_estimated": changed, "note": note,
        "disclosure_month": body.get("dcls_month") or (body.get("baseList") or [{}])[0].get("dcls_month"),
        "count": len(items), "products": items[: max(1, int(limit))],
    })


# 상세 조회 Tool
def get_product_detail(fin_prdt_cd: str, product_type: str, term_months: int,
                       fin_group: str = "bank") -> dict:
    if product_type not in PRODUCT_PATH:
        return fail("BAD_ARGUMENT", "product_type 은 deposit 또는 saving 입니다.")
    body, err = _fetch_all(product_type, fin_group if fin_group in FIN_GROUP else "bank")
    if err:
        return err
    base = next((b for b in body.get("baseList", []) if b["fin_prdt_cd"] == fin_prdt_cd), None)
    if base is None:
        return fail("NOT_FOUND", f"상품코드 {fin_prdt_cd} 를 찾을 수 없습니다.")
    options = _options_by_product(body).get((base["fin_co_no"], fin_prdt_cd), [])
    rsrv = "S" if product_type == "saving" else None  # 적금은 정액적립식 먼저
    o = _pick_option(options, int(term_months), rsrv)
    if o is None or _to_float(o.get("intr_rate")) is None:
        avail = sorted({int(x["save_trm"]) for x in options if str(x.get("save_trm", "")).isdigit()})
        return fail("NO_RATE", f"{base['fin_prdt_nm']} 은(는) {term_months}개월 금리가 공시되어 있지 않습니다.",
                    available_terms=avail)
    limit = _to_float(base.get("max_limit"))
    return ok({
        "fin_prdt_cd": fin_prdt_cd, "fin_co_no": base["fin_co_no"],
        "bank": base["kor_co_nm"], "product": base["fin_prdt_nm"], "product_type": product_type,
        "term_months": int(term_months),
        "base_rate": _to_float(o.get("intr_rate")),  # 판정용 (우대 X)
        "max_rate": _to_float(o.get("intr_rate2")),  # 우대 다 채웠을 때
        "interest_type": o.get("intr_rate_type"),  # S 단리, M 복리
        "reserve_type": o.get("rsrv_type"),  # S 정액, F 자유
        "max_limit": int(limit) if limit else None,  # 없으면 0 말고 None
        "join_deny": base.get("join_deny"), "join_deny_name": JOIN_DENY.get(str(base.get("join_deny")), "알수없음"),
        "join_member": base.get("join_member"), "join_way": base.get("join_way"),
        "special_condition": (base.get("spcl_cnd") or "").strip(),
        "after_maturity": (base.get("mtrt_int") or "").strip(),
        "disclosure_month": base.get("dcls_month"),
        "available_terms": sorted({int(x["save_trm"]) for x in options if str(x.get("save_trm", "")).isdigit()}),
    })


def _kind(pt):
    return "예금" if pt == "deposit" else "적금"


REGISTRY.register(Tool(
    name="search_savings_products",
    description=("목표 금액을 모을 예금·적금 상품을 아직 정하지 않았을 때 후보 목록을 얻는다. "
                 "매달 나눠 넣을 돈은 saving(적금), 지금 가진 목돈은 deposit(예금)으로 부른다. "
                 "기간(개월)을 주지 않으면 12개월 기준이다."),
    parameters={"type": "object", "properties": {
        "product_type": {"type": "string", "enum": ["deposit", "saving"]},
        "term_months": {"type": "integer", "description": "저축 기간(개월). 공시 기간 1·3·6·12·24·36"},
        "fin_group": {"type": "string", "enum": ["bank", "savings_bank"], "default": "bank"},
        "limit": {"type": "integer", "default": 8}},
        "required": ["product_type"]},
    func=search_savings_products, kind="external", source=SOURCE,
))

REGISTRY.register(Tool(
    name="get_product_detail",
    description=("후보 상품이 목표를 채울 수 있는지 계산하기 전에 부른다. 상품코드로 해당 기간의 기본·최고 금리, "
                 "단리/복리, 월 납입 한도, 가입 제한, 우대 조건을 확인한다."),
    parameters={"type": "object", "properties": {
        "fin_prdt_cd": {"type": "string"},
        "product_type": {"type": "string", "enum": ["deposit", "saving"]},
        "term_months": {"type": "integer"},
        "fin_group": {"type": "string", "enum": ["bank", "savings_bank"], "default": "bank"}},
        "required": ["fin_prdt_cd", "product_type", "term_months"]},
    func=get_product_detail, kind="external", source=SOURCE,
))
