import os
from datetime import datetime, timedelta
from typing import Optional

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# --- 1. CONFIGURATION ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(os.getenv("MODEL_NAME", "gemini-3-flash-preview"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. MODELS ---
class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: str
    book_id: str
    session_id: Optional[str] = None

# --- 3. AUTH HELPERS ---
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id: raise HTTPException(status_code=401)
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")

# --- 4. ENDPOINTS ---

@app.post("/auth/login")
async def auth_login(payload: dict):
    token = payload.get("credential")
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        user_data = {
            "email": idinfo['email'],
            "name": idinfo.get('name'),
            "avatar_url": idinfo.get('picture'),
            "last_login": datetime.utcnow().isoformat()
        }
        res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
        user_record = res.data[0]
        access_token = create_access_token({"user_id": user_record['id']})
        return {"token": access_token, "user": user_record}
    except Exception:
        raise HTTPException(status_code=401, detail="Google Auth Failed")

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    books = supabase.table("books").select("id, title").execute().data
    results = []
    for b in books:
        img = supabase.table("story_stages").select("image_url").eq("book_id", b["id"]).eq("stage_number", 1).single().execute().data
        results.append({"book_id": b["id"], "title": b["title"], "thumbnail": img["image_url"] if img else None})
    return {"books": results}

@app.get("/session/{book_id}")
async def get_session(book_id: str, user_id: str = Depends(get_current_user)):
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).order("created_at", desc=True).limit(1).execute()
    if res.data:
        return {"has_session": True, "session": res.data[0]}
    return {"has_session": False}

@app.post("/session/{book_id}/start")
async def start_session(book_id: str, payload: dict, user_id: str = Depends(get_current_user)):
    if payload.get("archive_existing"):
        supabase.table("sessions").update({"is_archived": True}).eq("user_id", user_id).eq("book_id", book_id).execute()
    
    book = supabase.table("books").select("*").eq("id", book_id).single().execute().data
    stages = supabase.table("story_stages").select("*").eq("book_id", book_id).order("stage_number").execute().data
    
    session_data = {
        "user_id": user_id,
        "book_id": book_id,
        "current_stage": 1,
        "stage_turn_count": 0,
        "story_context": f"Book: {book['title']}"
    }
    new_session = supabase.table("sessions").insert(session_data).execute().data[0]
    
    return {
        "reply": f"Hi! I'm Story Buddy. {book['welcome_question']}",
        "session_id": new_session["id"],
        "current_stage": 1,
        "total_stages": len(stages),
        "image_url": stages[0]["image_url"]
    }

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, user_id: str = Depends(get_current_user)):
    updated_context = req.story_context + f" | Child: {req.user_input}"
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    nudge = f"Theme: {curr['theme']}. " + (f"Next: {nxt['theme']}" if nxt else "Final goodbye.")
    prompt = f"Co-author mode. Context: {updated_context}. Goal: {nudge}. Max 2 sentences. End with [ADVANCE] if turn 3, else [STAY]."
    
    ai_res = model.generate_content(prompt).text
    should_adv = "[ADVANCE]" in ai_res and nxt
    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    
    # Persist to DB
    if req.session_id:
        supabase.table("sessions").update({
            "current_stage": new_stage,
            "stage_turn_count": 0 if should_adv else req.stage_turn_count + 1,
            "story_context": updated_context
        }).eq("id", req.session_id).execute()

    return {
        "reply": ai_res.replace("[ADVANCE]", "").replace("[STAY]", "").strip(),
        "current_stage": new_stage,
        "action": "ADVANCE" if should_adv else ("FINISH" if not nxt and "[ADVANCE]" in ai_res else "STAY"),
        "image_url": stages_map[new_stage]["image_url"]
    }
