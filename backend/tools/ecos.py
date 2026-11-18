"""한국은행 ECOS API (물가, 기준금리)

https://ecos.bok.or.kr/api  StatisticSearch
  /api/StatisticSearch/{KEY}/json/kr/{시작}/{끝}/{통계표}/{주기}/{시작시점}/{종료시점}/{항목}
  소비자물가지수: 901Y009 / 0 / M  (2020=100)
  기준금리: 722Y001 / 0101000 / M
하루 호출 한도가 있어서 1시간 캐시

물가상승률은 지수로 직접 계산: (이번 달 / 12개월 전 - 1) * 100
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import requests

from .base import REGISTRY, Tool, fail, fake_mode, ok, scrub

BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
SOURCE = "한국은행 경제통계시스템 ECOS Open API (ecos.bok.or.kr)"
FIXTURES = Path(__file__).parent / "fixtures"
SERIES = {
    "cpi": ("901Y009", "M", "0"),
    "base_rate": ("722Y001", "M", "0101000"),
}
_CACHE: dict[str, tuple[float, list]] = {}
_TTL = 3600


def _ym(d: date, back_months: int = 0) -> str:
    y, m = d.year, d.month - back_months
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}{m:02d}"


def _series(name: str, months: int) -> tuple[list | None, dict | None]:
    ck = f"{name}:{fake_mode()}"
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1], None
    code, cycle, item = SERIES[name]
    if fake_mode():
        rows = json.loads((FIXTURES / f"ecos_{code}.json").read_text(encoding="utf-8"))["StatisticSearch"]["row"]
        _CACHE[ck] = (time.time(), rows)
        return rows, None
    key = os.getenv("ECOS_API_KEY")
    if not key:
        return None, fail("INVALID_KEY", "ECOS_API_KEY 가 설정되지 않았습니다.")
    today = date.today()
    url = f"{BASE}/{key}/json/kr/1/100/{code}/{cycle}/{_ym(today, months)}/{_ym(today)}/{item}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        body = res.json()
    except (requests.RequestException, ValueError) as e:
        print("[ecos]", scrub(e))
        return None, fail("UPSTREAM_ERROR", "경제지표를 가져오지 못했습니다.")
    if "RESULT" in body:  # 오류나 결과 없음은 RESULT 로 옴
        c = body["RESULT"].get("CODE", "")
        if c == "INFO-200":
            return None, fail("NO_DATA", "해당 기간의 경제지표가 없습니다.")
        if c in ("INFO-100", "ERROR-300", "ERROR-301"):
            return None, fail("INVALID_KEY", "ECOS 인증키를 확인하세요.")
        if c == "ERROR-602":
            return None, fail("RATE_LIMITED", "ECOS 호출 한도를 넘었습니다.")
        return None, fail("UPSTREAM_ERROR", f"ECOS 오류 {c}")
    rows = body.get("StatisticSearch", {}).get("row", [])
    if not rows:
        return None, fail("NO_DATA", "해당 기간의 경제지표가 없습니다.")
    _CACHE[ck] = (time.time(), rows)
    return rows, None


def get_economic_indicators() -> dict:
    """최근 물가상승률(전년동월대비), 기준금리"""
    cpi_rows, err = _series("cpi", 14)
    if err:
        return err
    by_month = {r["TIME"]: float(r["DATA_VALUE"]) for r in cpi_rows if r.get("DATA_VALUE")}
    months = sorted(by_month)
    latest = months[-1]
    prev_year = str(int(latest[:4]) - 1) + latest[4:]
    if prev_year not in by_month:
        return fail("NO_DATA", f"{prev_year} 물가지수가 없어 상승률을 계산할 수 없습니다.")
    inflation = round((by_month[latest] / by_month[prev_year] - 1) * 100, 2)

    data = {
        "cpi_month": latest, "cpi_index": by_month[latest], "cpi_index_prev_year": by_month[prev_year],
        "inflation_yoy": inflation,
        "inflation_formula": f"({by_month[latest]} / {by_month[prev_year]} - 1) x 100",
        "base_rate": None, "base_rate_month": None,
    }
    br_rows, err2 = _series("base_rate", 3)
    if not err2 and br_rows:
        last = sorted(br_rows, key=lambda r: r["TIME"])[-1]
        data["base_rate"], data["base_rate_month"] = float(last["DATA_VALUE"]), last["TIME"]
    else:
        data["base_rate_note"] = "기준금리는 이번에 확인하지 못했습니다."  # 일부 실패는 사유만 남김
    data["is_estimated"] = latest != _ym(date.today())
    data["note"] = f"물가는 {latest[:4]}년 {int(latest[4:])}월 공표값 기준입니다(최신 달은 아직 공표 전)."
    return ok(data)


REGISTRY.register(Tool(
    name="get_economic_indicators",
    description=("추천 상품의 금리가 물가를 이기는지(실질 수익률) 판단해야 할 때, 또는 사용자가 요즘 금리·물가 수준을 "
                 "물을 때 부른다. 최근 소비자물가 상승률(전년동월대비)과 한국은행 기준금리를 돌려준다."),
    parameters={"type": "object", "properties": {}},
    func=get_economic_indicators, kind="external", source=SOURCE,
))
