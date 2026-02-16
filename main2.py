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
MODEL_NAME = os.getenv("MODEL_NAME")

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

@app.get("/books")
async def get_all_books():
    """Returns all books with their Stage 1 image as a thumbnail."""
    try:
        # 1. Fetch all books
        books_res = supabase.table("books").select("id, title").execute()
        
        if not books_res.data:
            return {"total_books": 0, "books": []}

        all_books = []
        
        for book in books_res.data:
            # 2. For each book, fetch the image for stage_number 1
            stage_res = supabase.table("story_stages") \
                .select("image_url") \
                .eq("book_id", book["id"]) \
                .eq("stage_number", 1) \
                .single() \
                .execute()
            
            # Use a placeholder if no image is found
            thumbnail = stage_res.data["image_url"] if stage_res.data else "https://via.placeholder.com/150"
            
            all_books.append({
                "book_id": book["id"],
                "title": book["title"],
                "thumbnail": thumbnail
            })

        return {
            "total_books": len(all_books),
            "books": all_books
        }
    except Exception as e:
        print(f"Error fetching books: {e}")
        raise HTTPException(status_code=500, detail="Could not load books.")

@app.get("/start/{book_id}")
async def start_story(book_id: str):
    """Initializes the story and returns the dynamic total stages count."""
    try:
        # 1. Fetch book metadata
        book_res = supabase.table("books").select("*").eq("id", book_id).single().execute()
        if not book_res.data:
            raise HTTPException(status_code=404, detail="Book not found.")
        
        # 2. Fetch all stages to get count and first image
        stages_res = supabase.table("story_stages").select("stage_number, image_url").eq("book_id", book_id).order("stage_number").execute()
        
        total_stages = len(stages_res.data)
        first_stage = stages_res.data[0] if stages_res.data else None

        return {
            "reply": f"Hi! I'm your Story Buddy. {book_res.data['welcome_question']}",
            "current_stage": 1,
            "total_stages": total_stages, 
            "stage_turn_count": 0,
            "story_context": f"Book: {book_res.data['title']}",
            "action": "STAY",
            "image_url": first_stage["image_url"] if first_stage else None,
            "book_id": book_id
        }
    except Exception as e:
        print(f"Start Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """Main loop with dynamic progression and strict name-check logic."""
    
    updated_context = req.story_context + f" | Child: {req.user_input}"
    
    # 1. Fetch stage info and total count dynamically
    stages_res = supabase.table("story_stages") \
        .select("stage_number, theme, image_url") \
        .eq("book_id", req.book_id) \
        .execute()
    
    total_stages = len(stages_res.data)
    stages_data = {s['stage_number']: s for s in stages_res.data}
    
    current_stage_info = stages_data.get(req.current_stage)
    next_stage_exists = (req.current_stage + 1) in stages_data
    next_stage_info = stages_data.get(req.current_stage + 1)

    if not current_stage_info:
        raise HTTPException(status_code=404, detail="Stage info missing.")

    next_theme_clue = next_stage_info['theme'] if next_stage_exists else "the magical ending"

    # --- PROGRESSION NUDGES ---
    if req.current_stage == 1 and req.stage_turn_count < 1:
        nudge = "The story just started. Your ONLY goal is to find out the names for the hero and their friend. Do NOT move on until they give you names."
    elif req.stage_turn_count < 2:
        nudge = f"Deepen the scene. Ask specific questions about what they see in this image: {current_stage_info['theme']}."
    else:
        nudge = f"Time to transition. Specifically ask if they see any images related to: {next_theme_clue}."

    prompt = f"""
    You are 'Story Buddy', a magical co-author. 
    CURRENT GOAL: {current_stage_info['theme']}
    NEXT DISCOVERY: {next_theme_clue}
    TURN: {req.stage_turn_count + 1} of {MAX_TURNS_PER_STAGE}
    CHILD'S INPUT: "{req.user_input}"

    DIRECTIONS:
    1. Acknowledge child warmly. {nudge}
    2. If turn < {MAX_TURNS_PER_STAGE}, end with [STAY]. 
    3. If turn >= {MAX_TURNS_PER_STAGE}, ask discovery question about NEXT DISCOVERY and end with [ADVANCE].
    4. Keep it to 2 enchanting sentences.
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        
        # --- DYNAMIC ADVANCEMENT LOGIC ---
        button_done = "i have finished writing" in req.user_input.lower()
        ai_wants_advance = "[ADVANCE]" in raw_response
        turn_limit_reached = req.stage_turn_count >= (MAX_TURNS_PER_STAGE - 1)
        
        # Gate: At least 2 turns (3 messages) unless explicitly finished
        ready_to_move = (req.stage_turn_count >= 2) or button_done
        should_advance = ready_to_move and (ai_wants_advance or turn_limit_reached)
        
        if should_advance and next_stage_exists:
            new_stage = req.current_stage + 1
            action = "ADVANCE"
            new_turn_count = 0
        elif should_advance and not next_stage_exists:
            new_stage = req.current_stage
            action = "FINISH"
            new_turn_count = req.stage_turn_count + 1
        else:
            new_stage = req.current_stage
            action = "STAY"
            new_turn_count = req.stage_turn_count + 1

        final_stage_info = stages_data.get(new_stage, current_stage_info)
        clean_reply = raw_response.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

        return {
            "reply": clean_reply,
            "current_stage": new_stage,
            "total_stages": total_stages,
            "stage_turn_count": new_turn_count,
            "story_context": updated_context,
            "action": action,
            "image_url": final_stage_info["image_url"]
        }
    except Exception as e:
        print(f"Chat Error: {e}")
        raise HTTPException(status_code=500, detail="Story Buddy is resting.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
