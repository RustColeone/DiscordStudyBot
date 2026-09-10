import os
from openai import OpenAI
import yaml
import json
import datetime
import pytz
import copy
from services import database as db
from services.config_service import optional_secret
from services.context_compression import compact_history_if_needed, context_limit_for_model
from services.effort import max_tokens_for_effort
from services.vision import inline_image_urls, openai_image_content, persistent_image_text

# Load system prompts from shared JSON file
with open("llm_config.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)["prompts"]

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
api_key = optional_secret(os.getenv('OPENAI_API_KEY') or botConfig.get('OPENAI_API_KEY'))
client = OpenAI(api_key=api_key) if api_key else None

AI_MODEL_NAME = "chatgpt"

def _summarize_context(source, model):
    response = client.chat.completions.create(
        messages=[{"role": "user", "content": source}],
        model=model,
        temperature=0.1,
        max_tokens=4096,
    )
    return response.choices[0].message.content or ""

def _load_history_from_db(channelID, model="gpt-3.5-turbo", effort="high"):
    """Load chat history from database"""
    compact_history_if_needed(
        str(channelID),
        AI_MODEL_NAME,
        lambda source: _summarize_context(source, model),
        max_tokens_for_effort(effort),
        context_limit_for_model(AI_MODEL_NAME, model),
    )
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    
    # If no history, initialize with system prompt
    if not history:
        system_message = {"role": "system", "content": system_prompt[0]["content"]}
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[0]["content"])
        return [system_message]
    
    return history

def queryChatGPT(user_input, channelID, time, model="gpt-3.5-turbo", username=None, system_context=None,
                 effort="high", persist=True, images=None):
    if client is None:
        return "ChatGPT is not configured."

    # Load history from database
    history = _load_history_from_db(channelID, model, effort)
    
    # Prepend username to message if provided
    message_content = f"[{username}]: {user_input}" if username else user_input
    images = images or []
    try:
        request_images = inline_image_urls(images) if images else []
    except Exception as error:
        return f"ChatGPT image preparation failed: {error}"
    
    # Add user message to history
    prompt = {"role": "user", "content": openai_image_content(message_content, request_images)}
    history.append(prompt)
    if persist:
        db.save_chat_message(
            str(channelID), AI_MODEL_NAME, "user", persistent_image_text(message_content, images)
        )

    request_history = list(history)
    if system_context:
        request_history.insert(1, {"role": "system", "content": system_context})
    
    # Query ChatGPT
    params = {
        "messages": request_history,
        "model": model,
        "temperature": 0.5,
        "max_tokens": max_tokens_for_effort(effort)
    }
    try:
        response = client.chat.completions.create(**params)
        replied = response.choices[0].message.content
        
        # Save assistant response to database
        if persist:
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
