"""Image generation with OpenAI or Gemini (ported from providers/openai_image.py and gemini_image.py).

The SDK clients are private to this extension; images are written into the run's workspace.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
from pathlib import Path

from contracts import Capability, Contributions, Extension, FileRef, Risk, ToolResult

SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_REFERENCE_BYTES = 20 * 1024 * 1024


def _key(name: str) -> str:
    value = os.getenv(name, "").strip().strip("\"'")
    return "" if value.lower() in {"", "disabled", "none", "null"} else value


class ImageGenerator:
    def __init__(self, config):
        self.default_provider = str(config.get("default_provider", "gemini"))
        self.models = {"openai": str(config.get("openai_model", "gpt-image-2.5-sunburst")),
                       "gemini": str(config.get("gemini_model", "gemini-2.5-flash-image"))}
        self.openai_options = {"size": str(config.get("size", "1024x1024")),
                               "quality": str(config.get("quality", "auto")),
                               "output_format": str(config.get("output_format", "png"))}
        self._clients = {}

    def configured(self) -> list[str]:
        return [name for name, env in (("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")) if _key(env)]

    def _client(self, provider: str):
        if provider not in self._clients:
            if provider == "openai":
                from openai import OpenAI
                key = _key("OPENAI_API_KEY")
                self._clients[provider] = OpenAI(api_key=key) if key else None
            else:
                from google import genai
                key = _key("GEMINI_API_KEY")
                self._clients[provider] = genai.Client(api_key=key) if key else None
        client = self._clients[provider]
        if client is None:
            raise LookupError(f"{provider} image generation is not configured (no API key)")
        return client

    def generate(self, provider: str, prompt: str, references: list[FileRef], directory: Path) -> tuple[list[Path], str]:
        if provider == "openai":
            return self._openai(prompt, references, directory)
        return self._gemini(prompt, references, directory)

    def _openai(self, prompt, references, directory):
        client, options = self._client("openai"), self.openai_options
        if references:
            streams = []
            for index, file in enumerate(references):
                stream = io.BytesIO(Path(file.path).read_bytes())
                stream.name = f"reference-{index + 1}{SUFFIXES.get(file.mime, '.png')}"
                streams.append(stream)
            response = client.images.edit(model=self.models["openai"], image=streams, prompt=prompt, **options)
        else:
            response = client.images.generate(model=self.models["openai"], prompt=prompt, **options)
        fmt = options["output_format"]
        suffix = ".jpg" if fmt == "jpeg" else f".{fmt}"
        paths, note = [], ""
        for item in response.data or []:
            encoded = getattr(item, "b64_json", None)
            if encoded:
                paths.append(self._write(directory, "openai-", suffix, base64.b64decode(encoded)))
                note = str(getattr(item, "revised_prompt", "") or note)
        if not paths:
            raise RuntimeError("OpenAI returned no image")
        return paths, note

    def _gemini(self, prompt, references, directory):
        from google.genai import types
        client = self._client("gemini")
        parts = [types.Part.from_text(text=prompt)]
        for file in references:
            parts.append(types.Part.from_bytes(data=Path(file.path).read_bytes(), mime_type=file.mime or "image/png"))
        response = client.models.generate_content(
            model=self.models["gemini"], contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"], image_config=types.ImageConfig()))
        returned = list(getattr(response, "parts", None) or [])
        if not returned and getattr(response, "candidates", None):
            returned = list(getattr(getattr(response.candidates[0], "content", None), "parts", None) or [])
        note = "\n".join(part.text.strip() for part in returned if getattr(part, "text", None) and part.text.strip())
        paths = []
        for part in returned:
            inline = getattr(part, "inline_data", None)
            data, mime = getattr(inline, "data", None), getattr(inline, "mime_type", None) or "image/png"
            if data and mime.startswith("image/"):
                paths.append(self._write(directory, "gemini-", SUFFIXES.get(mime, ".img"), data))
        if not paths:
            raise RuntimeError(f"Gemini could not generate an image: {note or 'no image returned'}")
        return paths, note

    @staticmethod
    def _write(directory: Path, prefix: str, suffix: str, data: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        return Path(path)


def setup(context) -> Contributions:
    generator = ImageGenerator(context.config)

    async def generate(arguments, tool) -> ToolResult:
        provider = arguments.get("provider") or generator.default_provider
        references = []
        if arguments.get("use_attached_images"):
            references = [file for file in tool.files if (file.mime or "").startswith("image/")
                          and Path(file.path).is_file() and Path(file.path).stat().st_size <= MAX_REFERENCE_BYTES]
            if not references:
                return ToolResult.fail("No attached image is available to edit.", "no_reference_image")
        await tool.progress(f"Drawing with {provider}...")
        try:
            paths, note = await asyncio.to_thread(generator.generate, provider, arguments["prompt"], references,
                                                  tool.workspace)
        except LookupError as error:
            return ToolResult.fail(str(error), "not_configured")
        except Exception as error:
            return ToolResult.fail(f"{provider} image generation failed: {type(error).__name__}: {error}"[:800],
                                   "generation_failed", retryable=True)
        files = tuple(FileRef(str(path), path.name, "image/" + path.suffix.lstrip(".").replace("jpg", "jpeg"),
                              path.stat().st_size) for path in paths)
        summary = f"Generated {len(files)} image(s) with {provider} ({generator.models[provider]})."
        return ToolResult.ok(summary + (f" Model note: {note[:300]}" if note else ""), files=files,
                             data={"provider": provider, "model": generator.models[provider]})

    return Contributions(capabilities=[Capability(
        "images.generate",
        "Draw, illustrate, render or edit an image from a description and deliver it. Use it whenever the user wants "
        "an image; do not describe one instead. Gemini is the default; use provider=openai only when the user asks for "
        "GPT, ChatGPT, OpenAI or DALL-E. Set use_attached_images to edit or restyle images the user attached.",
        generate,
        parameters={"prompt": {"type": "string", "description": "Complete visual description and style"},
                    "provider": {"type": "string", "enum": ["gemini", "openai"]},
                    "use_attached_images": {"type": "boolean"}},
        required=("prompt",), risk=Risk.PREPARE, needs={"files"}, command="draw", command_argument="prompt",
        timeout_s=300)])


EXTENSION = Extension("images", setup, contracts="1", version="1", description="Image generation (Gemini, OpenAI)")
