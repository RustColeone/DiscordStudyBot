import base64
import io
import os
from pathlib import Path
import tempfile

from openai import OpenAI

from providers.image_types import GeneratedImage
from services.config_service import optional_secret
from services.vision import download_image_bytes


IMAGE_MODEL = os.getenv("DRAW_OPENAI_MODEL", "gpt-image-2.5-sunburst").strip()
KNOWN_IMAGE_MODELS = (
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
)
OUTPUT_DIR = Path(os.getenv(
    "DRAW_OUTPUT_DIR",
    os.getenv("GENERATED_IMAGE_DIR", "temp/generated_images"),
))
OUTPUT_FORMAT = os.getenv("DRAW_OPENAI_OUTPUT_FORMAT", "png").strip().lower()
QUALITY = os.getenv("DRAW_OPENAI_QUALITY", "auto").strip().lower()
SIZE = os.getenv("DRAW_OPENAI_SIZE", "1024x1024").strip().lower()

_api_key = optional_secret(os.getenv("OPENAI_API_KEY"))
client = OpenAI(api_key=_api_key) if _api_key else None


def list_image_models() -> list[str]:
    if client is None:
        raise RuntimeError("OpenAI image generation is not configured")
    models = {
        str(item.id)
        for item in client.models.list()
        if str(getattr(item, "id", "")).startswith("gpt-image-")
    }
    models.update(KNOWN_IMAGE_MODELS)
    if IMAGE_MODEL:
        models.add(IMAGE_MODEL)
    return sorted(models)


def generate_image(
    prompt: str,
    reference_images: list[dict] | None = None,
    model: str | None = None,
) -> list[GeneratedImage]:
    if client is None:
        raise RuntimeError("OpenAI image generation is not configured")
    selected_model = (model or IMAGE_MODEL).strip()
    if not selected_model:
        raise RuntimeError("DRAW_OPENAI_MODEL is not configured")

    if reference_images:
        streams = []
        for index, image in enumerate(reference_images):
            stream = io.BytesIO(download_image_bytes(image.get("path") or image["url"]))
            content_type = str(image.get("content_type") or "image/png").split(";", 1)[0]
            suffix = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(content_type, ".img")
            stream.name = f"reference-{index + 1}{suffix}"
            streams.append(stream)
        response = client.images.edit(
            model=selected_model,
            image=streams,
            prompt=prompt,
            output_format=OUTPUT_FORMAT,
            quality=QUALITY,
            size=SIZE,
        )
    else:
        response = client.images.generate(
            model=selected_model,
            prompt=prompt,
            output_format=OUTPUT_FORMAT,
            quality=QUALITY,
            size=SIZE,
        )

    generated = []
    suffix = ".jpg" if OUTPUT_FORMAT == "jpeg" else f".{OUTPUT_FORMAT}"
    mime_type = "image/jpeg" if OUTPUT_FORMAT == "jpeg" else f"image/{OUTPUT_FORMAT}"
    for item in response.data or []:
        encoded = getattr(item, "b64_json", None)
        if not encoded:
            continue
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix="openai-", suffix=suffix, dir=OUTPUT_DIR)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(base64.b64decode(encoded))
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        generated.append(GeneratedImage(
            path=path,
            filename=Path(path).name,
            mime_type=mime_type,
            text=str(getattr(item, "revised_prompt", "") or ""),
        ))

    if not generated:
        raise RuntimeError("OpenAI returned no image")
    return generated