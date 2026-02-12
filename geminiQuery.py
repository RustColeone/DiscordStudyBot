import os
import google.generativeai as genai
import yaml
import json
import datetime
import pytz
import copy
import database as db

# Load system prompts from shared JSON file
with open("llm_config.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)["prompts"]

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
api_key = os.getenv('GEMINI_API_KEY') or botConfig.get('GEMINI_API_KEY', '')
genai.configure(api_key=api_key)

AI_MODEL_NAME = "gemini"

# Cache for active chat sessions (not persisted)
chat_sessions = {}

def _get_current_prompt_index(channelID):
    """Get current system prompt index from first message in history"""
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    if history and history[0]["role"] == "system":
        content = history[0]["content"]
        for i, prompt in enumerate(system_prompt):
            if prompt["content"] == content:
                return i
    return 0

def _create_chat_session(channelID, prompt_index=0, model_name="gemini-2.5-flash"):
    """Create a new Gemini chat session with history from database"""
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    
    # Initialize with system prompt if no history
    if not history:
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[prompt_index]["content"])
    
    model = genai.GenerativeModel(
        model_name,
        system_instruction=system_prompt[prompt_index]["content"]
    )
    
    # Convert database history to Gemini format (skip system message)
    gemini_history = []
    for msg in history:
        if msg["role"] == "system":
            continue
        gemini_history.append({
            "role": "user" if msg["role"] == "user" else "model",
            "parts": [msg["content"]]
        })
    
    chat = model.start_chat(history=gemini_history)
    chat_sessions[str(channelID)] = chat
    return chat

def queryGemini(user_input, channelID, time, model="gemini-2.5-flash"):
    # Get or create chat session
    if str(channelID) not in chat_sessions:
        prompt_idx = _get_current_prompt_index(channelID)
        _create_chat_session(channelID, prompt_idx, model)
    
    chat = chat_sessions[str(channelID)]
    
    # Save user message to database
    db.save_chat_message(str(channelID), AI_MODEL_NAME, "user", user_input)
    
    try:
        response = chat.send_message(
            user_input,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=500,
                temperature=0.5,
            )
        )
        replied = response.text
        
        # Save assistant response to database
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "assistant", replied)
        
        return replied
    except Exception as e:
        return f"Gemini error: {str(e)}"


def clearHistory(channelID):
    """Clear chat history from database and reset session"""
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    if str(channelID) in chat_sessions:
        del chat_sessions[str(channelID)]

def changePrompt(channelID, index, time):
    """Change system prompt for a channel"""
    if index < 0 or index >= len(system_prompt):
        return "invalid index"
    
    # Clear existing history
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    
    # Remove cached session
    if str(channelID) in chat_sessions:
        del chat_sessions[str(channelID)]
    
    # Create new session with new prompt
    _create_chat_session(channelID, index)
    
    return f"✅ Prompt changed to **{system_prompt[index]['name']}** (index {index})\n🗑️ Chat history cleared"
