from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure
from pylatexenc.latex2text import LatexNodes2Text

from app.bot_types import BotAttachment, BotResponse, HandlerResult


def _integer_setting(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


OUTPUT_DIR = Path(os.getenv("RICH_CONTENT_OUTPUT_DIR", "temp/rendered_math"))
MATH_IMAGE_WIDTH_PX = _integer_setting("RICH_CONTENT_MATH_WIDTH_PX", 1440, 640)
MATH_IMAGE_DPI = _integer_setting("RICH_CONTENT_MATH_DPI", 180, 72)
MATH_FONT_SIZE_PT = _integer_setting("RICH_CONTENT_MATH_FONT_SIZE_PT", 20, 12)
MATH_MIN_HEIGHT_PX = _integer_setting("RICH_CONTENT_MATH_MIN_HEIGHT_PX", 220, 120)
CODE_FENCE_PATTERN = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
DISPLAY_MATH_PATTERN = re.compile(
    r"(?<!\\)\$\$(?P<dollar>.*?)(?<!\\)\$\$|\\\[(?P<bracket>.*?)\\\]",
    re.DOTALL,
)
INLINE_MATH_PATTERN = re.compile(
    r"(?<!\\)\$(?!\$)(?P<dollar>[^\n$]+?)(?<!\\)\$(?!\$)|"
    r"\\\((?P<paren>.+?)\\\)",
)
SUPERSCRIPT = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")
SUBSCRIPT = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
LATEX_TO_TEXT = LatexNodes2Text(math_mode="text")


def expand_response(response: BotResponse) -> HandlerResult:
    if response.attachment is not None or not response.text:
        return [response]

    parts = _split_rich_text(response.text)
    if len(parts) == 1 and parts[0][0] == "text":
        converted = parts[0][1]
        if converted == response.text:
            return [response]
        return [BotResponse(text=converted, metadata=dict(response.metadata))]

    expanded = []
    for kind, value in parts:
        if kind == "text":
            if value.strip():
                expanded.append(BotResponse(text=value, metadata=dict(response.metadata)))
            continue
        try:
            attachment = render_display_math(value)
            expanded.append(BotResponse(
                attachment=attachment,
                metadata={**response.metadata, "rendered_math": True, "latex": value},
            ))
        except Exception as error:
            print(f"Math rendering failed: {error}")
            fallback = f"```text\n{inline_math_to_unicode(value)}\n```"
            expanded.append(BotResponse(text=fallback, metadata=dict(response.metadata)))
    return expanded or [response]


def _split_rich_text(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    position = 0
    for code_match in CODE_FENCE_PATTERN.finditer(text):
        _append_outside_math(parts, text[position:code_match.start()])
        _append_text(parts, code_match.group(0))
        position = code_match.end()
    _append_outside_math(parts, text[position:])
    return parts or [("text", text)]


def _append_outside_math(parts: list[tuple[str, str]], text: str) -> None:
    position = 0
    for math_match in DISPLAY_MATH_PATTERN.finditer(text):
        _append_text(parts, _replace_inline_math(text[position:math_match.start()]))
        formula = (math_match.group("dollar") or math_match.group("bracket") or "").strip()
        if formula:
            parts.append(("math", formula))
        position = math_match.end()
    _append_text(parts, _replace_inline_math(text[position:]))


def _append_text(parts: list[tuple[str, str]], text: str) -> None:
    if not text:
        return
    if parts and parts[-1][0] == "text":
        parts[-1] = ("text", parts[-1][1] + text)
    else:
        parts.append(("text", text))


def _replace_inline_math(text: str) -> str:
    def replace(match: re.Match) -> str:
        formula = match.group("dollar") or match.group("paren") or ""
        if not _looks_like_math(formula):
            return match.group(0)
        return inline_math_to_unicode(formula)

    return INLINE_MATH_PATTERN.sub(replace, text)


def _looks_like_math(formula: str) -> bool:
    stripped = formula.strip()
    if not stripped:
        return False
    if re.search(r"[\\_^=<>+*/]|[α-ωΑ-Ω∞≤≥≪≫∑∫]", stripped):
        return True
    return bool(re.fullmatch(r"[A-Za-z](?:\d+)?", stripped))


def inline_math_to_unicode(formula: str) -> str:
    converted = formula.strip()
    for _ in range(8):
        previous = converted
        converted = re.sub(
            r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}",
            lambda match: f"({match.group(1)})/({match.group(2)})",
            converted,
        )
        converted = re.sub(
            r"\\sqrt\s*\{([^{}]+)\}",
            lambda match: f"√({match.group(1)})",
            converted,
        )
        if converted == previous:
            break
    converted = re.sub(
        r"\^\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), SUPERSCRIPT, "^"),
        converted,
    )
    converted = re.sub(
        r"_\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), SUBSCRIPT, "_"),
        converted,
    )
    converted = re.sub(
        r"\^([A-Za-z0-9+\-=()])",
        lambda match: _script_text(match.group(1), SUPERSCRIPT, "^"),
        converted,
    )
    converted = re.sub(
        r"_([A-Za-z0-9+\-=()])",
        lambda match: _script_text(match.group(1), SUBSCRIPT, "_"),
        converted,
    )
    converted = LATEX_TO_TEXT.latex_to_text(converted)
    converted = re.sub(r"\s*([=+×/<>≤≥≪≫→])\s*", r" \1 ", converted)
    return re.sub(r"[ \t]+", " ", converted).strip()


def _script_text(value: str, table: dict, marker: str) -> str:
    translated = value.translate(table)
    return translated if all(ord(character) in table or character.isspace() for character in value) else f"{marker}{value}"


def render_display_math(formula: str) -> BotAttachment:
    normalized = _normalize_display_formula(formula)
    lines = [line.strip() for line in re.split(r"\\\\(?:\[[^]]*\])?", normalized) if line.strip()]
    if not lines:
        raise ValueError("display equation is empty")

    line_height_px = round(MATH_FONT_SIZE_PT * MATH_IMAGE_DPI / 72 * 1.45)
    height_px = max(MATH_MIN_HEIGHT_PX, 96 + len(lines) * line_height_px)
    figure = Figure(
        figsize=(MATH_IMAGE_WIDTH_PX / MATH_IMAGE_DPI, height_px / MATH_IMAGE_DPI),
        dpi=MATH_IMAGE_DPI,
        facecolor="#313338",
    )
    for index, line in enumerate(lines):
        y = 1.0 - ((index + 0.5) / len(lines))
        figure.text(
            0.5,
            y,
            f"${line}$",
            color="#f2f3f5",
            fontsize=MATH_FONT_SIZE_PT,
            horizontalalignment="center",
            verticalalignment="center",
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="formula-", suffix=".png", dir=OUTPUT_DIR)
    os.close(descriptor)
    try:
        figure.savefig(
            path,
            format="png",
            dpi=MATH_IMAGE_DPI,
            facecolor=figure.get_facecolor(),
        )
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return BotAttachment(
        path=path,
        filename=Path(path).name,
        delete_after_send=True,
    )


def _normalize_display_formula(formula: str) -> str:
    normalized = formula.strip()
    normalized = re.sub(r"\\begin\{(?:equation\*?|aligned|align\*?)\}", "", normalized)
    normalized = re.sub(r"\\end\{(?:equation\*?|aligned|align\*?)\}", "", normalized)
    normalized = re.sub(r"\\[td]frac", r"\\frac", normalized)
    normalized = normalized.replace(r"\!", "")
    normalized = normalized.replace("&", "")
    return normalized.strip()