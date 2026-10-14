import os
from pathlib import Path
import tempfile

from google import genai
from google.genai import types

from providers.image_types import GeneratedImage
from services.config_service import optional_secret
from services.vision import download_image_bytes


IMAGE_MODEL = os.getenv(
    "DRAW_GEMINI_MODEL",
    os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
).strip()
KNOWN_IMAGE_MODELS = (
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
)
OUTPUT_DIR = Path(os.getenv(
    "DRAW_OUTPUT_DIR",
    os.getenv("GENERATED_IMAGE_DIR", "temp/generated_images"),
))

_api_key = optional_secret(os.getenv("GEMINI_API_KEY"))
client = genai.Client(api_key=_api_key) if _api_key else None


def list_image_models() -> list[str]:
    if client is None:
        raise RuntimeError("Gemini image generation is not configured")

    models = set(KNOWN_IMAGE_MODELS)
    if IMAGE_MODEL:
        models.add(IMAGE_MODEL)
    for item in client.models.list():
        name = str(getattr(item, "name", "")).removeprefix("models/")
        actions = getattr(item, "supported_actions", None) or []
        if name and ("-image" in name.lower() or name.lower().startswith("imagen-") or "generateImages" in actions):
            models.add(name)
    return sorted(models)


def generate_image(
    prompt: str,
    reference_images: list[dict] | None = None,
    model: str | None = None,
) -> list[GeneratedImage]:
    if client is None:
        raise RuntimeError("Gemini image generation is not configured")
    selected_model = (model or IMAGE_MODEL).strip()
    if not selected_model:
        raise RuntimeError("DRAW_GEMINI_MODEL is not configured")

    request_parts = [types.Part.from_text(text=prompt)]
    for image in reference_images or []:
        request_parts.append(types.Part.from_bytes(
            data=download_image_bytes(image.get("path") or image["url"]),
            mime_type=image["content_type"],
        ))

    response = client.models.generate_content(
        model=selected_model,
        contents=types.Content(role="user", parts=request_parts),
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(),
        ),
    )

    parts = list(getattr(response, "parts", None) or [])
    if not parts and getattr(response, "candidates", None):
        content = getattr(response.candidates[0], "content", None)
        parts = list(getattr(content, "parts", None) or [])

    response_text = "\n".join(
        part.text.strip()
        for part in parts
        if getattr(part, "text", None) and part.text.strip()
    )
    generated = []
    for part in parts:
        inline_data = getattr(part, "inline_data", None)
        image_bytes = getattr(inline_data, "data", None)
        mime_type = getattr(inline_data, "mime_type", None) or "image/png"
        if not image_bytes or not mime_type.startswith("image/"):
            continue
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(mime_type, ".img")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix="gemini-", suffix=suffix, dir=OUTPUT_DIR)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(image_bytes)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        generated.append(GeneratedImage(
            path=path,
            filename=Path(path).name,
            mime_type=mime_type,
            text=response_text,
        ))

    if not generated:
        reason = response_text or "the model returned no image"
        raise RuntimeError(f"Gemini could not generate an image: {reason}")
    return generated