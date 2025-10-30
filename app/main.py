from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    body = await request.json()
    text = body.get("userRequest", {}).get("utterance", "")
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": f"👋 부산시 AI 응답 테스트: {text}"}}
            ]
        }
    }
