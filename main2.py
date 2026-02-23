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
from supabase import create_client, Clientimport os
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
    return {"access_token": access_token, "user": user_record, "status": "success"}

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

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user)):
    background_tasks.add_task(log_to_db, req.session_id, user_id, "user", req.user_input)

    updated_context = (req.story_context or "") + f" | Child: {req.user_input}"
    stages = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute().data
    stages_map = {s['stage_number']: s for s in stages}
    
    curr = stages_map[req.current_stage]
    nxt = stages_map.get(req.current_stage + 1)
    
    # SYSTEM PROMPT: Enforce conversation turns
    system_instruction = f"""
    You are 'Story Buddy', a magical friend for a child author.
    
    CONTEXT: {updated_context}
    CURRENT THEME: {curr['theme']}
    CURRENT TURN: {req.stage_turn_count}
    
    RULES:
    1. BRAINSTORMING (Turns 0-2): Be very curious! Ask about character names, what they are wearing, where they are, or what they are doing in the image. 
       - NEVER ask the child to "write" in the template during these turns.
       - Always include [STAY].
    2. WRITING CHECK (Turns 3+): After enough brainstorming, say: "Tell me when you have written this part in your template!"
       - Only include [ADVANCE] if the child confirms they wrote it (e.g. "done", "yes", "i wrote it").
       - If they say "no", do a friendly nudge and include [STAY].
    3. Use simple, excited English. 2 short sentences max. No mention of "drawings".
    """

    messages = [{"role": "user", "parts": [system_instruction]}]
    clean_history = [msg for msg in req.history if msg["content"] != req.user_input]
    for msg in clean_history:
        role = "model" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [msg["content"]]})
    messages.append({"role": "user", "parts": [req.user_input]})
    
    ai_res_raw = model.generate_content(messages).text
    
    should_adv = "[ADVANCE]" in ai_res_raw and nxt
    clean_reply = ai_res_raw.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    # If transitioning, fetch the intro for the NEXT stage immediately
    if should_adv:
        next_theme = nxt['theme']
        next_prompt = f"The child finished writing. Now we are on a new scene: '{next_theme}'. Give a 1-sentence cheer, then ask a simple conversational question about what is happening in the new image."
        next_res = model.generate_content(next_prompt).text
        clean_reply = f"Yay! You did it! {next_res.strip()}"

    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    new_turn_count = 0 if should_adv else req.stage_turn_count + 1

    background_tasks.add_task(log_to_db, req.session_id, user_id, "assistant", clean_reply)
    background_tasks.add_task(update_session_state, req.session_id, new_stage, new_turn_count, updated_context)

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
    return {"access_token": access_token, "user": user_record, "status": "success"}

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
    book_data_res = supabase.table("books").select("*").eq("id", book_id).single().execute()
    book = book_data_res.data
    
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

    stage_data_res = supabase.table("story_stages").select("*").eq("book_id", book_id).eq("stage_number", session['current_stage']).single().execute()
    stage = stage_data_res.data
    
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
    
    # PASS 1: Evaluate current stage response
    system_instruction = f"""
    You are 'Story Buddy', a magical, silly, and very kind friend for a child author.
    
    CONTEXT: {updated_context}
    CURRENT THEME: {curr['theme']}
    
    KID-FRIENDLY RULES:
    1. Use very simple English. Be encouraging ("Yay!", "Great job!").
    2. Reference the "images in the book" for the theme: {curr['theme']}.
    3. If brainstorming is done (2+ turns), ask: "Tell me when you have written this part in your template!"
    4. ADVANCING: 
       - Include [ADVANCE] ONLY if the child confirms they finished writing (e.g. "done", "yes", "i wrote it").
       - Otherwise, include [STAY].
    5. Keep response to 2 short sentences.
    """

    messages = [{"role": "user", "parts": [system_instruction]}]
    clean_history = [msg for msg in req.history if msg["content"] != req.user_input]
    for msg in clean_history:
        role = "model" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [msg["content"]]})
    messages.append({"role": "user", "parts": [req.user_input]})
    
    ai_res_raw = model.generate_content(messages).text
    
    should_adv = "[ADVANCE]" in ai_res_raw and nxt
    clean_reply = ai_res_raw.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    # PASS 2: If advancing, get the next stage's question immediately
    if should_adv:
        next_theme = nxt['theme']
        next_prompt = f"The child just finished the previous page. Now we are on a new page with the theme: '{next_theme}'. Give a 1-sentence excited celebration, then ask a simple question about what they see in the new image for this theme."
        next_res = model.generate_content(next_prompt).text
        clean_reply = f"Yay! You did a wonderful job. {next_res.strip()}"

    new_stage = req.current_stage + 1 if should_adv else req.current_stage
    new_turn_count = 0 if should_adv else req.stage_turn_count + 1

    background_tasks.add_task(log_to_db, req.session_id, user_id, "assistant", clean_reply)
    background_tasks.add_task(update_session_state, req.session_id, new_stage, new_turn_count, updated_context)

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

