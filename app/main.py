from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    # 1) 어떤 형식이 와도 안전하게 파싱
    try:
        body = await request.json()
    except Exception:
        body = {}

    # 2) 발화 텍스트 방어적으로 추출
    utter = ""
    try:
        ur = body.get("userRequest") or {}
        utter = ur.get("utterance") or ""
    except Exception:
        utter = ""

    # 3) 빈 문자열이면 기본값
    if not isinstance(utter, str):
        utter = str(utter)

    # 4) 카카오 스키마로 항상 200 응답
    payload = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": f"👋 부산시 AI 응답 테스트: {utter or '안녕하세요!'}"
                    }
                }
            ]
        }
    }
    return JSONResponse(status_code=200, content=payload)
