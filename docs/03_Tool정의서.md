# 3. Tool 정의서

## 3.1 공통 약속

- **응답:** 성공은 `{"ok": true, "data": {...}}`, 실패는 `{"ok": false, "error": {"code", "message", ...}}` 입니다. 예외를 밖으로 던지지 않습니다(`Tool.run`이 마지막 안전망).
- **LLM에게 넘기는 것:** 이름, 설명, 입력 스키마만 넘깁니다(`REGISTRY.catalog()`). 설명에는 "언제 부르는지"를 적습니다.
- **원본 응답:** 통째로 넘기지 않고 판단에 쓸 값만 뽑습니다. 비어 있는 값은 0이나 ""로 채우지 않습니다.

| 실패 코드 | 뜻 | Worker가 LLM으로 고칠 수 있나 |
|---|---|---|
| BAD_ARGUMENT | 입력 형식이 틀림, 모르는 인자 | O |
| MISSING_ARGUMENT | 필수 인자 없음 → 사용자에게 되묻기 | X |
| INVALID_KEY | 키 없음·틀림 → 바로 설정 안내, 재계획 안 함 | X |
| NOT_FOUND | 식별자 없음 | O |
| NO_DATA | 조회됐으나 결과 없음 | O |
| NO_RESULT | 보조 정보 검색 결과 없음 | O |
| AMBIGUOUS | 같은 이름이 여러 지역에 있음 → 되묻기 | X |
| NO_RATE | 해당 기간 금리 미공시 → 판단하지 않고 제외 | X |
| NO_INDEX | RAG 색인 없음 | X |
| RATE_LIMITED | 호출 한도 초과 | X |
| UPSTREAM_ERROR | 외부 서버 오류·시간 초과 (캐시하지 않음) | X |

## 3.2 외부 API 툴

### search_savings_products — 후보 조회 (F3002)

| 항목 | 내용 |
|---|---|
| 언제 | 상품을 아직 정하지 않았을 때. 매달 넣을 돈은 saving, 목돈은 deposit |
| 입력 | `product_type`(deposit/saving, 필수), `term_months`(기본 12), `fin_group`(bank/savings_bank, 기본 bank), `limit` |
| 출력 | `term_months`(실제 조회 기준), `requested_term`, `is_estimated`, `note`, `disclosure_month`, `count`, `products[{fin_prdt_cd, fin_co_no, bank, product, base_rate, max_rate, interest_type}]` (기본금리 순) |
| 실패 | BAD_ARGUMENT, INVALID_KEY, RATE_LIMITED, NO_DATA, UPSTREAM_ERROR |
| 다음 입력 | `fin_prdt_cd`, `product_type`, `term_months`, `fin_group` → get_product_detail |
| 추정 표시 | 기간을 안 주면 "기본값 12개월", 공시 기간이 아니면 "요청 10개월 → 6개월 기준" |

### get_product_detail — 상세 조회 (F3003)

| 항목 | 내용 |
|---|---|
| 언제 | 후보가 목표를 채울 수 있는지 계산하기 전 |
| 입력 | `fin_prdt_cd`, `product_type`, `term_months` (필수), `fin_group` |
| 출력 | `base_rate`(판정용), `max_rate`(우대 최대), `interest_type`(S/M), `reserve_type`, `max_limit`(없으면 null), `join_deny`(1/2/3), `join_member`, `join_way`, `special_condition`, `after_maturity`, `available_terms` |
| 실패 | NOT_FOUND, NO_RATE(+available_terms), INVALID_KEY … |

### get_economic_indicators — 물가·기준금리

| 항목 | 내용 |
|---|---|
| 언제 | 실질 수익률 판단, 또는 금리·물가 수준 질문 |
| 입력 | 없음 |
| 출력 | `inflation_yoy`(전년동월대비 %, 지수로 계산), `inflation_formula`, `cpi_month`, `base_rate`, `base_rate_month`, `is_estimated`, `note` |
| 실패 | INVALID_KEY, NO_DATA, RATE_LIMITED, UPSTREAM_ERROR. 기준금리만 실패하면 `base_rate_note`로 표시하고 나머지는 반환 |

### find_bank_branches — 보조 정보 (F3004)

| 항목 | 내용 |
|---|---|
| 언제 | 지역을 말했고 추천 은행의 영업점이 필요할 때 |
| 입력 | `bank_name`, `region` (필수), `size` |
| 출력 | `query`, `count`, `branches[{name, address, phone, url}]`, `note`("지점별 판매 상품은 확인하지 않음") |
| 결과 없음 | `NO_RESULT` + `query` + `hint` |
| 모호 | `AMBIGUOUS` + `options`(시·도 목록) |

## 3.3 로컬 서버 API 툴 (심화)

| Tool | 입력 | 출력 | 실패 |
|---|---|---|---|
| rag_search (F3022) | `query`, `top_k` | `hits[{text, title, section, source, url, checked, score}]` | NO_INDEX, NO_RESULT, MISSING_ARGUMENT |
| get_schedule (F3023) | `start_date`, `end_date` (선택) | `events[]`, `monthly_committed`, `calendar`(local/google) | BAD_ARGUMENT |
| register_plan_schedule (F3023) | **`plan_id`만** | `events[]`(매월 반복 납입 + 만기) | NOT_FOUND, BAD_ARGUMENT(이미 등록) |

## 3.4 Tool이 아닌 것

- **계획 저장:** 사용자가 이미 고른 값을 다루는 작업이라 Tool로 두지 않고, 서버 코드가 직접 저장합니다.
- **계산·판정:** 모델 판단이 필요 없어서 실행 흐름(judge)이 직접 합니다.

## 3.5 등록 및 확장 방법

```python
# tools/새도구.py
from .base import REGISTRY, Tool, ok, fail

def my_tool(arg: str) -> dict:
    if not arg:
        return fail("MISSING_ARGUMENT", "arg 는 필수입니다.")
    ...
    return ok({...})

REGISTRY.register(Tool(name="my_tool", description="이럴 때 부른다 …",
                       parameters={"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]},
                       func=my_tool, kind="external", source="출처"))
# tools/__init__.py 에 import 한 줄 추가 → graph.plan 에서 작업으로 넣기
```

캘린더 구현체 교체는 `MOAPLAN_CALENDAR=google`과 `backend/data/credentials.json`(Google Cloud OAuth 데스크톱 클라이언트)로 합니다. `LocalCalendar`와 `GoogleCalendar`는 같은 `list_events`·`add_events`를 갖고 Tool 입출력은 그대로입니다.
