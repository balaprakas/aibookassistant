import os
from datetime import datetime, timedelta
from typing import Optional

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="Story Buddy - Full Session Management")

# --- 1. CONFIGURATION ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

origins = [
    "https://accessible-aili-untoldstories-da4d51c9.lovable.app",
    "http://localhost:5173",
    "https://aistoryassistant.lovable.app",
    "https://a5d02d6e-03c4-413d-9c83-f019e987dcc1.lovableproject.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
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
    session_id: str

class SessionActionRequest(BaseModel):
    archive_existing: bool = False

# --- 3. BACKGROUND TASKS ---
def log_to_db(session_id: str, user_id: str, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "session_id": session_id, "user_id": user_id, "role": role, "content": content
        }).execute()
    except Exception as e:
        print(f"Log Error: {e}")

def update_session_state(session_id: str, stage: int, turns: int, context: str):
    try:
        supabase.table("sessions").update({
            "current_stage": stage, "stage_turn_count": turns, "story_context": context
        }).eq("id", session_id).execute()
    except Exception as e:
        print(f"Update Error: {e}")

# --- 4. AUTH HELPERS ---
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")

# --- 5. ENDPOINTS ---

@app.post("/auth/login")
async def auth_login(payload: dict):
    token = payload.get("credential")
    idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    user_data = {"email": idinfo['email'], "name": idinfo.get('name'), "avatar_url": idinfo.get('picture'), "last_login": datetime.utcnow().isoformat()}
    res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
    user_record = res.data[0]
    access_token = jwt.encode({"user_id": user_record['id']}, JWT_SECRET, algorithm=ALGORITHM)
    return {"token": access_token, "user": user_record}

@app.get("/session/{book_id}/check")
async def check_session(book_id: str, user_id: str = Depends(get_current_user)):
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).execute()
    return {"has_existing": len(res.data) > 0, "session": res.data[0] if res.data else None}

@app.post("/session/{book_id}/start")
async def start_session(book_id: str, req: SessionActionRequest, user_id: str = Depends(get_current_user)):
    if req.archive_existing:
        supabase.table("sessions").update({"is_archived": True}).eq("user_id", user_id).eq("book_id", book_id).execute()
    
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).execute()
    
    recent_messages = []
    
    if res.data:
        session = res.data[0]
        # Fetch last 5 interactions for context and UI
        msg_res = supabase.table("chat_messages")\
            .select("role, content, created_at")\
            .eq("session_id", session["id"])\
            .order("created_at", desc=True)\
            .limit(10)\
            .execute()
        recent_messages = msg_res.data[::-1] # Order chronologically
    else:
        book = supabase.table("books").select("*").eq("id", book_id).single().execute().data
        session = supabase.table("sessions").insert({
            "user_id": user_id, "book_id": book_id, "current_stage": 1, "stage_turn_count": 0, "story_context": f"Book: {book['title']}"
        }).execute().data[0]

    stage = supabase.table("story_stages").select("*").eq("book_id", book_id).eq("stage_number", session['current_stage']).single().execute().data
    
    return {
        "session": session, 
        "image_url": stage['image_url'], 
        "recent_history": recent_messages,
        "is_resume": len(recent_messages) > 0
    }

@app.get("/session/{session_id}/history")
async def get_full_history(session_id: str, limit: int = 20, offset: int = 0, user_id: str = Depends(get_current_user)):
    """Lazy load previous messages."""
    res = supabase.table("chat_messages")\
        .select("role, content, created_at")\
        .eq("session_id", session_id)\
        .order("created_at", desc=True)\
        .range(offset, offset + limit)\
        .execute()
    return {"history": res.data}

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    # 1. Background Audit: User Message
    background_tasks.add_task(log_to_db, req.session_id, user_id, "user", req.user_input)

    updated_context = req.story_context + f" | Child: {req.user_input}"
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    prompt = f"Co-author mode. Context: {updated_context}. Goal: {curr['theme']}. Max 2 sentences. End with [ADVANCE] if turn {req.stage_turn_count + 1} >= 3, else [STAY]."
    ai_res_raw = model.generate_content(prompt).text
    
    should_adv = "[ADVANCE]" in ai_res_raw and nxt
    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    clean_reply = ai_res_raw.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    # 2. Background Audit: AI Response
    background_tasks.add_task(log_to_db, req.session_id, user_id, "assistant", clean_reply)

    # 3. Background State Update
    background_tasks.add_task(update_session_state, req.session_id, new_stage, 0 if should_adv else req.stage_turn_count + 1, updated_context)

    return {
        "reply": clean_reply,
        "current_stage": new_stage,
        "stage_turn_count": 0 if should_adv else req.stage_turn_count + 1,
        "image_url": stages_map[new_stage]["image_url"],
        "action": "ADVANCE" if should_adv else "STAY",
        "story_context": updated_context
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
