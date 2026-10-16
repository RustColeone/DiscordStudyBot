"""Math in Discord text replies (ported from the legacy rich content pass).

Inline `$...$` / `\\(...\\)` math becomes readable Unicode; display `$$...$$` / `\\[...\\]` math is
rendered to PNG with Matplotlib MathText (no LaTeX installation needed) on a fixed-width canvas, so
every equation shows at the same scale. Code fences and inline code are left untouched. Matplotlib
and pylatexenc are optional: without them, display math stays text and inline math is unchanged.
"""

from __future__ import annotations

import re
import tempfile
import threading
from pathlib import Path

CODE_PATTERN = re.compile(r"```.*?(?:```|\Z)|`[^`\n]+`", re.DOTALL)
DISPLAY_MATH_PATTERN = re.compile(r"(?<!\\)\$\$(?P<dollar>.*?)(?<!\\)\$\$|\\\[(?P<bracket>.*?)\\\]", re.DOTALL)
# Pandoc's rule: the opening `$` has a non-space on its right, the closing one a non-space on its
# left and no digit after it, so "$5 and $10" and "$help, $tools" are not math.
INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$(?![$\s])(?P<dollar>[^\n$]+?)(?<![\s\\])\$(?![$\d])|"
                                 r"\\\((?P<paren>.+?)\\\)")
SUPERSCRIPT = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")
SUBSCRIPT = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")

_latex_to_text = None


def _latex_text(text: str) -> str:
    global _latex_to_text
    if _latex_to_text is None:
        try:
            from pylatexenc.latex2text import LatexNodes2Text
            _latex_to_text = LatexNodes2Text(math_mode="text").latex_to_text
        except ImportError:
            _latex_to_text = str
    return _latex_to_text(text)


def _setting(config, name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(config.get(name, default)))
    except (TypeError, ValueError):
        return default


def looks_like_math(formula: str) -> bool:
    stripped = formula.strip()
    if not stripped:
        return False
    if re.search(r"[\\_^=<>+*/]|[α-ωΑ-Ω∞≤≥≪≫∑∫]", stripped):
        return True
    return bool(re.fullmatch(r"[A-Za-z](?:\d+)?", stripped))


def _script_text(value: str, table: dict, marker: str) -> str:
    translated = value.translate(table)
    return translated if all(ord(character) in table or character.isspace() for character in value) else f"{marker}{value}"


def inline_math_to_unicode(formula: str) -> str:
    converted = formula.strip()
    for _ in range(8):
        previous = converted
        converted = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", lambda m: f"({m.group(1)})/({m.group(2)})", converted)
        converted = re.sub(r"\\sqrt\s*\{([^{}]+)\}", lambda m: f"√({m.group(1)})", converted)
        if converted == previous:
            break
    converted = re.sub(r"\^\{([^{}]+)\}", lambda m: _script_text(m.group(1), SUPERSCRIPT, "^"), converted)
    converted = re.sub(r"_\{([^{}]+)\}", lambda m: _script_text(m.group(1), SUBSCRIPT, "_"), converted)
    converted = re.sub(r"\^([A-Za-z0-9+\-=()])", lambda m: _script_text(m.group(1), SUPERSCRIPT, "^"), converted)
    converted = re.sub(r"_([A-Za-z0-9+\-=()])", lambda m: _script_text(m.group(1), SUBSCRIPT, "_"), converted)
    converted = _latex_text(converted)
    converted = re.sub(r"\s*([=+×/<>≤≥≪≫→])\s*", r" \1 ", converted)
    return re.sub(r"[ \t]+", " ", converted).strip()


def _replace_inline_math(text: str) -> str:
    def replace(match: re.Match) -> str:
        formula = match.group("dollar") or match.group("paren") or ""
        return inline_math_to_unicode(formula) if looks_like_math(formula) else match.group(0)
    return INLINE_MATH_PATTERN.sub(replace, text)


def _append_text(parts: list[tuple[str, str]], text: str) -> None:
    if not text:
        return
    if parts and parts[-1][0] == "text":
        parts[-1] = ("text", parts[-1][1] + text)
    else:
        parts.append(("text", text))


def _append_outside_code(parts: list[tuple[str, str]], text: str) -> None:
    position = 0
    for match in DISPLAY_MATH_PATTERN.finditer(text):
        _append_text(parts, _replace_inline_math(text[position:match.start()]))
        formula = (match.group("dollar") or match.group("bracket") or "").strip()
        if formula:
            parts.append(("math", formula))
        position = match.end()
    _append_text(parts, _replace_inline_math(text[position:]))


def split_math(text: str) -> list[tuple[str, str]]:
    """`[("text", converted text) | ("math", display formula), ...]` in reading order."""
    parts: list[tuple[str, str]] = []
    position = 0
    for match in CODE_PATTERN.finditer(text):
        _append_outside_code(parts, text[position:match.start()])
        _append_text(parts, match.group(0))
        position = match.end()
    _append_outside_code(parts, text[position:])
    return parts or [("text", text)]


def normalize_display_formula(formula: str) -> str:
    normalized = formula.strip()
    normalized = re.sub(r"\\begin\{(?:equation\*?|aligned|align\*?)\}", "", normalized)
    normalized = re.sub(r"\\end\{(?:equation\*?|aligned|align\*?)\}", "", normalized)
    normalized = re.sub(r"\\[td]frac", r"\\frac", normalized)
    return normalized.replace(r"\!", "").replace("&", "").strip()


def fallback_text(formula: str) -> str:
    return f"```text\n{inline_math_to_unicode(formula)}\n```"


class MathRenderer:
    """Settings (this plugin's config): `math` (true), `math_width_px` (1440, min 640), `math_dpi`
    (180, min 72), `math_font_size_pt` (20, min 12), `math_min_height_px` (220, min 120),
    `math_max_images` (10 per reply; further equations are shown as text)."""

    def __init__(self, output_dir: Path, config=None):
        config = config or {}
        self.output_dir = Path(output_dir)
        self.enabled = bool(config.get("math", True))
        self.width_px = _setting(config, "math_width_px", 1440, 640)
        self.dpi = _setting(config, "math_dpi", 180, 72)
        self.font_size_pt = _setting(config, "math_font_size_pt", 20, 12)
        self.min_height_px = _setting(config, "math_min_height_px", 220, 120)
        self.max_images = _setting(config, "math_max_images", 10, 0)
        self._lock = threading.Lock()

    def render(self, formula: str) -> Path:
        """Render one display formula to a PNG file (blocking; run it in a thread)."""
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure

        lines = [line.strip() for line in re.split(r"\\\\(?:\[[^]]*\])?", normalize_display_formula(formula))
                 if line.strip()]
        if not lines:
            raise ValueError("display equation is empty")
        line_height_px = round(self.font_size_pt * self.dpi / 72 * 1.45)
        height_px = max(self.min_height_px, 96 + len(lines) * line_height_px)
        figure = Figure(figsize=(self.width_px / self.dpi, height_px / self.dpi), dpi=self.dpi, facecolor="#313338")
        for index, line in enumerate(lines):
            figure.text(0.5, 1.0 - ((index + 0.5) / len(lines)), f"${line}$", color="#f2f3f5",
                        fontsize=self.font_size_pt, horizontalalignment="center", verticalalignment="center")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="formula-", suffix=".png", dir=self.output_dir, delete=False) as file:
            path = Path(file.name)
        try:
            with self._lock:
                figure.savefig(path, format="png", dpi=self.dpi, facecolor=figure.get_facecolor())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path
