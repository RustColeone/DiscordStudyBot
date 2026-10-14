import asyncio
from pathlib import Path

from app.bot_types import BotAttachment, BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_draw_command
from providers import image_generation
from services.generation_activity import show_generation_typing


class DrawFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content == "$draw" or message.content.startswith("$draw ")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        command = parse_draw_command(message.content)
        if command.errors:
            return [BotResponse(text="\n".join(command.errors))]
        if command.status:
            return [BotResponse(text=self._format_status())]
        if command.list_models:
            catalogs, errors = await asyncio.to_thread(
                image_generation.discover_models,
                command.provider,
            )
            return [BotResponse(text=self._format_models(catalogs, errors))]
        if not command.prompt:
            return [BotResponse(text=(
                "Usage: `$draw [-p provider -m model] <description>`\n"
                "List current models with `$draw --models`."
            ))]
        try:
            return await self.generate(
                message,
                command.prompt,
                provider=command.provider,
                model=command.model,
            )
        except Exception as error:
            print(f"Image generation failed: {error}")
            return [BotResponse(text="Image generation failed. Check the bot logs for details.")]

    async def generate(
        self,
        message: IncomingMessage,
        description: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> HandlerResult:
        selection = image_generation.resolve_selection(provider, model)
        reference_images = (
            (message.metadata.get("referenced_image_attachments") or [])
            + (message.metadata.get("image_attachments") or [])
        )
        async with show_generation_typing(message):
            images = await asyncio.to_thread(
                image_generation.generate_image,
                description,
                reference_images,
                provider=selection.provider,
                model=selection.model,
            )

        display_name = "Gemini" if selection.provider == "gemini" else "OpenAI"
        responses = []
        for index, image in enumerate(images):
            detail = image.text or "I finished the image."
            caption = f"**{display_name}** - `{selection.model}`\n{detail}" if index == 0 else None
            image_path = Path(image.path)
            tool_image = {
                "url": image.path,
                "path": image.path,
                "filename": image.filename,
                "content_type": image.mime_type,
                "size": image_path.stat().st_size if image_path.is_file() else None,
                "origin": "generated",
                "description": description,
            }
            responses.append(BotResponse(
                text=caption,
                attachment=BotAttachment(
                    path=image.path,
                    filename=image.filename,
                    delete_after_send=True,
                ),
                metadata={
                    "generated_by": display_name,
                    "generated_image": True,
                    "generation_prompt": description,
                    "image_provider": selection.provider,
                    "image_model": selection.model,
                    "tool_images": [tool_image],
                },
            ))
        return responses

    @staticmethod
    def _format_status() -> str:
        default_provider = image_generation.normalize_provider(None)
        return (
            "**Draw configuration**\n"
            f"Default provider: `{default_provider}`\n"
            f"Gemini default: `{image_generation.default_model('gemini')}`\n"
            f"OpenAI default: `{image_generation.default_model('openai')}`"
        )

    @staticmethod
    def _format_models(catalogs: dict[str, list[str]], errors: dict[str, str]) -> str:
        lines = [
            "**Available draw models**",
            "Provider catalogs are queried live; verified current models remain visible if lookup is unavailable.",
        ]
        for provider in ["gemini", "openai"]:
            if provider not in catalogs and provider not in errors:
                continue
            default_model = image_generation.default_model(provider)
            display_name = "Gemini" if provider == "gemini" else "OpenAI"
            lines.append(f"\n**{display_name}** (default: `{default_model}`)")
            if catalogs.get(provider):
                lines.extend(f"- `{model}`" for model in catalogs[provider])
            else:
                lines.append("No image models returned by the provider.")
            if provider in errors:
                lines.append(f"Live lookup unavailable: {errors[provider]}.")
        return "\n".join(lines)