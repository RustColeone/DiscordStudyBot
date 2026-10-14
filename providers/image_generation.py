import os
from dataclasses import dataclass

from providers import gemini_image, openai_image


DEFAULT_PROVIDER = os.getenv("DRAW_DEFAULT_PROVIDER", "gemini").strip().lower()
PROVIDER_ALIASES = {
    "gemini": "gemini",
    "google": "gemini",
    "nano-banana": "gemini",
    "openai": "openai",
    "gpt": "openai",
}


@dataclass(frozen=True)
class DrawSelection:
    provider: str
    model: str


def normalize_provider(provider: str | None) -> str:
    name = (provider or DEFAULT_PROVIDER).strip().lower()
    normalized = PROVIDER_ALIASES.get(name)
    if normalized is None:
        supported = ", ".join(sorted({*PROVIDER_ALIASES.values()}))
        raise ValueError(f"Unknown image provider `{name}`. Choose: {supported}")
    return normalized


def default_model(provider: str) -> str:
    return gemini_image.IMAGE_MODEL if provider == "gemini" else openai_image.IMAGE_MODEL


def resolve_selection(provider: str | None = None, model: str | None = None) -> DrawSelection:
    selected_provider = normalize_provider(provider)
    selected_model = (model or default_model(selected_provider)).strip()
    if not selected_model:
        raise ValueError(f"No default model is configured for {selected_provider}")
    return DrawSelection(provider=selected_provider, model=selected_model)


def generate_image(
    prompt: str,
    reference_images: list[dict] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
):
    selection = resolve_selection(provider, model)
    implementation = gemini_image if selection.provider == "gemini" else openai_image
    return implementation.generate_image(prompt, reference_images, model=selection.model)


def discover_models(provider: str | None = None) -> tuple[dict[str, list[str]], dict[str, str]]:
    providers = [normalize_provider(provider)] if provider else ["gemini", "openai"]
    catalogs = {}
    errors = {}
    for name in providers:
        implementation = gemini_image if name == "gemini" else openai_image
        try:
            catalogs[name] = implementation.list_image_models()
        except Exception as error:
            print(f"{name.title()} image model discovery failed: {error}")
            fallback_models = set(implementation.KNOWN_IMAGE_MODELS)
            if implementation.IMAGE_MODEL:
                fallback_models.add(implementation.IMAGE_MODEL)
            catalogs[name] = sorted(fallback_models)
            detail = str(error).lower()
            errors[name] = "not configured" if "not configured" in detail else "provider query failed; check bot logs"
    return catalogs, errors