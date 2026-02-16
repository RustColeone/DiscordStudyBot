import os
from openai import OpenAI
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
api_key = os.getenv('OPENAI_API_KEY') or botConfig['OPENAI_API_KEY']
client = OpenAI(api_key=api_key)

AI_MODEL_NAME = "chatgpt"

def _load_history_from_db(channelID):
    """Load chat history from database"""
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    
    # If no history, initialize with system prompt
    if not history:
        system_message = {"role": "system", "content": system_prompt[0]["content"]}
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[0]["content"])
        return [system_message]
    
    return history

def queryChatGPT(user_input, channelID, time, model="gpt-3.5-turbo", username=None):
    # Load history from database
    history = _load_history_from_db(channelID)
    
    # Prepend username to message if provided
    message_content = f"[{username}]: {user_input}" if username else user_input
    
    # Add user message to history
    prompt = {"role": "user", "content": message_content}
    history.append(prompt)
    db.save_chat_message(str(channelID), AI_MODEL_NAME, "user", message_content)
    
    # Query ChatGPT
    params = {
        "messages": history,
        "model": model,
        "temperature": 0.5,
        "max_tokens": 500
    }
    try:
        response = client.chat.completions.create(**params)
        replied = response.choices[0].message.content
        
        # Save assistant response to database
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "assistant", replied)
        
        return replied
    except Exception as e:
        return f"ChatGPT error: {str(e)}"


def clearHistory(channelID):
    """Clear chat history from database"""
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)

def changePrompt(channelID, index, time):
    """Change system prompt for a channel"""
    if index < 0 or index >= len(system_prompt):
        return "invalid index"
    
    # Clear existing history
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    
    # Set new system prompt
    db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[index]["content"])
    
    return f"✅ Prompt changed to **{system_prompt[index]['name']}** (index {index})\n🗑️ Chat history cleared"
