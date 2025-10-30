import os
import json
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI
import logging

logger = logging.getLogger("uvicorn.error")

# ---- ENV ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

app = FastAPI()


def kakao_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }


@app.get("/health")
async def health():
    return {"ok": True}


# 🔎 환경변수/상태 확인용 (개발 디버그용)
@app.get("/debug/env")
async def debug_env():
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "max_tokens": MAX_TOKENS,
        "timeout": OPENAI_TIMEOUT
    }


# 🔎 OpenAI 호출 자체를 점검하는 간단 테스트 (개발 디버그용)
@app.get("/debug/chat")
async def debug_chat(q: str = "안녕! 부산시 AI야?"):
    if not client:
        return JSONResponse(
            {"error": "OPENAI_API_KEY is not set on server"},
            status_code=500
        )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "You are Busan City public service assistant. "
                            "Answer concisely in the same language as the question. "
                            "No markdown, plain text."},
                {"role": "user", "content": q}
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        return {"ok": True, "answer": resp.choices[0].message.content.strip()}
    except Exception as e:
        logger.exception(f"[debug_chat] OpenAI error: {e}")
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500
        )


@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 1) 요청 파싱
    try:
        body = await request.json()
    except Exception:
        body = {}

    utter = ""
    try:
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    # 2) OpenAI 키 확인
    if not client:
        # 개발 중 문제 파악 위해 임시로 원인 노출 (운영 전엔 일반 문구로 교체해도 됨)
        text = "서버 설정 오류: OPENAI_API_KEY가 설정되지 않았습니다."
        return Response(content=json.dumps(kakao_text(text), ensure_ascii=False),
                        media_type="application/json")

    # 3) OpenAI 호출
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "You are Busan City public service assistant. "
                            "Answer concisely in the same language as the user's message. "
                            "No markdown, plain text."},
                {"role": "user", "content": (utter or "안녕하세요")}
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        answer = resp.choices[0].message.content.strip()
        payload = kakao_text(answer)
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        media_type="application/json")
    except Exception as e:
        # 4) 에러는 로그로 남기고, 사용자에겐 기본 문구
        logger.exception(f"[kakao/skill] OpenAI error: {e}")
        fallback = "죄송합니다. 잠시 후 다시 시도해 주세요."
        return Response(content=json.dumps(kakao_text(fallback), ensure_ascii=False),
                        media_type="application/json")
