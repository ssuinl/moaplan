# 4. API 명세서

기본 주소 `http://localhost:8000`. 요청·응답 형식은 Pydantic(`backend/app/schemas.py`)으로 정의했고, `/docs`에서 직접 호출해 볼 수 있습니다.

## 대화

### POST /api/chat
메시지 한 건을 처리하고 응답과 이번 요청의 실행 이벤트를 함께 돌려줍니다.

```json
// 요청 ChatIn
{"message": "1년 안에 1000만원 모으고 싶어. 월 90만원까지", "session_id": null, "mode": "single", "worker_llm": true}
// 응답 ChatOut
{"session_id": "a1b2c3d4e5", "run_id": "9f8e7d6c", "mode": "single",
 "answer": "조건을 모두 만족하는 조합 2개를 …",
 "view": {"type": "plan", "conditions": {...}, "recommendations": [{"rank": 1, "label": "…", "expected_total": 10101489,
          "monthly_payment": 830000, "best_case_total": 10133438, "real_rate": 0.6, "saving": {"formula": "…"},
          "rate_used": 4.05, "base_rate_raw": 3.85, "bonus_applied": [{"cond": "자동이체", "rate": 0.2, "text": "자동이체시 우대금리 제공 : 연 0.20%p"}], ...}],
          "comparison": [{"months": 12, "label": "…", "rate": 3.85, "required_monthly": 1640000, "ok": false}, ...],
          "excluded": [{"product": "…", "reason": "필요 월 납입 830,000원 > 상품 월 한도 500,000원"}],
          "adjustments": null, "criteria": "…", "disclaimer": "…", "replans": []},
 "events": [{"event": "session.start", "session_id": "…", "run_id": "…", "seq": 1, "ts": "…", "payload": {...}}, ...],
 "stats": {"ms": 812, "llm_calls": 2, "tool_calls": 7, "cache_hits": 0, "replans": 0},
 "memory_slots": {"goal_amount": 10000000, "months": 12, "monthly_budget": 900000}}
```

| view.type | 뜻 |
|---|---|
| plan | 계획 결과 (추천·제외·조정 방안) |
| fact | 사실 조회 |
| knowledge | RAG 답변 (hits) |
| saved | 계획 저장 (plan, overlaps) |
| calendar | 일정 등록 |
| ask | 되묻기 |
| chitchat | 그 밖의 대화 |
| error | 오류 (error_code `INVALID_KEY`는 키 설정 안내) |

### POST /api/chat/stream
요청은 같습니다. `text/event-stream`으로 처리 중 이벤트(`event: event`)를 흘려보내고, 마지막에 `event: result`로 ChatOut을 보냅니다. 화면의 실시간 실행 기록이 이 경로를 씁니다.

## 계획 (F3015, F3016)

| 메서드 | 경로 | 요청 | 응답 |
|---|---|---|---|
| GET | /api/plans | — | PlanOut[] |
| POST | /api/plans | PlanIn | 201 PlanSaveResult |
| GET | /api/plans/{plan_id} | — | PlanOut / 404 |
| PUT | /api/plans/{plan_id} | PlanUpdate (바꿀 값만) | PlanSaveResult / 404 |
| DELETE | /api/plans/{plan_id} | — | 204 / 404 (연결된 캘린더 일정도 삭제) |

```json
// PlanIn
{"title": "여행 자금", "goal_amount": 3000000, "start_date": "2026-12-01", "term_months": 6,
 "payment_day": 10, "monthly_amount": 500000, "lump_sum": 0,
 "products": [{"product_type": "saving", "bank": "…", "product": "…", "base_rate": 3.1}]}
// PlanSaveResult
{"plan": {"plan_id": 2, "maturity_date": "2027-06-01", "created_at": "…", ...},
 "overlaps": [{"plan_id": 1, "title": "…", "same_payment_day": true, "overlap_from": "…", "overlap_to": "…"}],
 "overlap_message": "납입 기간이 겹치는 계획이 1개 있습니다(#1 …). 겹치는 기간에는 매달 합계 1,100,000원이 나갑니다. …"}
```

검증 규칙은 이렇습니다. 목표는 0보다 커야 하고, 기간은 1~60개월, 납입일은 1~31일입니다. 어기면 422를 반환합니다.

## 실행 로그 (F3014, F3020)

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | /api/logs | LogSession[] (최근순, 요청 수, 이벤트 수, 마지막 판정) |
| GET | /api/logs/{session_id}?run_id= | LogEvent[] (발생 순서) |
| DELETE | /api/logs/{session_id} | 204 |
| GET | /api/logs/{session_id}/anomalies | AnomalyReport[] (verdict normal/anomaly/undecidable, findings, stats, diagnosis) |

수정 경로(PUT/PATCH)는 없습니다. 호출하면 405를 반환합니다.

## 시스템

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | /api/status | 키 설정 여부(값은 보내지 않음), 연습 모드, LLM 종류, 캘린더 구현체 |
| GET | /api/tools | 등록된 Tool 명세 (이름, 설명, 입력 스키마, 구분, 출처) |
| GET | /api/calendar | 등록된 납입·만기 일정, 매달 나가는 납입 합계 |

## 오류 형식
- 4xx: `{"detail": "그런 계획이 없습니다."}`
- 5xx: `{"detail": "서버에서 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."}` (내부 오류 상세는 노출하지 않음)
