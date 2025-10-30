from fastapi import FastAPI, Request
from fastapi.responses import Response
import json

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 어떤 요청이 와도 안전하게 파싱
    try:
        body = await request.json()
    except Exception:
        body = {}

    utter = ""
    try:
        utter = (body.get("userRequest") or {}).get("utterance") or ""
    except Exception:
        utter = ""

    payload = {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": f"👋 부산시 AI 응답 테스트: {utter or '안녕하세요!'}"}}
            ]
        }
    }
    # ensure_ascii=False 로 한글 깨짐 방지 + 명시적 Content-Type
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
        status_code=200
    )
