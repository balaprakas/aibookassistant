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
app = FastAPI(title="Story Buddy API - Author Coach Mode")

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
origins = [
    "https://accessible-aili-untoldstories-da4d51c9.lovable.app",
    "http://localhost:5173",
    "https://aistoryassistant.lovable.app",
    "https://a5d02d6e-03c4-413d-9c83-f019e987dcc1.lovableproject.com",
    "https://id-preview--a5d02d6e-03c4-413d-9c83-f019e987dcc1.lovable.app"
]

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

    # Use 'AUTHOR INPUT' label to prevent user/character confusion
    updated_context = (req.story_context or "") + f" | AUTHOR INPUT: {req.user_input}"
    
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    prompt = f"""
    You are Story Buddy, a magical co-author coach for a child author.
    
    FULL STORY CONTEXT: {updated_context}
    CURRENT STAGE THEME: {curr['theme']}
    NEXT STAGE THEME: {nxt['theme'] if nxt else 'The End'}
    
    STRICT OPERATING RULES:
    1. THE USER IS THE AUTHOR: Never address the author by character names (like Bala or Dhiaan). 
    2. NAMES ARE DYNAMIC: Identify the names provided for the characters and use them to talk ABOUT the story.
    3. IMAGE NUDGING: Your goal is to get the author to look at their drawing/story sheet. Ask: "Looking at your picture, what are [Characters] doing?"
    4. NO AUTO-WRITING: Do not write the story for them. If they are stuck, give ONE tiny suggestion.
    5. PROGRESS: If the author has provided enough detail for this stage (usually 3 turns), include [ADVANCE]. Otherwise, [STAY].
    
    DIRECTIONS:
    - Keep it to 2-3 enchanting sentences.
    - End your reply with either [STAY] or [ADVANCE].
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
