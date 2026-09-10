from urllib.parse import urlparse


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def image_metadata(attachment):
    url = getattr(attachment, "url", None)
    if not url or not str(url).startswith(("https://", "http://")):
        return None

    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].lower()
    filename = getattr(attachment, "filename", None) or urlparse(str(url)).path.rsplit("/", 1)[-1]
    extension = "." + filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if content_type not in SUPPORTED_IMAGE_TYPES and extension not in SUPPORTED_IMAGE_EXTENSIONS:
        return None

    size = getattr(attachment, "size", None)
    if size is not None and size > MAX_IMAGE_BYTES:
        return None
    return {
        "url": str(url),
        "filename": filename,
        "content_type": content_type or _content_type_for_extension(extension),
        "size": size,
    }


def supports_image_input(provider: str, model: str) -> bool:
    normalized = model.lower()
    if provider == "deepseek":
        return normalized in {
            "deepseek-flash",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        }
    if provider == "gemini":
        return normalized.startswith(("gemini-", "gemma-3", "gemma-3n"))
    if provider == "chatgpt":
        return normalized.startswith(("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5")) or normalized == "gpt-4-turbo"
    return False


def openai_image_content(text: str, images: list[dict]):
    if not images:
        return text
    return [
        {"type": "text", "text": text},
        *[
            {
                "type": "image_url",
                "image_url": {"url": image["url"], "detail": "auto"},
            }
            for image in images
        ],
    ]


def persistent_image_text(text: str, images: list[dict]) -> str:
    if not images:
        return text
    names = ", ".join(image.get("filename") or "image" for image in images)
    return f"{text}\n[Attached images: {names}]"


def _content_type_for_extension(extension: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(extension, "application/octet-stream")