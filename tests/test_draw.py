import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.bot_types import IncomingMessage
from features.feature_draw import DrawFeature
from parsers.command_parsers import parse_draw_command
from providers import gemini_image, image_generation, openai_image


class GeminiImageTests(unittest.TestCase):
    def test_lists_only_image_capable_models(self):
        api_client = Mock()
        api_client.models.list.return_value = [
            SimpleNamespace(name="models/gemini-2.5-flash-image", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/imagen-4.0-generate-001", supported_actions=["generateImages"]),
            SimpleNamespace(name="models/gemini-3.8-flash-lite", supported_actions=["generateContent"]),
        ]

        with patch.object(gemini_image, "client", api_client):
            models = gemini_image.list_image_models()

        self.assertEqual(models, ["gemini-2.5-flash-image", "imagen-4.0-generate-001"])

    def test_generates_image_file_with_configured_model(self):
        api_client = Mock()
        api_client.models.generate_content.return_value = SimpleNamespace(parts=[
            SimpleNamespace(text="Here is the illustration.", inline_data=None),
            SimpleNamespace(
                text=None,
                inline_data=SimpleNamespace(data=b"png-data", mime_type="image/png"),
            ),
        ])

        with TemporaryDirectory() as directory, \
                patch.object(gemini_image, "client", api_client), \
                patch.object(gemini_image, "IMAGE_MODEL", "gemini-2.5-flash-image"), \
                patch.object(gemini_image, "OUTPUT_DIR", Path(directory)):
            generated = gemini_image.generate_image("Draw a moonlit library")

            self.assertEqual(Path(generated[0].path).read_bytes(), b"png-data")

        request = api_client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-2.5-flash-image")
        self.assertEqual(request["config"].response_modalities, ["IMAGE", "TEXT"])
        self.assertEqual(generated[0].text, "Here is the illustration.")
        self.assertTrue(generated[0].filename.endswith(".png"))

    def test_reference_image_is_sent_to_gemini(self):
        api_client = Mock()
        api_client.models.generate_content.return_value = SimpleNamespace(parts=[
            SimpleNamespace(
                text=None,
                inline_data=SimpleNamespace(data=b"result", mime_type="image/webp"),
            ),
        ])
        reference = {
            "url": "https://example.com/reference.png",
            "content_type": "image/png",
        }

        with TemporaryDirectory() as directory, \
                patch.object(gemini_image, "client", api_client), \
                patch.object(gemini_image, "OUTPUT_DIR", Path(directory)), \
                patch.object(gemini_image, "download_image_bytes", return_value=b"reference"):
            generated = gemini_image.generate_image("Redraw this in anime style", [reference])

        parts = api_client.models.generate_content.call_args.kwargs["contents"].parts
        self.assertEqual(parts[1].inline_data.data, b"reference")
        self.assertEqual(parts[1].inline_data.mime_type, "image/png")
        self.assertTrue(generated[0].filename.endswith(".webp"))

    def test_explicit_model_overrides_default(self):
        api_client = Mock()
        api_client.models.generate_content.return_value = SimpleNamespace(parts=[
            SimpleNamespace(
                text=None,
                inline_data=SimpleNamespace(data=b"result", mime_type="image/png"),
            ),
        ])

        with TemporaryDirectory() as directory, \
                patch.object(gemini_image, "client", api_client), \
                patch.object(gemini_image, "OUTPUT_DIR", Path(directory)):
            gemini_image.generate_image("Draw it", model="gemini-3.1-flash-image")

        self.assertEqual(
            api_client.models.generate_content.call_args.kwargs["model"],
            "gemini-3.1-flash-image",
        )


class OpenAIImageTests(unittest.TestCase):
    def test_lists_only_gpt_image_models(self):
        api_client = Mock()
        api_client.models.list.return_value = [
            SimpleNamespace(id="gpt-image-2.5-sunburst"),
            SimpleNamespace(id="gpt-image-2.5-flare"),
            SimpleNamespace(id="gpt-5.6-luna"),
        ]

        with patch.object(openai_image, "client", api_client):
            models = openai_image.list_image_models()

        self.assertEqual(models, ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])

    def test_generates_image_from_base64_response(self):
        api_client = Mock()
        api_client.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(b"png-data").decode("ascii"),
            revised_prompt="A refined prompt",
        )])

        with TemporaryDirectory() as directory, \
                patch.object(openai_image, "client", api_client), \
                patch.object(openai_image, "OUTPUT_DIR", Path(directory)):
            generated = openai_image.generate_image(
                "Draw a moonlit library",
                model="gpt-image-2.5-sunburst",
            )
            generated_bytes = Path(generated[0].path).read_bytes()

        self.assertEqual(generated_bytes, b"png-data")
        self.assertEqual(generated[0].text, "A refined prompt")
        request = api_client.images.generate.call_args.kwargs
        self.assertEqual(request["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(request["prompt"], "Draw a moonlit library")

    def test_reference_images_use_edit_endpoint(self):
        api_client = Mock()
        api_client.images.edit.return_value = SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(b"edited").decode("ascii"),
            revised_prompt=None,
        )])
        reference = {"url": "https://example.com/reference.png", "content_type": "image/png"}

        with TemporaryDirectory() as directory, \
                patch.object(openai_image, "client", api_client), \
                patch.object(openai_image, "OUTPUT_DIR", Path(directory)), \
                patch.object(openai_image, "download_image_bytes", return_value=b"reference"):
            generated = openai_image.generate_image("Edit this", [reference])
            generated_bytes = Path(generated[0].path).read_bytes()

        self.assertEqual(generated_bytes, b"edited")
        images = api_client.images.edit.call_args.kwargs["image"]
        self.assertEqual(images[0].getvalue(), b"reference")


class DrawParserTests(unittest.TestCase):
    def test_parses_provider_model_and_prompt(self):
        command = parse_draw_command(
            "$draw -p openai -m gpt-image-2.5-flare a moonlit library"
        )

        self.assertEqual(command.provider, "openai")
        self.assertEqual(command.model, "gpt-image-2.5-flare")
        self.assertEqual(command.prompt, "a moonlit library")
        self.assertEqual(command.errors, [])

    def test_model_requires_provider(self):
        command = parse_draw_command("$draw -m gemini-3.1-flash-image draw this")

        self.assertIn("--model requires --provider", command.errors)


class DrawFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.feature = DrawFeature()
        self.message = IncomingMessage(
            content="$draw a moonlit library",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
            metadata={"image_attachments": [{
                "url": "https://example.com/reference.png",
                "content_type": "image/png",
            }]},
        )

    async def test_draw_returns_gemini_attachment(self):
        generated = gemini_image.GeneratedImage(
            path="/tmp/generated.png",
            filename="generated.png",
            mime_type="image/png",
            text="Finished.",
        )
        with patch.object(gemini_image, "generate_image", return_value=[generated]) as generate:
            responses = await self.feature.handle(None, self.message)

        generate.assert_called_once_with(
            "a moonlit library",
            self.message.metadata["image_attachments"],
            model="gemini-2.5-flash-image",
        )
        self.assertEqual(
            responses[0].text,
            "**Gemini** - `gemini-2.5-flash-image`\nFinished.",
        )
        self.assertEqual(responses[0].attachment.path, "/tmp/generated.png")
        self.assertTrue(responses[0].attachment.delete_after_send)
        self.assertEqual(responses[0].metadata["generated_by"], "Gemini")

    async def test_draw_requires_a_description(self):
        self.message.content = "$draw"

        responses = await self.feature.handle(None, self.message)

        self.assertIn("$draw [-p provider -m model] <description>", responses[0].text)

    async def test_draw_uses_explicit_openai_model(self):
        self.message.content = "$draw -p openai -m gpt-image-2.5-flare a moonlit library"
        generated = gemini_image.GeneratedImage(
            path="/tmp/generated.png",
            filename="generated.png",
            mime_type="image/png",
        )
        with patch.object(image_generation, "generate_image", return_value=[generated]) as generate:
            responses = await self.feature.handle(None, self.message)

        generate.assert_called_once_with(
            "a moonlit library",
            self.message.metadata["image_attachments"],
            provider="openai",
            model="gpt-image-2.5-flare",
        )
        self.assertIn("**OpenAI**", responses[0].text)
        self.assertEqual(responses[0].metadata["image_provider"], "openai")

    async def test_draw_lists_live_models(self):
        self.message.content = "$draw --models"
        catalogs = {
            "gemini": ["gemini-2.5-flash-image"],
            "openai": ["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"],
        }
        with patch.object(image_generation, "discover_models", return_value=(catalogs, {})) as discover:
            responses = await self.feature.handle(None, self.message)

        discover.assert_called_once_with(None)
        self.assertIn("gemini-2.5-flash-image", responses[0].text)
        self.assertIn("gpt-image-2.5-flare", responses[0].text)

    async def test_direct_draw_reports_generation_failure(self):
        with patch.object(
            gemini_image,
            "generate_image",
            side_effect=RuntimeError("provider unavailable"),
        ):
            responses = await self.feature.handle(None, self.message)

        self.assertEqual(
            responses[0].text,
            "Image generation failed. Check the bot logs for details.",
        )


if __name__ == "__main__":
    unittest.main()