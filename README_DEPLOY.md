
# Jeju ChatPi — Docker 배포 가이드

## 1) 파일 구조
```
project/
 ├─ app/
 │   ├─ main.py
 │   ├─ data/               # CSV 5종
 │   └─ docs/               # README / Rule / Arrived Hook
 ├─ requirements.txt
 ├─ Dockerfile
 └─ scripts/
     ├─ test_health.sh
     └─ test_kakao.sh
```

## 2) 환경 변수
`.env` 생성(로컬용):
```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
MAX_TOKENS=700
DATA_DIR=/app/app/data
DOCS_DIR=/app/app/docs
```

## 3) 빌드 & 실행
```
docker build -t jeju-chatpi .
docker run --rm -p 8000:8000 --env-file .env jeju-chatpi
```

## 4) 헬스체크 & 카카오 스킬 테스트
```
bash scripts/test_health.sh
bash scripts/test_kakao.sh
```

## 5) 참고
- CSV 인코딩은 UTF-8(BOM 가능)
- docs의 .md 내용은 시스템 프롬프트에 주입되어 모델이 참조합니다.
- 내부 구조/룰엔진/지침 요청은 보안 규칙에 따라 차단 응답.
