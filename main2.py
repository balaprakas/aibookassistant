import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Header, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="Story Buddy API - Context Aware")

# --- CONFIG ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
JWT_SECRET = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3-flash-preview")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- AUTH ---
async def get_current_user(authorization: str = Header(None)):
    if not authorization or "undefined" in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        token = authorization.split(" ")[1]
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except:
        raise HTTPException(status_code=401, detail="Session expired")

# --- ENDPOINTS ---

@app.get("/books")
async def get_books(user_id: str = Depends(get_current_user)):
    books = supabase.table("books").select("id, title").execute().data
    results = []
    for b in books:
        img = supabase.table("story_stages").select("image_url").eq("book_id", b["id"]).eq("stage_number", 1).execute().data
        results.append({"book_id": b["id"], "title": b["title"], "thumbnail": img[0]["image_url"] if img else None})
    return {"total_books": len(results), "books": results}

@app.post("/session/{book_id}/start")
async def start_session(book_id: str, user_id: str = Depends(get_current_user)):
    book = supabase.table("books").select("*").eq("id", book_id).single().execute().data
    
    # We create/get session
    res = supabase.table("sessions").upsert({
        "user_id": user_id, 
        "book_id": book_id, 
        "current_stage": 1, 
        "is_archived": False
    }, on_conflict="user_id,book_id,is_archived").execute()
    
    session = res.data[0]
    # We return the question AND the role so the frontend can save it to history
    return {
        "reply": book['welcome_question'],
        "session": session,
        "role": "assistant" 
    }

@app.post("/chat")
async def chat(payload: dict = Body(...), user_id: str = Depends(get_current_user)):
    user_input = payload.get("user_input")
    book_id = payload.get("book_id")
    current_stage = payload.get("current_stage", 1)
    
    # CRITICAL: Receive previous messages from frontend
    # history format: [{"role": "assistant", "content": "..."}, {"role": "user", "content": "..."}]
    chat_history = payload.get("history", [])

    stage_data = supabase.table("story_stages").select("theme").eq("book_id", book_id).eq("stage_number", current_stage).single().execute()
    theme = stage_data.data['theme'] if stage_data.data else "Story introduction"

    # THE INSTRUCTION
    system_prompt = f"""
    You are 'Story Buddy', a guide for child authors.
    USER ROLE: The user is the AUTHOR. 
    STORY GOAL: {theme}
    
    STRICT RULES:
    1. Do not greet the user as the characters. 
    2. If the user provides names, acknowledge them as the characters of the book.
    3. Ask about the drawing on their sheet.
    4. Limit reply to 2 sentences.
    5. End with [ADVANCE] if names are confirmed, else [STAY].
    """

    # Build the conversation for Gemini
    messages = [{"role": "user", "parts": [system_prompt]}]
    for msg in chat_history:
        role = "model" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [msg["content"]]})
    
    # Add the current user input
    messages.append({"role": "user", "parts": [user_input]})

    response = model.generate_content(messages)
    ai_text = response.text

    return {
        "reply": ai_text.replace("[ADVANCE]", "").replace("[STAY]", "").strip(),
        "action": "ADVANCE" if "[ADVANCE]" in ai_text else "STAY"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
