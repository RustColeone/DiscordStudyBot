from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import tempfile

from ddgs import DDGS
from PIL import Image, UnidentifiedImageError

from providers.image_types import LocatedImage
from services.vision import download_image_bytes


OUTPUT_DIR = Path(os.getenv("IMAGE_SEARCH_OUTPUT_DIR", "temp/searched_images"))
MAX_CANDIDATES = 8
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 1800


def locate_image(query: str) -> LocatedImage:
    results = DDGS(timeout=10).images(
        query,
        max_results=MAX_CANDIDATES,
        safesearch="moderate",
    )
    errors = []
    for result in results:
        image_url = str(result.get("image") or result.get("thumbnail") or "").strip()
        if not image_url.startswith(("https://", "http://")):
            continue
        try:
            image_bytes = download_image_bytes(image_url, MAX_DOWNLOAD_BYTES)
            return _normalize_result(result, image_url, image_bytes)
        except Exception as error:
            errors.append(str(error))
    detail = errors[-1] if errors else "the search returned no downloadable images"
    raise RuntimeError(f"Image search failed: {detail}")


def _normalize_result(result: dict, image_url: str, image_bytes: bytes) -> LocatedImage:
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            source.load()
            image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("result was not a supported image") from error

    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="reference-", suffix=".png", dir=OUTPUT_DIR)
    os.close(descriptor)
    try:
        image.save(path, format="PNG", optimize=True)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise

    title = str(result.get("title") or "Supplementary image").strip()
    source_url = str(result.get("url") or result.get("source") or image_url).strip()
    return LocatedImage(
        path=path,
        filename=Path(path).name,
        mime_type="image/png",
        title=title,
        source_url=source_url,
        image_url=image_url,
    )