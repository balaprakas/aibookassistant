import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks, Body
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="Story Buddy API - Final Sync")

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
    history: List[dict] = [] 

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
    id_info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    
    user_data = {
        "email": id_info['email'],
        "name": id_info.get('name'),
        "avatar_url": id_info.get('picture'),
        "last_login": datetime.now(timezone.utc).isoformat()
    }
    
    res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
    user_record = res.data[0]
    
    access_token = jwt.encode({"user_id": user_record['id']}, JWT_SECRET, algorithm=ALGORITHM)
    
    return {
        "access_token": access_token,
        "user": user_record,
        "status": "success"
    }

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    books_res = supabase.table("books").select("id, title").execute()
    all_books_data = books_res.data or []
    results = []
    for book in all_books_data:
        img_res = supabase.table("story_stages").select("image_url").eq("book_id", book["id"]).eq("stage_number", 1).execute()
        results.append({
            "book_id": book["id"], 
            "title": book["title"], 
            "thumbnail": img_res.data[0]["image_url"] if img_res.data else None
        })
    return {"total_books": len(results), "books": results}

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
        "image_url": stage['image_url'] if stage else None, 
        "recent_history": recent_messages
    }

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    background_tasks.add_task(log_to_db, req.session_id, user_id, "user", req.user_input)

    updated_context = (req.story_context or "") + f" | Child: {req.user_input}"
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    system_instruction = f"""
    You are Story Buddy, a magical creative coach for a child author. 
    USER ROLE: The user is the AUTHOR.
    Context: {updated_context}
    Stage Goal: {curr['theme']}
    
    STRICT RULES:
    1. NEVER repeat a greeting (like "Hi" or "Hello") if you see one in the chat history.
    2. The author just gave you names or details. Acknowledge them immediately (e.g., "Those are great names for the boy and his chameleon!")
    3. Ask the author about their drawing—what is happening in the picture for this stage?
    4. Keep it to 2-3 sentences.
    5. If names are provided and the stage theme is addressed, include [ADVANCE]. Otherwise, include [STAY].
    """

    messages = [{"role": "user", "parts": [system_instruction]}]
    
    # Filter history to avoid duplicates if the frontend sends the current turn in history
    clean_history = [msg for msg in req.history if msg["content"] != req.user_input]
    
    for msg in clean_history:
        role = "model" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [msg["content"]]})
        
    # Append the actual current turn
    messages.append({"role": "user", "parts": [req.user_input]})
    
    ai_res_raw = model.generate_content(messages).text
    
    should_adv = "[ADVANCE]" in ai_res_raw and nxt
    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    new_turn_count = 0 if should_adv else req.stage_turn_count + 1
    clean_reply = ai_res_raw.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    background_tasks.add_task(log_to_db, req.session_id, user_id, "assistant", clean_reply)
    background_tasks.add_task(update_session_state, req.session_id, new_stage, new_turn_count, updated_context)

    # Safety check for image URL
    final_image = stages_map.get(new_stage, {}).get("image_url", curr["image_url"])

    return {
        "reply": clean_reply,
        "current_stage": new_stage,
        "stage_turn_count": new_turn_count,
        "image_url": final_image,
        "action": "ADVANCE" if should_adv else "STAY",
        "story_context": updated_context
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
