"""Tool 테스트"""
import os

from tools import REGISTRY
from tools.base import scrub


def test_catalog_matches_spec():
    cards = {c["name"]: c for c in REGISTRY.catalog()}
    assert {"search_savings_products", "get_product_detail", "get_economic_indicators",
            "find_bank_branches"} <= {c["name"] for c in REGISTRY.catalog("external")}
    assert {"rag_search", "get_schedule", "register_plan_schedule"} == {c["name"] for c in REGISTRY.catalog("local")}
    for c in cards.values():  # LLM 에는 이름, 설명, 스키마만
        assert set(c) == {"name", "description", "parameters"}


def test_every_tool_fails_in_same_shape():
    bad = [REGISTRY.run("search_savings_products", product_type="loan"),
           REGISTRY.run("search_savings_products", product_type="saving", unknown=1),
           REGISTRY.run("get_product_detail", fin_prdt_cd="NOPE", product_type="saving", term_months=12),
           REGISTRY.run("find_bank_branches", bank_name="가온은행"),
           REGISTRY.run("no_such_tool")]
    for r in bad:
        assert r["ok"] is False and set(r["error"]) >= {"code", "message"}


def test_default_term_and_estimation_notice():
    r = REGISTRY.run("search_savings_products", product_type="saving")
    assert r["ok"] and r["data"]["term_months"] == 12 and r["data"]["is_estimated"]
    r = REGISTRY.run("search_savings_products", product_type="saving", term_months=10)
    assert r["data"]["term_months"] == 6 and "10개월" in r["data"]["note"]
    r = REGISTRY.run("search_savings_products", product_type="saving", term_months=12)
    assert r["data"]["is_estimated"] is False


def test_ids_flow_into_detail():
    s = REGISTRY.run("search_savings_products", product_type="deposit", term_months=12)
    first = s["data"]["products"][0]
    d = REGISTRY.run("get_product_detail", fin_prdt_cd=first["fin_prdt_cd"], product_type="deposit", term_months=12)
    assert d["ok"] and d["data"]["base_rate"] == first["base_rate"]


def test_missing_rate_is_not_invented():
    r = REGISTRY.run("get_product_detail", fin_prdt_cd="SAV-E1", product_type="saving", term_months=12)
    assert r["ok"] is False and r["error"]["code"] == "NO_RATE" and r["error"]["available_terms"] == [6]


def test_max_limit_empty_stays_none():
    r = REGISTRY.run("get_product_detail", fin_prdt_cd="SAV-A1", product_type="saving", term_months=12)
    assert r["data"]["max_limit"] is None


def test_branch_search_no_result_shape():
    r = REGISTRY.run("find_bank_branches", bank_name="없는은행", region="강남")
    assert r["error"]["code"] == "NO_RESULT" and "hint" in r["error"]


def test_inflation_is_computed_from_index():
    r = REGISTRY.run("get_economic_indicators")
    d = r["data"]
    assert round((d["cpi_index"] / d["cpi_index_prev_year"] - 1) * 100, 2) == d["inflation_yoy"]


def test_missing_keys_return_invalid_key(monkeypatch):
    monkeypatch.setenv("MOAPLAN_FAKE", "0")
    for name, args in [("search_savings_products", {"product_type": "saving"}),
                       ("get_economic_indicators", {}),
                       ("find_bank_branches", {"bank_name": "가온은행", "region": "강남"})]:
        r = REGISTRY.run(name, **args)
        assert r["ok"] is False and r["error"]["code"] == "INVALID_KEY"


def test_scrub_hides_keys(monkeypatch):
    monkeypatch.setenv("FINLIFE_API_KEY", "abcd1234secret")
    text = "GET https://x/finlifeapi/a.json?auth=abcd1234secret&pageNo=1 /api/StatisticSearch/KEYKEY/json"
    out = scrub(text)
    assert "abcd1234secret" not in out and "KEYKEY" not in out
    assert "zzkey" not in scrub("header KakaoAK zzkey failed")
