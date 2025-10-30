import os
import json
import asyncio
import logging
import re
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from openai import OpenAI
import yaml

logger = logging.getLogger("uvicorn.error")

# ==== 환경변수 ====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
MAX_TOKENS     = int(os.getenv("MAX_TOKENS", "512"))
TIMEOUT        = float(os.getenv("TIMEOUT", "4.8"))  # Kakao 5초 제한 고려

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app = FastAPI()

# ==== Kakao UI ====
def kakao_simple(text, quick_replies=None):
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": quick_replies or []
        }
    }

def kakao_list(title, items, quick_replies=None):
    return {
        "version": "2.0",
        "template": {
            "outputs": [{
                "listCard": {
                    "header": {"title": title},
                    "items": items[:5]
                }
            }],
            "quickReplies": quick_replies or []
        }
    }

def lang_buttons(current):
    langs = {"ko": "한국어", "en": "English", "ja": "日本語", "zh": "中文"}
    btns = []
    for code, label in langs.items():
        if code != current:
            btns.append({
                "label": label,
                "action": "message",
                "messageText": f"/lang {code}"
            })
    return btns

# ==== 언어 감지 ====
def detect_lang(text: str):
    if any("\uac00" <= ch <= "\ud7a3" for ch in text):
        return "ko"
    if any("\u3040" <= ch <= "\u30ff" for ch in text):
        return "ja"
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return "zh"
    return "en"

def lang_prompt(lang: str):
    prompts = {
        "ko": "당신은 부산 지역 AI 어시스턴트입니다. 반드시 한국어로만 답하세요.",
        "en": "You are a friendly Busan local assistant. Answer strictly in English.",
        "ja": "あなたは釜山ローカルAIアシスタントです。必ず日本語で回答してください。",
        "zh": "你是釜山本地助手。请务必使用中文回答。"
    }
    return prompts.get(lang, prompts["en"])

# ==== Busan 데이터 ====
BUSAN_DATA_PATH = "data/busan.yaml"
if os.path.exists(BUSAN_DATA_PATH):
    with open(BUSAN_DATA_PATH, "r", encoding="utf-8") as f:
        BUSAN = yaml.safe_load(f)
else:
    BUSAN = {"spots": [], "foods": [], "hotels": []}

def search_spots(q: str):
    ql = q.lower()
    results = []
    for s in BUSAN.get("spots", []):
        text = " ".join([
            s.get("name_ko", ""), s.get("name_en", ""),
            s.get("area", ""), " ".join(s.get("tags", []))
        ]).lower()
        if any(k in text for k in ql.split()):
            results.append(s)
    return results[:5]

def get_marysol():
    for h in BUSAN.get("hotels", []):
        if h.get("id") == "marysol_haeundae":
            return h
    return None

# ==== Intent 분류 ====
def classify_intent(text: str):
    t = text.lower()
    if t.startswith("/lang "):
        return "lang"
    if any(k in t for k in ["faq", "문의", "가격", "환불", "시간"]):
        return "faq"
    if any(k in t for k in ["해운대", "광안리", "태종대", "부산타워", "감천"]):
        return "tour"
    if any(k in t for k in ["맛집", "돼지국밥", "밀면", "회", "카페"]):
        return "food"
    if any(k in t for k in ["지하철", "버스", "환승", "막차"]):
        return "transit"
    if any(k in t for k in ["메리솔", "marysol", "호텔", "숙박", "체크인", "체크아웃"]):
        return "hotel"
    if re.search(r"안녕|hello|hi|ㅎㅇ|하이", t):
        return "smalltalk"
    return "default"

# ==== Skill 핸들러 ====
async def handle_tour(text, lang):
    spots = search_spots(text)
    if not spots:
        msg = "부산 명소를 찾지 못했어요. 해운대/광안리/태종대 등으로 물어보세요." if lang=="ko" \
            else "Couldn't find Busan landmarks. Try Haeundae or Gwangalli."
        return kakao_simple(msg, lang_buttons(lang))
    items = []
    for s in spots:
        title = s["name_ko"] if lang=="ko" else s["name_en"]
        desc = s["desc_ko"] if lang=="ko" else s["desc_en"]
        items.append({"title": title, "description": desc or ""})
    return kakao_list("부산 명소" if lang=="ko" else "Busan Spots", items, lang_buttons(lang))

async def handle_hotel(lang):
    h = get_marysol()
    if not h:
        return kakao_simple("호텔 정보를 찾지 못했어요." if lang=="ko" else "Hotel info not found.")
    perks = h["perks_ko"] if lang=="ko" else h["perks_en"]
    msg = f"{h['name_ko']} 특전: " + ", ".join(perks) if lang=="ko" else f"{h['name_en']} perks: " + ", ".join(perks)
    return kakao_simple(msg, lang_buttons(lang))

async def handle_faq(lang):
    msg = "자주 묻는 질문: 체크인 15:00 / 체크아웃 11:00 / 주차 가능(사전 문의)." if lang=="ko" \
        else "FAQ: Check-in 15:00 / Check-out 11:00 / Parking available (ask ahead)."
    return kakao_simple(msg, lang_buttons(lang))

# ==== 헬스/디버그 ====
@app.get("/health")
async def health():
    return {"ok": True, "model": OPENAI_MODEL, "has_key": bool(OPENAI_API_KEY)}

@app.get("/debug/env")
async def debug_env():
    return {
        "openai": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "max_tokens": MAX_TOKENS,
        "timeout": TIMEOUT,
    }

@app.get("/debug/chat")
async def debug_chat(q: str = "안녕! 부산시 AI야?"):
    if not client:
        return {"error": "OPENAI_API_KEY not set"}
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are Busan City public service assistant."},
                {"role": "user", "content": q},
            ],
            max_tokens=MAX_TOKENS,
        )
        return {"ok": True, "answer": resp.choices[0].message.content.strip()}
    except Exception as e:
        logger.exception(f"[debug_chat] {e}")
        return {"ok": False, "error": str(e)}

# ==== 핵심: Kakao Skill ====
@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    try:
        body = await request.json()
        utter = (body.get("userRequest") or {}).get("utterance", "")
        user_id = (body.get("userRequest", {}).get("user", {}) or {}).get("id", "anon")
    except Exception:
        utter, user_id = "", "anon"

    if not client:
        text = "서버 설정 오류: OPENAI_API_KEY 미설정"
        return Response(content=json.dumps(kakao_simple(text), ensure_ascii=False),
                        media_type="application/json")

    lang = detect_lang(utter)
    header = lang_prompt(lang)
    intent = classify_intent(utter)

    async def _process():
        if intent == "lang":
            code = utter.strip().split()[-1][:2]
            text = f"언어를 변경했습니다: {code}" if code != "en" else f"Language changed to {code}"
            return kakao_simple(text, lang_buttons(code))
        elif intent == "tour":
            return await handle_tour(utter, lang)
        elif intent == "hotel":
            return await handle_hotel(lang)
        elif intent == "faq":
            return await handle_faq(lang)
        else:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": header},
                    {"role": "user", "content": utter or "안녕하세요"}
                ],
                max_tokens=MAX_TOKENS,
                temperature=0.3
            )
            answer = resp.choices[0].message.content.strip()
            return kakao_simple(answer, lang_buttons(lang))

    try:
        payload = await asyncio.wait_for(_process(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        payload = kakao_simple("응답이 지연되고 있어요. 잠시 후 다시 시도해주세요.", lang_buttons(lang))
    except Exception as e:
        logger.exception(f"[kakao/skill] {e}")
        payload = kakao_simple("죄송합니다. 오류가 발생했습니다.", lang_buttons(lang))

    return Response(content=json.dumps(payload, ensure_ascii=False),
                    media_type="application/json")
