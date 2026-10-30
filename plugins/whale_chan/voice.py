"""Her voice: replies spoken aloud and sent as a Discord voice message.

The default backend is OpenAI's speech API (`gpt-4o-mini-tts`, which takes a style instruction, so she sounds
like herself in every language), using OPENAI_API_KEY. `voice: {backend: cloned, reference: clip.wav}` speaks in the
voice of a short clip instead, through OpenRouter's speech API (OPENROUTER_API_KEY; Fish Audio's S2.1 makes a sentence
in two or three seconds; the clip goes with each request and nothing is stored there). Piper, a local voice that needs
no key, is an optional backend in `voice_piper.py`: nothing loads it unless `voice: {backend: piper}` is set, and the
file can be deleted to keep the module light.

She speaks through the `whale_chan.speak` capability, so the core's permissions apply and a model can choose to
send a voice note ("say goodnight to me"); the chat engine also uses it for her own first messages
(speak-up) and in conversations switched to voice with `/voice on`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from contracts import Capability, FileRef, Risk, ToolResult

VOICE_NAME = "voice-message.ogg"  # the bot sends files of this name as a Discord voice message
INSTRUCTIONS = ("A cute, playful, slightly tsundere young girl sending a voice message to someone close. Natural "
                "and warm, light, not too fast. Speak the language of the text.")


def speakable(text: str) -> str:
    """The words worth saying aloud: no code, links, Markdown or math."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\$\$.*?\$\$|\$[^$\n]+\$", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~|]+", "", text)
    return " ".join(text.split())


def _key(name: str) -> str:
    value = os.getenv(name, "").strip().strip("\"'")
    return "" if value.lower() in {"", "disabled", "none", "null"} else value


class OpenAIVoice:
    name = "openai"

    def __init__(self, config: Mapping[str, Any]):
        self.model = str(config.get("model", "gpt-4o-mini-tts"))
        self.voice = str(config.get("voice", "coral"))
        self.instructions = str(config.get("instructions", INSTRUCTIONS))
        self.speed = float(config.get("speed", 1.0))
        self.base_url = str(config.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.key_env = str(config.get("key_env", "OPENAI_API_KEY"))

    def ready(self) -> str:
        return "" if _key(self.key_env) else f"{self.key_env} is not set"

    def synthesize(self, text: str, path: Path) -> Path:
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "opus"}
        if self.instructions and not self.model.startswith("tts-1"):
            body["instructions"] = self.instructions
        if self.speed != 1.0:
            body["speed"] = self.speed
        request = urllib.request.Request(f"{self.base_url}/audio/speech", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {_key(self.key_env)}",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                path.write_bytes(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"OpenAI speech answered {error.code}: "
                               f"{error.read().decode('utf-8', 'replace')[:200]}") from None
        return path


class ClonedVoice:
    """A voice taken from a short clip (`reference`: a WAV or MP3 of clear speech, in her data folder unless the path
    is absolute), sent with each request to OpenRouter's speech API. `reference_text` is what the clip says, if known;
    a wrong one is worse than none. Keep the clip out of the repository: it is someone's voice."""

    name = "cloned"

    def __init__(self, config: Mapping[str, Any]):
        self.model = str(config.get("model", "fish-audio/s2.1-pro"))
        self.reference = Path(str(config.get("folder", "."))) / str(config.get("reference", "voice-reference.wav"))
        self.reference_text = str(config.get("reference_text", ""))
        self.base_url = str(config.get("base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        self.key_env = str(config.get("key_env", "OPENROUTER_API_KEY"))

    def ready(self) -> str:
        if not _key(self.key_env):
            return f"{self.key_env} is not set"
        if not self.reference.is_file():
            return f"her voice clip {self.reference} is missing"
        return "" if shutil.which("ffmpeg") else "ffmpeg is not installed"

    def _references(self) -> list[dict]:
        kind = mimetypes.guess_type(self.reference.name)[0] or "audio/wav"
        clip = base64.b64encode(self.reference.read_bytes()).decode()
        return [{"type": "input_audio", "input_audio": {"data": f"data:{kind};base64,{clip}"}},
                *([{"type": "text", "text": self.reference_text}] if self.reference_text else [])]

    def synthesize(self, text: str, path: Path) -> Path:
        body = {"model": self.model, "input": text, "response_format": "mp3", "input_references": self._references()}
        request = urllib.request.Request(f"{self.base_url}/audio/speech", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {_key(self.key_env)}",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                sound = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"OpenRouter speech answered {error.code}: "
                               f"{error.read().decode('utf-8', 'replace')[:200]}") from None
        # Discord's voice messages are Ogg Opus; the speech API gives MP3 or raw PCM.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "mp3", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "48k",
                        "-ac", "1", "-f", "ogg", str(path)], input=sound, check=True, capture_output=True, timeout=60)
        return path


def backend(config: Mapping[str, Any]):
    """The configured backend; Piper's module is imported only when asked for."""
    kind = str(config.get("backend", "openai"))
    if kind == "cloned":
        return ClonedVoice(config)
    if kind == "piper":
        from .voice_piper import PiperVoice  # optional: delete voice_piper.py and this is never reached
        return PiperVoice(dict(config.get("piper") or {}))
    return OpenAIVoice(config)


def capability(config: Mapping[str, Any]) -> Capability:
    voice = backend(config)
    limit = int(config.get("max_chars", 600))

    async def speak(arguments, tool) -> ToolResult:
        text = speakable(str(arguments.get("text", "")))
        if not text:
            return ToolResult.fail("There is nothing to say aloud in that text.", "empty")
        if len(text) > limit:
            return ToolResult.fail(f"Voice messages are for short things (at most {limit} characters).", "too_long")
        problem = voice.ready()
        if problem:
            return ToolResult.fail(f"Her voice is not set up: {problem}.", "not_configured")
        folder = Path(tool.workspace) / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(prefix="voice-", suffix=".ogg", dir=folder)
        os.close(descriptor)
        try:
            path = await asyncio.to_thread(voice.synthesize, text, Path(raw))
        except Exception as error:
            Path(raw).unlink(missing_ok=True)
            return ToolResult.fail(f"Speaking failed ({voice.name}): {type(error).__name__}: {error}"[:500],
                                   "speech_failed", retryable=True)
        ogg = path.suffix == ".ogg"  # Piper without ffmpeg gives WAV: an audio file, not a voice message
        file = FileRef(str(path), VOICE_NAME if ogg else "voice.wav", "audio/ogg" if ogg else "audio/wav",
                       path.stat().st_size)
        return ToolResult.ok(f"Said aloud as a voice message ({len(text)} characters).", files=(file,),
                             data={"backend": voice.name})

    return Capability(
        "whale_chan.speak",
        "Send a short voice message: your own words, spoken aloud in your voice. Use it when they ask to hear "
        "you, for a goodnight or a small surprise, or when a voice note fits better than text. A few sentences "
        "at most; the text is exactly what you say.",
        speak, parameters={"text": {"type": "string", "description": "What you say, as you would say it"}},
        required=("text",), risk=Risk.PREPARE, needs={"files"}, timeout_s=120)
