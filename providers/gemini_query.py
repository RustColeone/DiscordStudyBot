import os
import google.generativeai as genai
import yaml
import json
import datetime
import pytz
import copy
from services import database as db
from services.config_service import optional_secret
from services.context_compression import compact_history_if_needed, context_limit_for_model
from services.effort import max_tokens_for_effort

# Load system prompts from shared JSON file
with open("llm_config.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)["prompts"]

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
api_key = optional_secret(os.getenv('GEMINI_API_KEY') or botConfig.get('GEMINI_API_KEY', ''))
if api_key:
    genai.configure(api_key=api_key)

AI_MODEL_NAME = "gemini"

# Cache for active chat sessions (not persisted)
chat_sessions = {}
chat_session_contexts = {}

def _summarize_context(source, model_name):
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(
        source,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=8192,
            temperature=0.1,
        ),
    )
    return response.text

def _get_current_prompt_index(channelID):
    """Get current system prompt index from first message in history"""
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    if history and history[0]["role"] == "system":
        content = history[0]["content"]
        for i, prompt in enumerate(system_prompt):
            if prompt["content"] == content:
                return i
    return 0

def _create_chat_session(channelID, prompt_index=0, model_name="gemini-2.5-flash", system_context=None):
    """Create a new Gemini chat session with history from database"""
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    
    # Initialize with system prompt if no history
    if not history:
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[prompt_index]["content"])
    
    system_instruction = system_prompt[prompt_index]["content"]
    compressed_context = [msg["content"] for msg in history[1:] if msg["role"] == "system"]
    if compressed_context:
        system_instruction += "\n\n" + "\n\n".join(compressed_context)
    if system_context:
        system_instruction += f"\n\n{system_context}"
    model = genai.GenerativeModel(
        model_name,
        system_instruction=system_instruction,
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
    chat_session_contexts[str(channelID)] = system_context
    return chat

def _create_transient_chat_session(channelID, prompt_index=0, model_name="gemini-2.5-flash", system_context=None):
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    system_instruction = system_prompt[prompt_index]["content"]
    compressed_context = [msg["content"] for msg in history[1:] if msg["role"] == "system"]
    if compressed_context:
        system_instruction += "\n\n" + "\n\n".join(compressed_context)
    if system_context:
        system_instruction += f"\n\n{system_context}"
    model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
    gemini_history = [
        {
            "role": "user" if msg["role"] == "user" else "model",
            "parts": [msg["content"]],
        }
        for msg in history
        if msg["role"] != "system"
    ]
    return model.start_chat(history=gemini_history)

def queryGemini(user_input, channelID, time, model="gemini-2.5-flash", username=None, system_context=None,
                effort="high", persist=True):
    if not api_key:
        return "Gemini is not configured."

    compression = compact_history_if_needed(
        str(channelID),
        AI_MODEL_NAME,
        lambda source: _summarize_context(source, model),
        max_tokens_for_effort(effort),
        context_limit_for_model(AI_MODEL_NAME, model),
    )
    if compression.compressed:
        chat_sessions.pop(str(channelID), None)
        chat_session_contexts.pop(str(channelID), None)

    if not persist:
        prompt_idx = _get_current_prompt_index(channelID)
        chat = _create_transient_chat_session(channelID, prompt_idx, model, system_context)
    else:
        if str(channelID) not in chat_sessions or chat_session_contexts.get(str(channelID)) != system_context:
            prompt_idx = _get_current_prompt_index(channelID)
            _create_chat_session(channelID, prompt_idx, model, system_context)
        chat = chat_sessions[str(channelID)]

    message_content = f"[{username}]: {user_input}" if username else user_input
    if persist:
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "user", message_content)

    try:
        response = chat.send_message(
            message_content,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=max_tokens_for_effort(effort),
                temperature=0.5,
            )
        )
        replied = response.text
        if persist:
            db.save_chat_message(str(channelID), AI_MODEL_NAME, "assistant", replied)
        return replied
    except Exception as e:
        return f"Gemini error: {str(e)}"

def clearHistory(channelID):
    """Clear chat history from database and reset session"""
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    if str(channelID) in chat_sessions:
        del chat_sessions[str(channelID)]
        chat_session_contexts.pop(str(channelID), None)

def changePrompt(channelID, index, time):
    """Change system prompt for a channel"""
    if index < 0 or index >= len(system_prompt):
        return "invalid index"
    
    # Clear existing history
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    
    # Remove cached session
    if str(channelID) in chat_sessions:
        del chat_sessions[str(channelID)]
        chat_session_contexts.pop(str(channelID), None)
    
    # Create new session with new prompt
    _create_chat_session(channelID, index)
    
    return f"✅ Prompt changed to **{system_prompt[index]['name']}** (index {index})\n🗑️ Chat history cleared"
