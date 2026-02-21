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
genai.configure(api_key=GEMINI_API_KEY)
# Using your specified model
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
    # Fix: Ensure Gemini receives the current user input as part of the context immediately
    updated_context = req.story_context + f" | Child said: {req.user_input}"
    
    current_stage_data = STAGES.get(req.current_stage, STAGES[8])
    current_theme = current_stage_data["theme"]
    
    # Logic: Dynamic nudge instruction based on turn count to avoid monotony
    if req.stage_turn_count < 2:
        nudge_instruction = "Focus purely on play and brainstorming. Do NOT mention writing or the template."
    elif req.stage_turn_count == 2:
        nudge_instruction = "Give a natural nudge like: 'That belongs in your book! Want to add it to your page?'"
    else:
        nudge_instruction = "Acknowledge their great work and guide them toward the next part of the story."

    prompt = f"""
    You are 'Story Buddy', a magical co-author. 
    
    IMPORTANT: The child just said: "{req.user_input}". 
    You MUST respond specifically to that detail first.

    STORY PROGRESS:
    - Context: {updated_context}
    - Current Scene: {current_theme}
    - Turn count: {req.stage_turn_count}/4

    DIRECTIONS:
    - {nudge_instruction}
    - Keep responses to 2 sentences.
    - Start directly with dialogue. NEVER use prefixes like 'HAPPY:' or '(SURPRISED)'.
    - If turns < 4, use [STAY]. If turns >= 4, use [ADVANCE].
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        
        # Advance Logic
        button_done = "i have finished writing this part in my template" in req.user_input.lower()
        turn_limit_reached = req.stage_turn_count >= 4
        ai_wants_advance = "[ADVANCE]" in raw_response
        should_advance = ai_wants_advance or button_done or turn_limit_reached
        
        new_stage = req.current_stage + 1 if should_advance else req.current_stage
        if new_stage > 8: new_stage = 8
        new_turn_count = 0 if should_advance else req.stage_turn_count + 1

        # Extract Emotion
        detected_emotion = "HAPPY"
        for e in ["SURPRISED", "THINKING", "SAD"]:
            if e in raw_response.upper(): detected_emotion = e

        # Final string cleaning to remove technical tags and emotion prefixes
        clean_reply = raw_response.replace("[ADVANCE]", "").replace("[STAY]", "").strip()
        
        prefixes_to_clean = ["HAPPY:", "SURPRISED:", "THINKING:", "SAD:", "(HAPPY)", "(SURPRISED)", "STORY BUDDY:"]
        for p in prefixes_to_clean:
            if clean_reply.upper().startswith(p):
                clean_reply = clean_reply[len(p):].strip()

        return {
            "reply": clean_reply,
            "current_stage": new_stage,
            "stage_turn_count": new_turn_count,
            "story_context": updated_context,
            "action": "ADVANCE" if should_advance else "STAY",
            "emotion": detected_emotion,
            "image_url": IMAGE_MAP.get(new_stage, IMAGE_MAP[8])
        }

    except Exception as e:
        print(f"Error: {e}")

        raise HTTPException(status_code=500, detail="Story Buddy got a bit confused! Try again.")
