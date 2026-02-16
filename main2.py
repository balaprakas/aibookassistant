import os
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

app = FastAPI()

# --- 1. CONFIGURATION ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3-flash-preview")

# Constants for Story Logic
MAX_TURNS_PER_STAGE = 3 

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: str
    book_id: str

# --- 2. ENDPOINTS ---

@app.get("/")
async def root():
    return {"message": "Story Buddy API is running!"}

@app.get("/start/{book_id}")
async def start_story(book_id: str):
    """Initializes the story session."""
    try:
        book_res = supabase.table("books").select("title, welcome_question").eq("id", book_id).single().execute()
        stage_res = supabase.table("story_stages").select("image_url").eq("book_id", book_id).eq("stage_number", 1).single().execute()
        
        if not book_res.data:
            raise HTTPException(status_code=404, detail="Book not found.")
        
        book = book_res.data
        stage = stage_res.data

        return {
            "reply": f"Hi! I'm your Story Buddy. I'm so excited to help you write '{book['title']}'! {book['welcome_question']}",
            "current_stage": 1,
            "stage_turn_count": 0,
            "story_context": f"Starting the book: {book['title']}.",
            "action": "STAY",
            "image_url": stage["image_url"],
            "book_id": book_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """Main conversation loop with strict turn-gate advancement logic."""
    
    updated_context = req.story_context + f" | Child: {req.user_input}"
    
    stages_res = supabase.table("story_stages") \
        .select("stage_number, theme, image_url") \
        .eq("book_id", req.book_id) \
        .in_("stage_number", [req.current_stage, req.current_stage + 1]) \
        .execute()
    
    stages_data = {s['stage_number']: s for s in stages_res.data}
    current_stage_info = stages_data.get(req.current_stage)
    next_stage_info = stages_data.get(req.current_stage + 1)
    next_stage_exists = next_stage_info is not None

    if not current_stage_info:
        raise HTTPException(status_code=404, detail="Stage info missing.")

    next_theme_clue = next_stage_info['theme'] if next_stage_exists else "the wonderful ending"

    # --- PROGRESSION NUDGES ---
    if req.stage_turn_count < 1:
        nudge = "The conversation just started. Focus on getting names and setting the scene. Do NOT move on yet."
    elif req.stage_turn_count < 2:
        nudge = f"Deepen the scene. Ask specific questions about their ideas and mention the images at the back of their book."
    else:
        nudge = f"Time to transition. Specifically ask if they see any images related to: {next_theme_clue}."

    prompt = f"""
    You are 'Story Buddy', a magical co-author. 
    
    CURRENT GOAL: {current_stage_info['theme']}
    NEXT DISCOVERY: {next_theme_clue}
    
    STORY STATUS:
    - Child's latest input: "{req.user_input}"
    - Conversation Turn: {req.stage_turn_count + 1} of {MAX_TURNS_PER_STAGE}
    
    DIRECTIONS:
    1. Acknowledge the child's input warmly.
    2. {nudge}
    3. If the child said something short like 'hi' or 'okay', stay on this page ([STAY]) and ask for more detail.
    4. When ready to move (Turn 3), ask a specific question: "Do you see [clue from NEXT DISCOVERY] in your image sheet?"
    5. Use [STAY] for turns 1 and 2. Only use [ADVANCE] if the child is ready and you are at Turn 3.
    6. Keep it to 2 brief, enchanting sentences.
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        
        # --- STRICT ADVANCEMENT LOGIC ---
        button_done = "i have finished writing" in req.user_input.lower()
        ai_wants_advance = "[ADVANCE]" in raw_response
        turn_limit_reached = req.stage_turn_count >= (MAX_TURNS_PER_STAGE - 1)
        
        # GATE: We only allow advancement if we are at least at Turn 2 (the 3rd message) 
        # unless the child explicitly says they are finished writing.
        ready_to_move = (req.stage_turn_count >= 2) or button_done
        
        should_advance = ready_to_move and (ai_wants_advance or turn_limit_reached) and next_stage_exists
        
        # Calculate new state
        new_stage = req.current_stage + 1 if should_advance else req.current_stage
        new_turn_count = 0 if should_advance else req.stage_turn_count + 1
        final_stage_info = stages_data.get(new_stage, current_stage_info)
        
        # Clean AI response
        clean_reply = raw_response.replace("[ADVANCE]", "").replace("[STAY]", "").strip()
        if ":" in clean_reply[:12]: 
            clean_reply = clean_reply.split(":", 1)[-1].strip()

        return {
            "reply": clean_reply,
            "current_stage": new_stage,
            "stage_turn_count": new_turn_count,
            "story_context": updated_context,
            "action": "ADVANCE" if should_advance else "STAY",
            "image_url": final_stage_info["image_url"]
        }
        
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail="Story Buddy is resting. Try again soon!")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)