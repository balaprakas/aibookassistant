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
app = FastAPI(title="Story Buddy API - Character Identification Fix")

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
origins = ["*"] # Adjust for production security
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

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    background_tasks.add_task(log_to_db, req.session_id, user_id, "user", req.user_input)

    # We append the child's input specifically as character details to the context
    updated_context = (req.story_context or "") + f" | Child specified character names/details: {req.user_input}"
    
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    # REVISED PROMPT: Prevents "Hello [Character Name]" error
    prompt = f"""
    You are Story Buddy, a creative coach helping a child write a book.
    
    HISTORY AND CONTEXT:
    {updated_context}
    
    GOAL FOR THIS PART OF THE STORY:
    {curr['theme']}
    
    INSTRUCTIONS:
    1. NEVER address the child by the names they provide. Those are the NAMES OF THE CHARACTERS. 
    2. If the child provided names (like "Bala and Dhiaan"), acknowledge them as characters: e.g., "I love the names you chose for the boy and the chameleon!"
    3. NUDGE: Encourage the child to look at their story sheet/image. Ask them what the characters are doing in the picture.
    4. NO AUTO-WRITING: Do not write the story for them. Wait for the child to describe what happens.
    5. IDENTIFICATION: If the child just gave names, confirm who is who. For example: "Which one is the boy, and which one is the chameleon?"
    6. PROGRESS: If turn count {req.stage_turn_count + 1} >= 3, include [ADVANCE]. Otherwise, include [STAY].
    
    Tone: Magical, encouraging, and strictly a coach/assistant.
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

# Remaining endpoints (/books, /session/start, etc.) should remain as per your latest working version.
