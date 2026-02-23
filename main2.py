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
app = FastAPI(title="Story Buddy API - Format Sync")

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

# --- 3. AUTH HELPERS ---
def create_access_token(user_id: str):
    expire = datetime.now(timezone.utc) + timedelta(minutes=60)
    return jwt.encode({"user_id": str(user_id), "exp": expire, "type": "access"}, JWT_SECRET, algorithm=ALGORITHM)

async def get_current_user(authorization: str = Header(None)):
    if not authorization or "undefined" in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized: No token")
    try:
        token = authorization.split(" ")[1]
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except:
        raise HTTPException(status_code=401, detail="Session expired")

# --- 4. ENDPOINTS ---

@app.post("/auth/login")
async def auth_login(payload: dict = Body(...)):
    credential = payload.get("credential")
    idinfo = id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    
    user_data = {
        "email": idinfo['email'],
        "name": idinfo.get('name'),
        "avatar_url": idinfo.get('picture'),
        "last_login": datetime.now(timezone.utc).isoformat()
    }
    
    res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
    user_record = res.data[0] if res.data else supabase.table("users").select("*").eq("email", idinfo['email']).execute().data[0]
    
    token = create_access_token(user_record['id'])
    return {"access_token": token, "user": user_record, "status": "success"}

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    """
    Returns books in the specific format:
    { "total_books": X, "books": [...] }
    """
    # 1. Fetch all books
    books_res = supabase.table("books").select("id, title").execute()
    all_books_data = books_res.data or []
    
    results = []
    for book in all_books_data:
        # 2. Fetch the image for stage_number 1 (The Thumbnail)
        img_res = supabase.table("story_stages") \
            .select("image_url") \
            .eq("book_id", book["id"]) \
            .eq("stage_number", 1) \
            .execute()
        
        thumbnail_url = img_res.data[0]["image_url"] if img_res.data else None
        
        results.append({
            "book_id": book["id"], 
            "title": book["title"], 
            "thumbnail": thumbnail_url
        })

    # 3. Construct the response according to the requested format
    return {
        "total_books": len(results),
        "books": results
    }

@app.get("/session/{book_id}/check")
async def check_session(book_id: str, user_id: str = Depends(get_current_user)):
    if book_id == "undefined":
        raise HTTPException(status_code=400, detail="Invalid Book ID")
        
    res = supabase.table("sessions").select("*").eq("user_id", user_id).eq("book_id", book_id).eq("is_archived", False).execute()
    return {"has_existing": len(res.data) > 0, "session": res.data[0] if res.data else None}

@app.post("/chat")
async def chat(req: dict = Body(...), user_id: str = Depends(get_current_user)):
    user_input = req.get("user_input")
    context = (req.get("story_context") or "") + f" | AUTHOR INPUT: {user_input}"
    
    prompt = f"""
    You are Story Buddy (gemini-3-flash-preview). 
    User is the Author. 
    Context: {context}
    Rules: Never address them by character names. Nudge them to describe their drawing.
    End with [STAY] or [ADVANCE].
    """
    
    ai_res = model.generate_content(prompt).text
    return {
        "reply": ai_res.replace("[ADVANCE]", "").replace("[STAY]", "").strip(),
        "action": "ADVANCE" if "[ADVANCE]" in ai_res else "STAY",
        "story_context": context
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
