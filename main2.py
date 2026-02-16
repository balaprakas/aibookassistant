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

# Load environment variables
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
model = genai.GenerativeModel(os.getenv("MODEL_NAME", "gemini-1.5-flash"))

# --- 2. CORS MIDDLEWARE ---
# Essential for allowing Lovable to send Authorization headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- 3. MODELS ---
class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: str
    book_id: str
    session_id: Optional[str] = None

# --- 4. AUTH HELPERS ---
def create_access_token(data: dict):
    """Creates a local JWT token for the user session."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

async def get_current_user(authorization: str = Header(None)):
    """Dependency to protect routes and extract user identity."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id: 
            raise HTTPException(status_code=401)
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

# --- 5. LOGIN ENDPOINT ---

@app.post("/auth/login")
async def auth_login(payload: dict):
    """
    Exchanges a Google Credential for an app-specific JWT.
    Updates or Creates the user in Supabase.
    """
    token = payload.get("credential")
    if not token:
        raise HTTPException(status_code=400, detail="Missing credential")

    try:
        # 1. Verify the Google ID Token
        idinfo = id_token.verify_oauth2_token(
            token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
        )

        # 2. Extract user profile info
        user_data = {
            "email": idinfo['email'],
            "name": idinfo.get('name'),
            "avatar_url": idinfo.get('picture'),
            "last_login": datetime.utcnow().isoformat()
        }

        # 3. Upsert user into Supabase 'users' table
        # If email exists, update the record. Otherwise, insert new.
        res = supabase.table("users").upsert(user_data, on_conflict="email").execute()
        
        if not res.data:
            raise HTTPException(status_code=500, detail="Database error during upsert")
            
        user_record = res.data[0]

        # 4. Create an internal App JWT
        access_token = create_access_token({"user_id": user_record['id']})

        # 5. Return token and user profile to Lovable
        return {
            "token": access_token, 
            "user": {
                "id": user_record['id'],
                "name": user_record['name'],
                "email": user_record['email'],
                "avatar": user_record['avatar_url']
            }
        }
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    except Exception as e:
        print(f"Auth error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error during Authentication")

# --- PLACEHOLDERS FOR REMAINING LOGIC ---
# These will be updated with full logic in subsequent steps as we add sessions.

@app.get("/books")
async def get_all_books(user_id: str = Depends(get_current_user)):
    # Logic to fetch books...
    return {"message": "Authenticated. Books will be returned here."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
