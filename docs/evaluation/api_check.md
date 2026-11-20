# 외부 API 호출 확인 (2026-11-19 23:48)

## 금감원 적금 후보 (12개월 · 은행)
- 결과: 성공
- 응답(앞부분): `{"product_type": "saving", "fin_group": "bank", "requested_term": 12, "term_months": 12, "is_estimated": false, "note": null, "disclosure_month": "202610", "count": 51, "products": [{"fin_co_no": "0010030", "fin_prdt_cd": "03700", "bank": "한국산업은행", "product": "KDB 자유적금", "base_rate": 3.91, "max_rate`

## 금감원 상품 상세 (KDB 자유적금)
- 결과: 성공
- 응답(앞부분): `{"fin_prdt_cd": "03700", "fin_co_no": "0010030", "bank": "한국산업은행", "product": "KDB 자유적금", "product_type": "saving", "term_months": 12, "base_rate": 3.91, "max_rate": 3.91, "interest_type": "S", "reserve_type": "F", "max_limit": 5000000, "join_deny": "1", "join_deny_name": "제한없음", "join_member": "개인,`
- 판단 속성 base_rate, interest_type, join_deny: 모두 있음

## 금감원 상세 — 없는 상품코드
- 결과: 실패 NOT_FOUND — 상품코드 NOPE 를 찾을 수 없습니다.

## 금감원 예금 후보 (12개월 · 저축은행)
- 결과: 성공
- 응답(앞부분): `{"product_type": "deposit", "fin_group": "savings_bank", "requested_term": 12, "term_months": 12, "is_estimated": false, "note": null, "disclosure_month": "202610", "count": 322, "products": [{"fin_co_no": "0010350", "fin_prdt_cd": "240031", "bank": "스카이저축은행", "product": "회전정기예금", "base_rate": 4.2, `

## ECOS 물가 · 기준금리
- 결과: 성공
- 응답(앞부분): `{"cpi_month": "202610", "cpi_index": 120.73, "cpi_index_prev_year": 117.42, "inflation_yoy": 2.82, "inflation_formula": "(120.73 / 117.42 - 1) x 100", "base_rate": 3.0, "base_rate_month": "202610", "is_estimated": true, "note": "물가는 2026년 10월 공표값 기준입니다(최신 달은 아직 공표 전)."}`
- 판단 속성 inflation_yoy, base_rate: 모두 있음

## 카카오 영업점 (한국산업은행 @ 강남)
- 결과: 성공
- 응답(앞부분): `{"query": "강남 한국산업은행", "count": 5, "branches": [{"name": "한국산업은행 강남지점", "address": "서울 강남구 영동대로 421", "phone": "02-560-0300", "url": "http://place.map.kakao.com/8634199"}, {"name": "한국산업은행 도곡지점", "address": "서울 강남구 남부순환로 2806", "phone": "02-3498-3800", "url": "http://place.map.kakao.com/8138498"}, {`

## 카카오 영업점 — 결과 없음
- 결과: 실패 NO_RESULT — '강남' 근처에서 없는은행이름 영업점을 찾지 못했습니다.

## LLM (GMS) 요청 분석
- 결과: `{"intent": "plan", "reason": "1년 안에 목표 금액(1000만원)과 월 예산 상한(월 90만원)을 제시해 저축 계획 수립 요청", "goal_amount": 10000000, "months": 12, "monthly_budget": 900000, "lump_sum": null, "product_pref": null, "allow_savings_bank": null, "region": null, "pick": null, "start_date": null, "payment_day": null, "topic": null}`

## LLM 이 Tool 명세를 보고 Tool 을 고르는지
- 고른 Tool: `["search_savings_products"]`
