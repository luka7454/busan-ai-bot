# 제주도 여행플래너 챗피 v1.0 — 운영 패키지
생성일: 2025-10-30

## 포함 파일
- jeju_arrival_hubs.csv — 도착 허브(공항/도심/리조트) 좌표/반경
- jeju_hotel_halftime_courses.csv — 5성급 호텔 근거리 반나절 코스 템플릿(샘플 ≥20)
- jeju_access_blacklist.csv — 자연휴식년제/기상/공사 등 접근 제한 규칙
- jeju_congestion_rules.csv — 혼잡도 힌트 규칙(월/요일/시간대/주차/이벤트)
- jeju_arrived_mode_prompt_hook.md — 도착자 모드 프롬프트 훅
- jeju_rule_engine_spec.md — 룰엔진 결합 사양
- jeju_sample_halfday_courses.csv — 샘플 반나절 코스 20개

## 적용 순서
1) CSV 5종을 커스텀 GPT '지식'에 업로드
2) 지침에 '도착자 모드 훅' 추가
3) 룰엔진 사양대로 우선순위 결합(Blacklist ▶ Weather ▶ Congestion ▶ Hotel ▶ Generic)
4) 시나리오 테스트 8건 수행 후 운영 대시보드 연결

## 주기적 운영
- 매주: last_verified 점검·source_url 보완
- 이벤트/공사/휴식년제 변경 시 즉시 블랙리스트 갱신
