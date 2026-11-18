"""카카오 로컬 키워드 검색 (은행 영업점)

https://dapi.kakao.com/v2/local/search/keyword.json
헤더 Authorization: KakaoAK {REST 키}, category_group_code=BK9 (은행)

영업점 검색으로는 그 지점에서 어떤 상품을 파는지 알 수 없어서 은행 이름으로만 찾음.
가입 방법이 인터넷/스마트폰뿐인 상품이면 영업점은 참고용
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from .base import REGISTRY, Tool, fail, fake_mode, ok, scrub

URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
SOURCE = "카카오 로컬 API (developers.kakao.com)"
FIXTURES = Path(__file__).parent / "fixtures"
SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
        "충북", "충남", "전북", "전남", "경북", "경남", "제주")


def _search(query: str, size: int) -> tuple[list | None, dict | None]:
    if fake_mode():
        data = json.loads((FIXTURES / "kakao_local.json").read_text(encoding="utf-8"))
        return data.get(query, []), None
    key = os.getenv("KAKAO_REST_API_KEY")
    if not key:
        return None, fail("INVALID_KEY", "KAKAO_REST_API_KEY 가 설정되지 않았습니다.")
    try:
        res = requests.get(URL, headers={"Authorization": f"KakaoAK {key}"},
                           params={"query": query, "category_group_code": "BK9", "size": size},
                           timeout=10)
        if res.status_code in (401, 403):
            return None, fail("INVALID_KEY", "카카오 REST 키를 확인하세요.")
        if res.status_code == 429:
            return None, fail("RATE_LIMITED", "카카오 API 호출 한도를 넘었습니다.")
        res.raise_for_status()
        return res.json().get("documents", []), None
    except (requests.RequestException, ValueError) as e:
        print("[kakao]", scrub(e))
        return None, fail("UPSTREAM_ERROR", "영업점 정보를 가져오지 못했습니다.")


def find_bank_branches(bank_name: str, region: str, size: int = 5) -> dict:
    bank_name, region = (bank_name or "").strip(), (region or "").strip()
    if not bank_name or not region:
        return fail("MISSING_ARGUMENT", "은행 이름과 지역이 모두 필요합니다.")
    query = f"{region} {bank_name}"
    docs, err = _search(query, size)
    if err:
        return err
    # 지점명에 은행 이름이 들어간 것만 (키워드 검색이 비슷한 곳도 섞어서 줌)
    core = bank_name.replace("은행", "").strip() or bank_name
    docs = [d for d in docs if core in d.get("place_name", "")]
    if not docs:
        return fail("NO_RESULT", f"'{region}' 근처에서 {bank_name} 영업점을 찾지 못했습니다.",
                    query=query, hint="지역을 넓히거나(구 → 시) 은행 공식 이름으로 다시 검색할 수 있습니다.")
    sidos = {d.get("address_name", "").split(" ")[0] for d in docs}
    if len(sidos) > 1 and not region.startswith(SIDO):
        return fail("AMBIGUOUS", f"'{region}' 이(가) 여러 지역에 있습니다: {', '.join(sorted(sidos))}",
                    options=sorted(sidos))
    return ok({
        "query": query, "count": len(docs),
        "branches": [{
            "name": d.get("place_name"), "address": d.get("road_address_name") or d.get("address_name"),
            "phone": d.get("phone") or None, "url": d.get("place_url"),
        } for d in docs[:size]],
        "note": "영업점 위치 정보입니다. 지점별 판매 상품은 확인하지 않았습니다.",
    })


REGISTRY.register(Tool(
    name="find_bank_branches",
    description=("사용자가 지역을 말했고 추천 상품을 영업점에서 가입하거나 상담받고 싶어 할 때 부른다. "
                 "추천 상품의 은행 이름과 지역으로 가까운 영업점 이름·주소·지도 링크를 찾는다."),
    parameters={"type": "object", "properties": {
        "bank_name": {"type": "string"}, "region": {"type": "string"},
        "size": {"type": "integer", "default": 5}},
        "required": ["bank_name", "region"]},
    func=find_bank_branches, kind="external", source=SOURCE,
))
