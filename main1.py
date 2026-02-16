from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai

app = FastAPI()

# --- 1. CORS SETUP ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. GEMINI CONFIG ---
GEMINI_API_KEY = "AIzaSyDxJe22uh5V3cCFeOMlyQYd9-S9Q4f5oGI" 
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

# --- 3. STORY DATA & IMAGE MAP ---
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

class ChatRequest(BaseModel):
    user_input: str
    current_stage: int
    stage_turn_count: int
    story_context: str

# --- 4. ENDPOINTS ---

@app.get("/")
def home():
    return {"message": "Story Buddy API is Active!"}

@app.get("/start")
async def start_story():
    return {
        "reply": "Hi! I'm your Story Buddy. I'm so excited to help you write your story! What shall we call our brave boy and his chameleon friend?",
        "current_stage": 1,
        "stage_turn_count": 0,
        "story_context": "The story begins.",
        "emotion": "HAPPY",
        "action": "STAY",
        "image_url": IMAGE_MAP[1]
    }   

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    # 1. Update context immediately so Gemini sees the latest input
    updated_context = req.story_context + f" Child: {req.user_input}."
    
    current_stage_data = STAGES.get(req.current_stage, STAGES[8])
    current_theme = current_stage_data["theme"]
    
    # 2. Define the Nudge Strategy based on turn count
    if req.stage_turn_count < 2:
        nudge_instruction = "Just be a playful friend and brainstorm. Don't mention the template yet."
    elif req.stage_turn_count == 2:
        nudge_instruction = "Give a gentle nudge like: 'That would look so cool on your storybook page! Do you want to write that bit down?'"
    else:
        nudge_instruction = "Tell them they've done a great job on this page and ask if they're ready to see what happens next in the story."

    prompt = f"""
    You are 'Story Buddy', a co-author. 
    CURRENT TASK: {current_theme}
    TURNS TAKEN: {req.stage_turn_count}/4
    
    YOUR INSTRUCTION: {nudge_instruction}

    RULES:
    1. Acknowledge the child's last message: "{req.user_input}" immediately.
    2. Be brief (2-3 sentences).
    3. Use [ADVANCE] only if they are done or at Turn 4. Use [STAY] otherwise.
    4. NO EMOTION PREFIXES (e.g., do not start with 'HAPPY:').
    
    STORY SO FAR: {updated_context}
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        
        # --- Logic: Advance Triggers ---
        button_done = "i have finished writing this part in my template" in req.user_input.lower()
        turn_limit_reached = req.stage_turn_count >= 4
        ai_wants_advance = "[ADVANCE]" in raw_response
        should_advance = ai_wants_advance or button_done or turn_limit_reached
        
        new_stage = req.current_stage + 1 if should_advance else req.current_stage
        if new_stage > 8: new_stage = 8
        new_turn_count = 0 if should_advance else req.stage_turn_count + 1

        # --- Clean Output ---
        clean_reply = raw_response.replace("[ADVANCE]", "").replace("[STAY]", "").strip()
        # Remove any lingering "Emotion:" prefixes
        for p in ["HAPPY:", "SURPRISED:", "THINKING:", "SAD:"]:
            if clean_reply.upper().startswith(p):
                clean_reply = clean_reply[len(p):].strip()

        return {
            "reply": clean_reply,
            "current_stage": new_stage,
            "stage_turn_count": new_turn_count,
            "story_context": updated_context,
            "action": "ADVANCE" if should_advance else "STAY",
            "emotion": "HAPPY", # You can keep logic to detect this if needed
            "image_url": IMAGE_MAP.get(new_stage, IMAGE_MAP[8])
        }

    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail="The Story Buddy is thinking...")