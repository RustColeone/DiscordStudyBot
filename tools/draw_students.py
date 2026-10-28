"""Draw the students' art with OpenAI's image model (extensions/students/art.yml), from each one's avatar.png.

    python tools/draw_students.py                          # everything not drawn yet
    python tools/draw_students.py --students claude --kinds stage-3 --force   # draw one again after tuning it
    python tools/draw_students.py --dry-run                # what it would draw, and the prompts

Each picture goes to extensions/students/<name>/art/<kind>.webp (portraits at most 768×1152, chibis 512×512, so the
repository stays small); the students module serves every picture in art/ by its file name. Her model ladder
(`ladders:`) draws only the tiers she needs, never `chibi` twice. The key is OPENAI_API_KEY, from the environment or
the .env file named by --env-file (only that key is read from it). Every call is printed with its size, quality and
the tokens it used, so the cost is known.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STUDENTS = ROOT / "extensions" / "students"
LIMITS = {"chibi": (512, 512)}  # kind → the largest size kept; anything else: a portrait
PORTRAIT = (768, 1152)


def load_key(env_file: Path | None) -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip().strip("\"'")
    if key or env_file is None or not env_file.is_file():
        return key
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def wanted(plan: dict, name: str) -> list[str]:
    """The kinds she gets: every kind but the model tiers, and the model tiers her ladder names."""
    tiers = {f"model-{tier}" for tier in (plan.get("ladders") or {}).get(name, {}).values() if tier != "chibi"}
    return [kind for kind in plan["kinds"] if not kind.startswith("model-") or kind in tiers]


def prompt_for(plan: dict, name: str, display: str, kind: str) -> str:
    who = {"name": display, **{key: str(value) for key, value in (plan["students"].get(name) or {}).items()}}
    who.setdefault("look", "")
    who.setdefault("motif", "")
    who.setdefault("role", "")
    return " ".join([plan["style"].format(**who), plan["kinds"][kind]["prompt"].format(**who)])


def keep(data: bytes, kind: str, target: Path) -> int:
    """The drawn PNG as a WebP no bigger than the kind needs; returns its size in bytes."""
    from PIL import Image
    image = Image.open(io.BytesIO(data))
    image.thumbnail(LIMITS.get(kind, PORTRAIT), Image.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "WEBP", quality=86, method=6)
    return target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--students", default="", help="comma-separated names (default: every student)")
    parser.add_argument("--kinds", default="", help="comma-separated kinds (default: every kind she gets)")
    parser.add_argument("--force", action="store_true", help="draw again what is already drawn")
    parser.add_argument("--dry-run", action="store_true", help="only say what would be drawn")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="where OPENAI_API_KEY is (if not in the environment)")
    arguments = parser.parse_args()
    plan = yaml.safe_load((STUDENTS / "art.yml").read_text(encoding="utf-8"))
    names = [item for item in arguments.students.split(",") if item] or list(plan["students"])
    only = {item for item in arguments.kinds.split(",") if item}
    jobs = []
    for name in names:
        folder = STUDENTS / name
        meta = yaml.safe_load((folder / "student.yml").read_text(encoding="utf-8")) if (folder / "student.yml").is_file() else None
        if meta is None or not (folder / "avatar.png").is_file():
            print(f"{name}: no student.yml or avatar.png, skipped", file=sys.stderr)
            continue
        for kind in wanted(plan, name):
            target = folder / "art" / f"{kind}.webp"
            if (only and kind not in only) or (target.exists() and not arguments.force):
                continue
            jobs.append((name, str(meta.get("display_name") or name), kind, target))
    if arguments.dry_run:
        for name, display, kind, target in jobs:
            options = plan["kinds"][kind]
            print(f"{name}/{kind}: {options.get('size')} {options.get('quality')}\n  {prompt_for(plan, name, display, kind)}\n")
        print(f"{len(jobs)} picture(s) to draw")
        return 0
    key = load_key(Path(arguments.env_file) if arguments.env_file else None)
    if not key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=300)
    totals = {"pictures": 0, "input_tokens": 0, "output_tokens": 0}
    for index, (name, display, kind, target) in enumerate(jobs, 1):
        options = dict(plan["kinds"][kind])
        reference = io.BytesIO((STUDENTS / name / "avatar.png").read_bytes())
        reference.name = "reference.png"
        extra = {"background": options["background"]} if options.get("background") else {}
        started = time.monotonic()
        try:
            response = client.images.edit(model=plan["model"], image=[reference], prompt=prompt_for(plan, name, display, kind),
                                          size=options.get("size", "1024x1024"), quality=options.get("quality", "medium"),
                                          output_format="png", **extra)
        except Exception as error:  # say it and go on with the rest (an empty account stops everything after it too)
            print(f"[{index}/{len(jobs)}] {name}/{kind}: FAILED {type(error).__name__}: {str(error)[:300]}", flush=True)
            if "insufficient_quota" in str(error) or "billing" in str(error).lower():
                return 3
            continue
        data = next((base64.b64decode(item.b64_json) for item in response.data or () if getattr(item, "b64_json", None)), None)
        if data is None:
            print(f"[{index}/{len(jobs)}] {name}/{kind}: no picture came back", flush=True)
            continue
        size = keep(data, kind, target)
        usage = getattr(response, "usage", None)
        used = {"input_tokens": getattr(usage, "input_tokens", 0) or 0, "output_tokens": getattr(usage, "output_tokens", 0) or 0}
        totals["pictures"] += 1
        for field in ("input_tokens", "output_tokens"):
            totals[field] += used[field]
        print(f"[{index}/{len(jobs)}] {name}/{kind}: {options.get('size')} {options.get('quality')} · "
              f"{used['input_tokens']} in / {used['output_tokens']} out · {time.monotonic() - started:.0f}s · "
              f"{size // 1024} KB → {target.relative_to(ROOT)}", flush=True)
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
