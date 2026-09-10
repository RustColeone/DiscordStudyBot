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
from services.effort import deepseek_max_tokens_for_effort, normalize_effort
from services.vision import openai_image_content, persistent_image_text

# Load system prompts from shared JSON file
with open("llm_config.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)["prompts"]

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
# DeepSeek uses OpenAI-compatible API
api_key = optional_secret(os.getenv('DEEPSEEK_API_KEY') or botConfig.get('DEEPSEEK_API_KEY', ''))
client = (
    OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    if api_key
    else None
)

AI_MODEL_NAME = "deepseek"

def _summarize_context(source, model):
    params = {
        "messages": [{"role": "user", "content": source}],
        "model": model,
        "temperature": 0.1,
        "max_tokens": 8192,
        "reasoning_effort": "low",
    }
    try:
        response = client.chat.completions.create(**params)
    except Exception as error:
        if "reasoning_effort" not in str(error):
            raise
        params.pop("reasoning_effort")
        response = client.chat.completions.create(**params)
    return response.choices[0].message.content or ""

def _create_completion(params):
    try:
        return client.chat.completions.create(**params)
    except Exception as error:
        if "reasoning_effort" not in str(error):
            raise
        fallback_params = dict(params)
        fallback_params.pop("reasoning_effort", None)
        return client.chat.completions.create(**fallback_params)

def _is_context_error(error):
    message = str(error).lower()
    return "context" in message and ("length" in message or "token" in message)

def _load_history_from_db(channelID, model="deepseek-flash", effort="high"):
    """Load chat history from database"""
    compact_history_if_needed(
        str(channelID),
        AI_MODEL_NAME,
        lambda source: _summarize_context(source, model),
        deepseek_max_tokens_for_effort(effort),
        context_limit_for_model(AI_MODEL_NAME, model),
    )
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    
    # If no history, initialize with system prompt
    if not history:
        system_message = {"role": "system", "content": system_prompt[0]["content"]}
        db.save_chat_message(str(channelID), AI_MODEL_NAME, "system", system_prompt[0]["content"])
        return [system_message]
    
    return history

def queryDeepSeek(user_input, channelID, time, model="deepseek-flash", username=None, system_context=None,
                  effort="high", persist=True, images=None):
    if client is None:
        return "DeepSeek is not configured."

    # Load history from database
    history = _load_history_from_db(channelID, model, effort)
    
    # Prepend username to message if provided
    message_content = f"[{username}]: {user_input}" if username else user_input
    images = images or []
    
    # Add user message to history
    prompt = {"role": "user", "content": openai_image_content(message_content, images)}
    history.append(prompt)
    if persist:
        db.save_chat_message(
            str(channelID), AI_MODEL_NAME, "user", persistent_image_text(message_content, images)
        )

    request_history = list(history)
    if system_context:
        request_history.insert(1, {"role": "system", "content": system_context})
    
    # Query DeepSeek
    params = {
        "messages": request_history,
        "model": model,
        "temperature": 0.5,
        "max_tokens": deepseek_max_tokens_for_effort(effort),
        "reasoning_effort": normalize_effort(effort),
    }
    try:
        try:
            response = _create_completion(params)
        except Exception as error:
            if not _is_context_error(error):
                raise
            compression = compact_history_if_needed(
                str(channelID),
                AI_MODEL_NAME,
                lambda source: _summarize_context(source, model),
                deepseek_max_tokens_for_effort(effort),
                context_limit_for_model(AI_MODEL_NAME, model),
                force=True,
            )
            if not compression.compressed:
                raise
            request_history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
            if images and request_history and request_history[-1]["role"] == "user":
                request_history[-1] = prompt
            if system_context:
                request_history.insert(1, {"role": "system", "content": system_context})
            params["messages"] = request_history
            response = _create_completion(params)
        choice = response.choices[0]
        replied = choice.message.content or ""
        if choice.finish_reason == "length":
            replied += (
                "\n\n[Response stopped at DeepSeek's output-token limit. "
                "Ask me to continue from where I stopped.]"
            )
        
        # Save assistant response to database
        if persist:
            db.save_chat_message(str(channelID), AI_MODEL_NAME, "assistant", replied)
        
        return replied
    except Exception as e:
        error_message = str(e)
        lowered_error = error_message.lower()
        if _is_context_error(e):
            return (
                "DeepSeek's 1M-token context window was exceeded, and automatic context "
                "compression could not complete. Your history was left intact; please retry shortly."
            )
        return f"DeepSeek error: {error_message}"


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
