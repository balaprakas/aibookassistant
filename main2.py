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
app = FastAPI(title="Story Buddy API - Robust Login")

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

# --- 4. AUTH HELPERS ---

def create_access_token(user_id: str):
    expire = datetime.now(timezone.utc) + timedelta(minutes=60)
    to_encode = {"user_id": str(user_id), "exp": expire, "type": "access"}
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

async def get_current_user(authorization: str = Header(None)):
    if not authorization or "undefined" in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized: No token provided")
    
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except JWTError:
        raise HTTPException(status_code=401, detail="Unauthorized: Session expired")

# --- 5. ENDPOINTS ---

@app.post("/auth/login")
async def auth_login(payload: dict = Body(...)):
    credential = payload.get("credential")
    if not credential:
        raise HTTPException(status_code=400, detail="Missing Google credential")
        
    try:
        # 1. Verify Google Token
        idinfo = id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo['email']
        
        user_data = {
            "email": email,
            "name": idinfo.get('name'),
            "avatar_url": idinfo.get('picture'),
            "last_login": datetime.now(timezone.utc).isoformat()
        }
        
        # 2. Upsert to Supabase
        # We use select() after upsert to ensure data is returned
        res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
        
        # 3. Fallback: If upsert didn't return data, fetch it manually
        if not res.data:
            res = supabase.table("users").select("*").eq("email", email).execute()
        
        if not res.data:
            raise Exception("Failed to create or retrieve user record")

        user_record = res.data[0]
        user_id = str(user_record['id'])
        
        # 4. Generate Token
        access_token = create_access_token(user_id)
        
        # 5. EXPLICIT RETURN (Ensuring non-empty response)
        response_data = {
            "access_token": access_token,
            "user": user_record,
            "status": "success"
        }
        print(f"DEBUG: Returning login for {email}")
        return response_data

    except Exception as e:
        print(f"CRITICAL LOGIN ERROR: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Login failed: {str(e)}")

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    books = supabase.table("books").select("id, title").execute().data
    return {"books": books or []}

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, user_id: str = Depends(get_current_user)):
    updated_context = (req.story_context or "") + f" | AUTHOR INPUT: {req.user_input}"
    
    stages_res = supabase.table("story_stages").select("*").eq("book_id", req.book_id).order("stage_number").execute()
    stages_map = {s['stage_number']: s for s in (stages_res.data or [])}
    
    curr = stages_map.get(req.current_stage)
    if not curr:
        raise HTTPException(status_code=404, detail="Stage not found")
        
    prompt = f"""
    You are Story Buddy (gemini-3-flash-preview). 
    The user is the AUTHOR. 
    Context: {updated_context}
    Stage Theme: {curr['theme']}
    
    Rules:
    - Never call the user by character names.
    - Ask what is in their picture.
    - End with [STAY] or [ADVANCE].
    """
    
    ai_res = model.generate_content(prompt).text
    should_adv = "[ADVANCE]" in ai_res
    clean_reply = ai_res.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

    return {
        "reply": clean_reply,
        "current_stage": req.current_stage + 1 if should_adv else req.current_stage,
        "action": "ADVANCE" if should_adv else "STAY",
        "story_context": updated_context
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
