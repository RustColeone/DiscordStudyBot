from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

from app.bot_types import BotResponse
from services import rich_content


class RichContentTests(unittest.TestCase):
    def test_inline_math_becomes_readable_unicode(self):
        responses = rich_content.expand_response(BotResponse(
            text=r"Use $Z_{out} \ll Z_{in}$ and $x^2 \to \infty$.",
        ))

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].text, "Use Zₒᵤₜ ≪ Zᵢₙ and x² → ∞.")

    def test_currency_and_code_fences_are_not_treated_as_math(self):
        text = "It costs $5 and $10.\n```python\nprice = '$x^2'\n```"

        responses = rich_content.expand_response(BotResponse(text=text))

        self.assertEqual(responses[0].text, text)

    def test_display_math_preserves_text_image_text_order(self):
        fake_attachment = rich_content.BotAttachment("/tmp/formula.png", "formula.png", True)
        with patch.object(rich_content, "render_display_math", return_value=fake_attachment):
            responses = rich_content.expand_response(BotResponse(
                text="Before.\n\n$$Z_L = Z_S^*$$\n\nAfter.",
                metadata={"speaker": "Luna"},
            ))

        self.assertEqual([response.text for response in responses], ["Before.\n\n", None, "\n\nAfter."])
        self.assertIs(responses[1].attachment, fake_attachment)
        self.assertEqual(responses[1].metadata["latex"], "Z_L = Z_S^*")

    def test_adjacent_display_math_omits_whitespace_only_messages(self):
        fake_attachment = rich_content.BotAttachment("/tmp/formula.png", "formula.png", True)
        with patch.object(rich_content, "render_display_math", return_value=fake_attachment):
            responses = rich_content.expand_response(BotResponse(
                text="Formulas:\n$$x=1$$\n$$y=2$$\nDone.",
            ))

        self.assertEqual(
            [(response.text, response.attachment is not None) for response in responses],
            [("Formulas:\n", False), (None, True), (None, True), ("\nDone.", False)],
        )
        self.assertTrue(all(response.text is None or response.text.strip() for response in responses))

    def test_display_math_renders_nonempty_png(self):
        attachment = rich_content.render_display_math(
            r"V_{in}=V_{oc}\frac{Z_{in}}{Z_{out}+Z_{in}}"
        )
        self.addCleanup(lambda: Path(attachment.path).unlink(missing_ok=True))

        self.assertTrue(Path(attachment.path).is_file())
        self.assertGreater(Path(attachment.path).stat().st_size, 1000)
        self.assertEqual(Path(attachment.path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_display_math_normalizes_mathtext_fraction_and_spacing_variants(self):
        attachment = rich_content.render_display_math(
            r"\sin\!\left(\tfrac{\pi}{2}-\alpha\right)=\cos\alpha"
        )
        self.addCleanup(lambda: Path(attachment.path).unlink(missing_ok=True))

        self.assertGreater(Path(attachment.path).stat().st_size, 1000)

    def test_display_math_uses_consistent_canvas_scale(self):
        short = rich_content.render_display_math(r"x=1")
        long = rich_content.render_display_math(
            r"V_{in}=V_{oc}\frac{Z_{in}}{Z_{out}+Z_{in}},\qquad Z_L=Z_S^*"
        )
        self.addCleanup(lambda: Path(short.path).unlink(missing_ok=True))
        self.addCleanup(lambda: Path(long.path).unlink(missing_ok=True))

        with Image.open(short.path) as short_image, Image.open(long.path) as long_image:
            self.assertEqual(short_image.size, long_image.size)
            self.assertEqual(short_image.width, rich_content.MATH_IMAGE_WIDTH_PX)
            self.assertAlmostEqual(short_image.info["dpi"][0], rich_content.MATH_IMAGE_DPI, delta=0.1)
            self.assertAlmostEqual(long_image.info["dpi"][0], rich_content.MATH_IMAGE_DPI, delta=0.1)


if __name__ == "__main__":
    unittest.main()