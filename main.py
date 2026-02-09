from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import google.generativeai as genai

app = FastAPI()

# --- 1. CORS SETUP (Crucial for React) ---
# This allows your React app (on a different port) to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, replace with your React URL
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. GEMINI CONFIG ---
GEMINI_API_KEY = "AIzaSyDxJe22uh5V3cCFeOMlyQYd9-S9Q4f5oGI" # Recommendation: Use env variables
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

# --- 3. THE STORY BIBLE ---
STAGES = {
    1: {"theme": "Introducing the boy and his chameleon friend."},
    2: {"theme": "The mystical forest is losing its colors; it is turning gray and dull."},
    3: {"theme": "They use a magnifying glass to explore and hear mysterious giggles."},
    4: {"theme": "They find the Greedy Crow with a heavy, fluttering sack of stolen colors."},
    5: {"theme": "Using a slingshot with red berries, the boy hits the crow; the crow drops the sack."},
    6: {"theme": "A massive explosion of light! Rainbow colors return to the flowers and trees."},
    7: {"theme": "The Crow is sad and gray. The heroes decide to help him become colorful and happy again."},
    8: {"theme": "A final lesson about a brave heart, kindness, and making everyone feel included."}
}

# --- 1. IMAGE MAPPING ---
# Replace these URLs with your actual hosted image links
IMAGE_MAP = {
    1: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image1.jpg",
    2: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image2.jpg",
    3: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image3.jpg",
    4: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image4.jpg",
    5: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image5.jpg",
    6: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image6.jpg",
    7: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image7.jpg",
    8: "https://raw.githubusercontent.com/balaprakas/images/refs/heads/main/rainbowstory/RainbowStory_Image8.jpg"
}

# --- 4. DATA MODELS (Schemas for Postman/React) ---
class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: str

@app.get("/")
def home():
    return {"message": "Story Buddy API is Active!"}

# --- 2. START ENDPOINT ---
@app.get("/start")
async def start_story():
    return {
        "reply": "Hi! I'm your Story Buddy. I'm so excited! What names have you given our two heroes?",
        "current_stage": 1,
        "stage_turn_count": 0,
        "story_context": "The story begins.",
        "emotion": "HAPPY",
        "action": "STAY",
        "image_url": IMAGE_MAP[1]  # Return first image immediately
    }   

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    # 1. Determine the themes for the current and next stages
    current_stage_data = STAGES.get(req.current_stage, STAGES[8])
    current_theme = current_stage_data["theme"]
    
    next_stage = req.current_stage + 1
    next_theme = STAGES.get(next_stage, {"theme": "the story is complete!"})["theme"]

    # 2. Construct the "Director" Prompt for Gemini
    prompt = f"""
    You are 'Story Buddy', a magical co-author for a child's book.
    
    STORY CONTEXT: {req.story_context}
    CURRENT STAGE ({req.current_stage}/8): {current_theme}
    NEXT STAGE GOAL: {next_theme}
    TURNS TAKEN IN THIS STAGE: {req.stage_turn_count}/2

    YOUR MISSION:
    1. Acknowledge the child's input: "{req.user_input}" with excitement.
    2. If req.stage_turn_count >= 2 OR the child has clearly completed the current theme, you MUST advance.
    3. TO ADVANCE: Transition the story and ask a question that leads them to discover the NEXT STAGE. 
       End your response with the tag [ADVANCE].
    4. TO STAY: Ask a follow-up question to explore the current scene more. 
       End your response with the tag [STAY].
    5. Briefly mention an emotion in your tone (HAPPY, SURPRISED, THINKING, or SAD).
    """

    try:
        # 3. Call Gemini
        raw_response = model.generate_content(prompt).text
        
        # 4. Logic to determine if we move to the next image/stage
        # We advance if the AI says [ADVANCE] or if we've hit our turn limit
        should_advance = "[ADVANCE]" in raw_response or req.stage_turn_count >= 2
        
        # Calculate new values
        new_stage = req.current_stage + 1 if should_advance else req.current_stage
        
        # Cap the stage at 8
        if new_stage > 8:
            new_stage = 8
            
        new_turn_count = 0 if should_advance else req.stage_turn_count + 1
        
        # 5. Extract Emotion (Default to HAPPY if not found)
        detected_emotion = "HAPPY"
        for e in ["SURPRISED", "THINKING", "SAD"]:
            if e in raw_response.upper():
                detected_emotion = e

        # 6. Clean the text for the child (remove the technical tags)
        clean_reply = raw_response.replace("[ADVANCE]", "").replace("[STAY]", "").strip()

        # 7. Final Response Object
        return {
            "reply": clean_reply,
            "current_stage": new_stage,
            "stage_turn_count": new_turn_count,
            "story_context": req.story_context + f" {req.user_input}.",
            "action": "ADVANCE" if should_advance else "STAY",
            "emotion": detected_emotion,
            "image_url": IMAGE_MAP.get(new_stage, IMAGE_MAP[8])
        }

    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail="The Story Buddy got a bit sleepy. Try again!")