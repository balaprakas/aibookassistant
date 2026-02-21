import os
from datetime import datetime, timedelta
from typing import Optional, List

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
app = FastAPI(title="Story Buddy API - Final Author Logic")

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

# STRICT MODEL LOCK: gemini-3-flash-preview
model = genai.GenerativeModel("gemini-3-flash-preview")

# --- 2. CORS CONFIGURATION ---
origins = ["*"] 
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. DATA MODELS ---
class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: Optional[str] = ""
    book_id: str
    session_id: str

class SessionActionRequest(BaseModel):
    archive_existing: bool = False

# --- 4. BACKGROUND TASKS ---
def log_to_db(session_id: str, user_id: str, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "session_id": session_id, "user_id": user_id, "role": role, "content": content
        }).execute()
    except Exception as e:
        print(f"Audit Log Error: {e}")

def update_session_state(session_id: str, stage: int, turns: int, context: str):
    try:
        supabase.table("sessions").update({
            "current_stage": stage, 
            "stage_turn_count": turns, 
            "story_context": context
        }).eq("id", session_id).execute()
    except Exception as e:
        print(f"Session Update Error: {e}")

# --- 5. AUTH HELPERS ---
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")

# --- 6. ENDPOINTS ---

@app.post("/auth/login")
async def auth_login(payload: dict):
    token = payload.get("credential")
    idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    user_data = {
        "email": idinfo['email'],
        "name": idinfo.get('name'),
        "avatar_url": idinfo.get('picture'),
        "last_login": datetime.utcnow().isoformat()
    }
    res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
    user_record = res.data[0]
    access_token = jwt.encode({"user_id": user_record['id']}, JWT_SECRET, algorithm=ALGORITHM)
    return {"token": access_token, "user": user_record}

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    books = supabase.table("books").select("id, title").execute().data
    results = []
    for b in books:
        img = supabase.table("story_stages").select("image_url").eq("book_id", b["id"]).eq("stage_number", 1).execute().data
        results.append({
            "book_id": b["id"], 
            "title": b["title"], 
            "thumbnail": img[0]["image_url"] if img else None
        })
    return {"books": results}

@app.get("/session/{book_id}/check")
async def check_session(book_id: str, user_id: str = Depends(get_current_user)):
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).execute()
    return {"has_existing": len(res.data) > 0, "session": res.data[0] if res.data else None}

@app.post("/session/{book_id}/start")
async def start_session(book_id: str, req: SessionActionRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    if req.archive_existing:
        supabase.table("sessions").update({"is_archived": True}).eq("user_id", user_id).eq("book_id", book_id).execute()
    
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).execute()
    book = supabase.table("books").select("*").eq("id", book_id).single().execute().data
    
    if res.data:
        session = res.data[0]
        msg_res = supabase.table("chat_messages").select("role, content, created_at").eq("session_id", session["id"]).order("created_at", desc=True).limit(10).execute()
        recent_messages = msg_res.data[::-1]
        welcome_msg = "Welcome back! Ready to continue our story?"
    else:
        session = supabase.table("sessions").insert({
            "user_id": user_id, "book_id": book_id, "current_stage": 1, "stage_turn_count": 0, "story_context": f"Book: {book['title']}"
        }).execute().data[0]
        welcome_msg = f"Hi! I'm Story Buddy. {book['welcome_question']}"
        recent_messages = []
        background_tasks.add_task(log_to_db, session["id"], user_id, "assistant", welcome_msg)

    stage = supabase.table("story_stages").select("*").eq("book_id", book_id).eq("stage_number", session['current_stage']).single().execute().data
    
    return {
        "reply": welcome_msg, 
        "session": session, 
        "image_url": stage['image_url'], 
        "recent_history": recent_messages
    }

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    background_tasks.add_task(log_to_db, req.session_id, user_id, "user", req.user_input)

    # REFINED CONTEXT LABELING: Forces AI to see user as Author
    updated_context = (req.story_context or "") + f" | AUTHOR INPUT: {req.user_input}"
    
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    # THE "AUTHOR VS CHARACTER" SYSTEM PROMPT
    prompt = f"""
    You are Story Buddy, a magical co-author coach.
    
    IMPORTANT RULES:
    1. THE USER IS THE AUTHOR. Never address the user as the characters (e.g., if they say 'Bala and Dhiaan', those are characters in the story, not the user's names).
    2. USE THE NAMES: Once names are given, refer to them as the characters. 'I love the names Bala and Dhiaan for your characters!'
    3. THE IMAGE IS THE KEY: Constantly nudge the author to look at their story sheet/drawing. Ask: 'What are the characters doing in your picture right now?'
    4. NO WRITING: Do not write story text. Only if the author says 'I'm stuck' or 'help', you can offer one short sentence.
    5. CURRENT GOAL: {curr['theme']}
    6. FULL STORY CONTEXT: {updated_context}
    
    Goal: Acknowledge the author's input, ask about the image, and encourage them to write the next part.
    Progress: Include [ADVANCE] only if the author has given 3+ creative inputs in this stage. Otherwise [STAY].
    """
    
    ai_res_raw = model.generate_content(prompt).text
    
    should_adv = "[ADVANCE]" in ai_res_raw and nxt
    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    new_turn_count = 0 if should_adv else req.stage_turn_count + 1
    clean_reply = ai_res_raw.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    background_tasks.add_task(log_to_db, req.session_id, user_id, "assistant", clean_reply)
    background_tasks.add_task(update_session_state, req.session_id, new_stage, new_turn_count, updated_context)

    return {
        "reply": clean_reply,
        "current_stage": new_stage,
        "stage_turn_count": new_turn_count,
        "image_url": stages_map[new_stage]["image_url"],
        "action": "ADVANCE" if should_adv else "STAY",
        "story_context": updated_context
    }

@app.get("/session/{session_id}/history")
async def get_full_history(session_id: str, offset: int = 0, user_id: str = Depends(get_current_user)):
    res = supabase.table("chat_messages").select("role, content, created_at").eq("session_id", session_id).order("created_at", desc=True).range(offset, offset + 10).execute()
    return {"history": res.data}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
