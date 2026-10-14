import os
from google import genai
from google.genai import types
import yaml
import json
from services import database as db
from services.config_service import optional_secret
from services.context_compression import compact_history_if_needed, context_limit_for_model
from services.effort import max_tokens_for_effort
from services.vision import download_image_bytes, persistent_image_text

# Load system prompts from shared JSON file
with open("llm_config.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)["prompts"]

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
api_key = optional_secret(os.getenv('GEMINI_API_KEY') or botConfig.get('GEMINI_API_KEY', ''))
client = genai.Client(api_key=api_key) if api_key else None

AI_MODEL_NAME = "gemini"

def _summarize_context(source, model_name):
    response = client.models.generate_content(
        model=model_name,
        contents=source,
        config=types.GenerateContentConfig(
            max_output_tokens=8192,
            temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
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

def _build_request(channelID, prompt_index=0, system_context=None, initialize=False):
    history = db.load_chat_history(str(channelID), AI_MODEL_NAME)
    if not history:
        if initialize:
            db.save_chat_message(
                str(channelID), AI_MODEL_NAME, "system", system_prompt[prompt_index]["content"]
            )
        history = [{"role": "system", "content": system_prompt[prompt_index]["content"]}]

    system_instruction = (
        history[0]["content"]
        if history[0]["role"] == "system"
        else system_prompt[prompt_index]["content"]
    )
    compressed_context = [msg["content"] for msg in history[1:] if msg["role"] == "system"]
    if compressed_context:
        system_instruction += "\n\n" + "\n\n".join(compressed_context)
    if system_context:
        system_instruction += f"\n\n{system_context}"

    contents = []
    for msg in history:
        if msg["role"] == "system":
            continue
        contents.append(types.Content(
            role="user" if msg["role"] == "user" else "model",
            parts=[types.Part.from_text(text=msg["content"])],
        ))
    return system_instruction, contents

def queryGemini(user_input, channelID, time, model="gemini-3.8-flash", username=None, system_context=None,
                effort="high", persist=True, images=None):
    if client is None:
        return "Gemini is not configured."

    compression = compact_history_if_needed(
        str(channelID),
        AI_MODEL_NAME,
        lambda source: _summarize_context(source, model),
        max_tokens_for_effort(effort),
        context_limit_for_model(AI_MODEL_NAME, model),
    )
    prompt_idx = _get_current_prompt_index(channelID)
    system_instruction, contents = _build_request(
        channelID, prompt_idx, system_context, initialize=persist
    )

    message_content = f"[{username}]: {user_input}" if username else user_input
    images = images or []
    if persist:
        db.save_chat_message(
            str(channelID), AI_MODEL_NAME, "user", persistent_image_text(message_content, images)
        )

    try:
        message_parts = [types.Part.from_text(text=message_content)]
        for image in images:
            message_parts.append(types.Part.from_bytes(
                data=download_image_bytes(image["url"]),
                mime_type=image["content_type"],
            ))
        contents.append(types.Content(role="user", parts=message_parts))
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_tokens_for_effort(effort),
                temperature=0.5,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        replied = response.text or ""
        if persist:
            db.save_chat_message(str(channelID), AI_MODEL_NAME, "assistant", replied)
        return replied
    except Exception as e:
        return f"Gemini error: {str(e)}"

def clearHistory(channelID):
    """Clear chat history from database."""
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)

def changePrompt(channelID, index, time):
    """Change system prompt for a channel"""
    if index < 0 or index >= len(system_prompt):
        return "invalid index"
    
    # Clear existing history
    db.clear_chat_history(str(channelID), AI_MODEL_NAME)
    
    db.save_chat_message(
        str(channelID), AI_MODEL_NAME, "system", system_prompt[index]["content"]
    )
    
    return f"✅ Prompt changed to **{system_prompt[index]['name']}** (index {index})\n🗑️ Chat history cleared"
