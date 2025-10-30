import os, json
from fastapi import FastAPI, Request
from fastapi.responses import Response
from langdetect import detect, LangDetectException
from openai import OpenAI

# ---- Config ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "15"))

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# ---- Utils ----
def detect_lang(text: str) -> str:
    try:
        if not text or not text.strip():
            return "ko"
        return detect(text)
    except LangDetectException:
        return "ko"

def kakao_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }

# ---- Health ----
@app.get("/health")
async def health():
    return {"ok": True}

# ---- Kakao Skill ----
@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 1) 요청 파싱 방어
    try:
        body = await request.json()
    except Exception:
        body = {}

    utter = ""
    try:
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    # 2) 언어 감지 (다국어 대응)
    user_lang = detect_lang(utter)

    # 3) 시스템 프롬프트: "사용자 언어로 답하라"
    system_prompt = (
        "You are Busan City public service assistant. "
        "Answer concisely in the user's language. "
        "If the user greets, greet back briefly. "
        "Format plain text (no markdown)."
    )

    # 4) OpenAI 호출 (타임아웃/토큰 방어)
    answer = "안녕하세요! 무엇을 도와드릴까요?"
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": utter or "안녕하세요"}
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.3,
            timeout=OPENAI_TIMEOUT,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as e:
        # 장애 시도 기본 응답 (서비스 지속성 확보)
        if user_lang.startswith("ko"):
            answer = "죄송합니다. 잠시 후 다시 시도해 주세요."
        else:
            answer = "Sorry, please try again in a moment."

    # 5) 카카오 스키마로 반환
    payload = kakao_text(answer)
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
        status_code=200
    )
