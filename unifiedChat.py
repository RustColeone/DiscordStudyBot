"""
Unified chat interface that routes to different LLM providers
"""
import chatGPTQuery
import geminiQuery
import deepseekQuery
import database as db
import json
from openai import OpenAI
import google.generativeai as genai
import os
import yaml
import time

# Load config for API keys
with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Model cache (refreshed periodically)
_model_cache = {}
_cache_timestamp = {}

def _load_fallback_models():
    """Load fallback models from llm_config.json"""
    with open("llm_config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
        return config.get("fallback_models", {
            "last_update": 0,
            "models": {
                "chatgpt": {
                    "name": "ChatGPT (OpenAI)",
                    "models": ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini"],
                    "default_model": "gpt-3.5-turbo"
                },
                "gemini": {
                    "name": "Gemini (Google)",
                    "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-pro"],
                    "default_model": "gemini-2.5-flash"
                },
                "deepseek": {
                    "name": "DeepSeek",
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                    "default_model": "deepseek-chat"
                }
            }
        })

def _save_fallback_models(fallback_data):
    """Save updated fallback models to llm_config.json"""
    try:
        with open("llm_config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        
        config["fallback_models"] = fallback_data
        
        with open("llm_config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Failed to save fallback models: {e}")

# Fallback models in case API fetch fails
FALLBACK_MODELS = _load_fallback_models()["models"]

def _fetch_openai_models():
    """Fetch available models from OpenAI API"""
    try:
        api_key = os.getenv('OPENAI_API_KEY') or botConfig.get('OPENAI_API_KEY', '')
        if not api_key:
            return None
        
        client = OpenAI(api_key=api_key)
        models = client.models.list()
        
        # Filter for chat models only (gpt-*)
        chat_models = [m.id for m in models.data if m.id.startswith('gpt-')]
        # Sort and prioritize common models
        priority = ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-4', 'gpt-3.5-turbo']
        sorted_models = [m for m in priority if m in chat_models]
        sorted_models += [m for m in sorted(chat_models) if m not in priority]
        
        return sorted_models[:10] if sorted_models else None  # Limit to 10 most relevant
    except Exception as e:
        print(f"Failed to fetch OpenAI models: {e}")
        return None

def _fetch_gemini_models():
    """Fetch available models from Google Gemini API"""
    try:
        api_key = os.getenv('GEMINI_API_KEY') or botConfig.get('GEMINI_API_KEY', '')
        if not api_key:
            return None
        
        genai.configure(api_key=api_key)
        models = genai.list_models()
        
        # Filter for models that support generateContent
        gemini_models = [
            m.name.replace('models/', '') 
            for m in models 
            if 'generateContent' in m.supported_generation_methods
        ]
        
        return gemini_models if gemini_models else None
    except Exception as e:
        print(f"Failed to fetch Gemini models: {e}")
        return None

def _fetch_deepseek_models():
    """Fetch available models from DeepSeek API"""
    try:
        api_key = os.getenv('DEEPSEEK_API_KEY') or botConfig.get('DEEPSEEK_API_KEY', '')
        if not api_key:
            return None
        
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        models = client.models.list()
        
        # Get all DeepSeek models
        deepseek_models = [m.id for m in models.data]
        
        return deepseek_models if deepseek_models else None
    except Exception as e:
        print(f"Failed to fetch DeepSeek models: {e}")
        return None

def _get_available_llms():
    """Get available LLMs with dynamically fetched models, fallback to hardcoded"""
    # Cache for 1 hour
    cache_duration = 3600
    current_time = time.time()
    
    if 'AVAILABLE_LLMS' in _model_cache:
        if current_time - _cache_timestamp.get('AVAILABLE_LLMS', 0) < cache_duration:
            return _model_cache['AVAILABLE_LLMS']
    
    # Check if fallback models need updating (older than 7 days)
    fallback_data = _load_fallback_models()
    fallback_age = current_time - fallback_data.get("last_update", 0)
    needs_fallback_update = fallback_age > (7 * 24 * 3600)  # 7 days
    
    # Fetch models dynamically
    llms = {}
    updated_fallbacks = {}
    
    # ChatGPT
    chatgpt_models = _fetch_openai_models()
    if chatgpt_models:
        updated_fallbacks["chatgpt"] = {
            "name": "ChatGPT (OpenAI)",
            "models": chatgpt_models,
            "default_model": chatgpt_models[0]
        }
    llms["chatgpt"] = {
        "name": "ChatGPT (OpenAI)",
        "models": chatgpt_models or fallback_data["models"]["chatgpt"]["models"],
        "default_model": chatgpt_models[0] if chatgpt_models else fallback_data["models"]["chatgpt"]["default_model"]
    }
    
    # Gemini
    gemini_models = _fetch_gemini_models()
    if gemini_models:
        updated_fallbacks["gemini"] = {
            "name": "Gemini (Google)",
            "models": gemini_models,
            "default_model": gemini_models[0]
        }
    llms["gemini"] = {
        "name": "Gemini (Google)",
        "models": gemini_models or fallback_data["models"]["gemini"]["models"],
        "default_model": gemini_models[0] if gemini_models else fallback_data["models"]["gemini"]["default_model"]
    }
    
    # DeepSeek
    deepseek_models = _fetch_deepseek_models()
    if deepseek_models:
        updated_fallbacks["deepseek"] = {
            "name": "DeepSeek",
            "models": deepseek_models,
            "default_model": deepseek_models[0]
        }
    llms["deepseek"] = {
        "name": "DeepSeek",
        "models": deepseek_models or fallback_data["models"]["deepseek"]["models"],
        "default_model": deepseek_models[0] if deepseek_models else fallback_data["models"]["deepseek"]["default_model"]
    }
    
    # Update fallback models if we fetched new data or they're stale
    if updated_fallbacks or needs_fallback_update:
        # Merge with existing fallbacks for any providers that failed to fetch
        for provider in ["chatgpt", "gemini", "deepseek"]:
            if provider not in updated_fallbacks:
                updated_fallbacks[provider] = fallback_data["models"][provider]
        
        _save_fallback_models({
            "last_update": current_time,
            "models": updated_fallbacks
        })
    
    # Cache the result
    _model_cache['AVAILABLE_LLMS'] = llms
    _cache_timestamp['AVAILABLE_LLMS'] = current_time
    
    return llms

# Get available LLMs (will be fetched dynamically on first call)
AVAILABLE_LLMS = _get_available_llms()

def get_models_list() -> str:
    """Generate formatted message showing all available LLMs and their models"""
    # Refresh available LLMs to get latest models
    global AVAILABLE_LLMS
    AVAILABLE_LLMS = _get_available_llms()
    
    msg = "**Available LLMs and Models:**\n\n"
    
    for llm_id, llm_info in AVAILABLE_LLMS.items():
        msg += f"**{llm_info['name']}** (`{llm_id}`)\n"
        msg += "├─ Models: " + ", ".join([f"`{m}`" for m in llm_info['models']]) + "\n"
        msg += f"└─ Default: `{llm_info['default_model']}`\n\n"
    
    return msg

def get_prompt_list() -> str:
    """Generate formatted message showing available prompt presets"""
    with open('llm_config.json', 'r', encoding='utf-8') as f:
        prompts = json.load(f)['prompts']
    
    msg = "**Available Prompt Presets:**\n\n"
    for i, prompt in enumerate(prompts):
        msg += f"`{i}` - **{prompt['name']}**\n"
        # Show preview (first 100 chars)
        preview = prompt['content'][:100] + "..." if len(prompt['content']) > 100 else prompt['content']
        msg += f"   _{preview}_\n\n"
    
    return msg

def query_chat(user_input: str, channel_id: int, time) -> str:
    """Route chat query to the appropriate LLM based on channel settings"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    model = settings["model"]
    
    # Route to the appropriate query function
    if llm == "chatgpt":
        return chatGPTQuery.queryChatGPT(user_input, channel_id, time, model)
    elif llm == "gemini":
        return geminiQuery.queryGemini(user_input, channel_id, time, model)
    elif llm == "deepseek":
        return deepseekQuery.queryDeepSeek(user_input, channel_id, time, model)
    else:
        return f"Unknown LLM: {llm}"

def clear_history(channel_id: int):
    """Clear chat history for the active LLM"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    
    if llm == "chatgpt":
        chatGPTQuery.clearHistory(channel_id)
    elif llm == "gemini":
        geminiQuery.clearHistory(channel_id)
    elif llm == "deepseek":
        deepseekQuery.clearHistory(channel_id)

def change_prompt(channel_id: int, index: int, time) -> str:
    """Change prompt for the active LLM"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    
    if llm == "chatgpt":
        return chatGPTQuery.changePrompt(channel_id, index, time)
    elif llm == "gemini":
        return geminiQuery.changePrompt(channel_id, index, time)
    elif llm == "deepseek":
        return deepseekQuery.changePrompt(channel_id, index, time)
    else:
        return "Unknown LLM"

def show_prompt(channel_id: int) -> tuple:
    """Get current prompt for the active LLM. Returns (llm_name, prompt_content or None)"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    
    history = db.load_chat_history(str(channel_id), llm)
    if history and history[0]['role'] == 'system':
        return (llm, history[0]['content'])
    else:
        return (llm, None)

def set_custom_prompt(channel_id: int, custom_prompt: str):
    """Set custom prompt for the active LLM"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    
    db.clear_chat_history(str(channel_id), llm)
    db.save_chat_message(str(channel_id), llm, 'system', custom_prompt)
    
    # Clear cached session for Gemini
    if llm == "gemini":
        import geminiQuery
        if str(channel_id) in geminiQuery.chat_sessions:
            del geminiQuery.chat_sessions[str(channel_id)]

def set_llm(channel_id: int, llm_name: str) -> str:
    """Set the active LLM for a channel"""
    # Refresh available LLMs
    global AVAILABLE_LLMS
    AVAILABLE_LLMS = _get_available_llms()
    
    llm_name = llm_name.lower()
    
    if llm_name not in AVAILABLE_LLMS:
        return f"Unknown LLM: {llm_name}. Available: " + ", ".join(AVAILABLE_LLMS.keys())
    
    default_model = AVAILABLE_LLMS[llm_name]["default_model"]
    
    # Get current prompt for the new LLM
    new_llm_history = db.load_chat_history(str(channel_id), llm_name)
    prompt_info = ""
    if new_llm_history and new_llm_history[0]['role'] == 'system':
        # Find prompt name
        with open('llm_config.json', 'r', encoding='utf-8') as f:
            prompts = json.load(f)['prompts']
        for i, p in enumerate(prompts):
            if p['content'] == new_llm_history[0]['content']:
                prompt_info = f"\n📝 Active prompt: **{p['name']}** (index {i})"
                break
    else:
        prompt_info = "\n📝 Using default prompt (index 0)"
    
    db.set_channel_llm(str(channel_id), llm_name, default_model)
    
    return f"🔄 Switched to **{AVAILABLE_LLMS[llm_name]['name']}** with model `{default_model}`{prompt_info}\n💡 Note: Each LLM has its own separate chat history"

def set_model(channel_id: int, model_name: str) -> str:
    """Set the active model for the current LLM"""
    # Refresh available LLMs
    global AVAILABLE_LLMS
    AVAILABLE_LLMS = _get_available_llms()
    
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    
    if model_name not in AVAILABLE_LLMS[llm]["models"]:
        available = ", ".join(AVAILABLE_LLMS[llm]["models"])
        return f"Invalid model for {llm}. Available: {available}"
    
    db.set_channel_model(str(channel_id), model_name)
    return f"Model set to `{model_name}` for {AVAILABLE_LLMS[llm]['name']}"

def get_status(channel_id: int) -> str:
    """Get current LLM and model status"""
    settings = db.get_channel_settings(str(channel_id))
    llm = settings["llm"]
    model = settings["model"]
    listen = settings["listen_mode"]
    
    status = f"**Current Configuration:**\n"
    status += f"LLM: **{AVAILABLE_LLMS[llm]['name']}** (`{llm}`)\n"
    status += f"Model: `{model}`\n"
    status += f"Listen Mode: {'🟢 ON' if listen else '🔴 OFF'}"
    
    return status
