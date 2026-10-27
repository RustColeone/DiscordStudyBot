"""The work a model does in a task's folder: look around, read, search, edit and run.

Every function takes the folder (a worktree, or the checkout itself for read-only questions) and returns
`(summary, data)`; a problem the model can fix (a path outside the folder, an edit whose text is not there)
raises ToolError with a message that says how. Outputs are cut to sizes a model can take in.
"""

from __future__ import annotations

from pathlib import Path

from . import system
from .git import execute, git

MAX_READ_LINES = 400
MAX_FILE_BYTES = 2_000_000
MAX_OUTPUT = 12_000
MAX_SEARCH = 120
HIDDEN = ".probe"  # the tester's scratch folder: never listed, searched or part of a change set


class ToolError(ValueError):
    pass


def inside(root: Path, path: str | None, links=(), follow: bool = True) -> Path:
    """`path` (relative to the folder, or absolute inside it) resolved, refusing anything outside it, in `.git`, or
    in a borrowed `link` folder. Without `follow`, a symlink itself is meant (its folder resolved, not it)."""
    root = Path(root).resolve()
    text = str(path or "").strip().replace("\\", "/")  # (a Windows-style path means the same)
    if text in ("", ".", "./"):
        return root
    candidate = Path(text)
    joined = candidate if candidate.is_absolute() else root / candidate
    full = joined.resolve() if follow else joined.parent.resolve() / joined.name
    if not full.is_relative_to(root) or full == root and not follow:
        raise ToolError(f"{text} is outside the project; paths are relative to the project's root")
    relative = full.relative_to(root)
    if any(part.lower() == ".git" for part in relative.parts):  # (any case: some file systems ignore it)
        raise ToolError("The .git folder is off limits; use git commands through run instead")
    if relative.parts and relative.parts[0] in set(links):
        raise ToolError(f"{relative.parts[0]} is borrowed from the checkout and cannot be read or changed here")
    return full


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(root).resolve()).as_posix() or "."


async def tracked(root: Path, links=()) -> list[str]:
    """Files git knows or would add (ignored ones left out), sorted."""
    listed = await git(root, "ls-files", "-co", "--exclude-standard", "-z", check=False)
    names = {name for name in listed.stdout.split("\0") if name}
    hidden = {*links, HIDDEN}
    return sorted(name for name in names if name.split("/", 1)[0] not in hidden and (Path(root) / name).exists())


async def list_files(root: Path, path: str = ".", depth: int = 3, links=()) -> tuple[str, dict]:
    base = inside(root, path, links)
    if base.is_file():
        return f"{relative(root, base)} is a file", {"entries": [relative(root, base)]}
    prefix = "" if base == Path(root).resolve() else relative(root, base) + "/"
    depth = max(1, min(int(depth or 3), 8))
    entries: list[str] = []
    folded: dict[str, int] = {}
    for name in await tracked(root, links):
        if not name.startswith(prefix):
            continue
        parts = name[len(prefix):].split("/")
        if len(parts) <= depth:
            entries.append(name)
        else:
            key = prefix + "/".join(parts[:depth]) + "/"
            folded[key] = folded.get(key, 0) + 1
    lines = entries + [f"{key} ({count} more files)" for key, count in sorted(folded.items())]
    lines.sort()
    shown = lines[:500]
    text = "\n".join(shown) + (f"\n… {len(lines) - 500} more" if len(lines) > 500 else "")
    return (text or "(no files)"), {"entries": shown, "count": len(entries) + sum(folded.values())}


def _text(path: Path) -> str:
    if not path.is_file():
        raise ToolError(f"{path.name} does not exist (or is a folder); list_files shows what is there")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolError(f"{path.name} is larger than {MAX_FILE_BYTES // 1_000_000} MB; search it instead")
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise ToolError(f"{path.name} is a binary file")
    return raw.decode("utf-8", "replace")


async def read_file(root: Path, path: str, start_line: int | None = None, end_line: int | None = None,
                    links=()) -> tuple[str, dict]:
    full = inside(root, path, links)
    lines = _text(full).splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
    width = len(str(max(end, 1)))
    body = "\n".join(f"{number:>{width}}| {lines[number - 1]}" for number in range(start, end + 1))
    more = f"\n… lines {end + 1}-{len(lines)} not shown; read again with start_line={end + 1}" if end < len(lines) else ""
    name = relative(root, full)
    return (f"{name} (lines {start}-{end} of {len(lines)})\n{body}{more}" if lines else f"{name} is empty"), {
        "path": name, "start": start, "end": end, "total": len(lines)}


async def search(root: Path, pattern: str, path: str = ".", ignore_case: bool = False, links=()) -> tuple[str, dict]:
    if not str(pattern or "").strip():
        raise ToolError("Give a pattern to search for")
    base = inside(root, path, links)
    flags = ["-i"] if ignore_case else []
    result = await git(root, "grep", "-n", "-I", "--untracked", "--exclude-standard", "-E", *flags, "-e", pattern,
                       "--", relative(root, base), *(f":(exclude){name}" for name in (*links, HIDDEN)), check=False)
    if result.code == 1:
        return f"No matches for {pattern!r}", {"matches": 0}
    if result.code != 0:
        raise ToolError(f"The search failed: {result.output[-400:]}")
    lines = [line if len(line) <= 300 else line[:300] + "…" for line in result.stdout.splitlines()]
    text = "\n".join(lines[:MAX_SEARCH]) + (f"\n… {len(lines) - MAX_SEARCH} more matches" if len(lines) > MAX_SEARCH
                                            else "")
    return text, {"matches": len(lines)}


def write_file(root: Path, path: str, content: str, links=(), append: bool = False) -> tuple[str, dict]:
    full = inside(root, path, links)
    if full.is_dir():
        raise ToolError(f"{relative(root, full)} is a folder")
    existed = full.exists()
    old = full.read_text(encoding="utf-8", errors="replace").count("\n") if existed and full.is_file() else 0
    full.parent.mkdir(parents=True, exist_ok=True)
    with full.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    name = relative(root, full)
    lines = full.read_text(encoding="utf-8", errors="replace").count("\n")
    verb = "Appended to" if append and existed else "Rewrote" if existed else "Created"
    return (f"{verb} {name} ({lines} lines now)"), {"path": name, "created": not existed, "lines": lines,
                                                    "old_lines": old}


def edit_file(root: Path, path: str, old: str, new: str, replace_all: bool = False, links=()) -> tuple[str, dict]:
    full = inside(root, path, links)
    text = _text(full)
    name = relative(root, full)
    if not old:
        raise ToolError("old is empty; to create a file use write_file")
    count = text.count(old)
    if count == 0 and "\r\n" in text and "\r\n" not in old:  # a CRLF file, edited with the lines read_file shows
        old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old)
    if count == 0:
        stripped = " ".join(old.split())
        hint = (" It matches when ignoring whitespace: copy the exact indentation from read_file."
                if stripped and stripped in " ".join(text.split()) else " Read the file again and copy the text exactly.")
        raise ToolError(f"The text to replace was not found in {name}.{hint}")
    if count > 1 and not replace_all:
        raise ToolError(f"The text to replace occurs {count} times in {name}; include more surrounding lines to make "
                        "it unique, or set replace_all")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    full.write_text(updated, encoding="utf-8", newline="")
    first = text.index(old)
    line = text.count("\n", 0, first) + 1
    lines = updated.splitlines()
    span = new.count("\n") + 1
    shown = range(max(1, line - 2), min(len(lines), line + span + 1) + 1)
    excerpt = "\n".join(f"{number:>4}| {lines[number - 1]}" for number in shown if number <= len(lines))
    return (f"Edited {name}{f' ({count} places)' if replace_all and count > 1 else ''} at line {line}:\n{excerpt}"), {
        "path": name, "line": line, "replacements": count if replace_all else 1,
        "added": new.count("\n") + 1, "removed": old.count("\n") + 1}


def delete_file(root: Path, path: str, links=()) -> tuple[str, dict]:
    full = inside(root, path, links, follow=False)  # a symlink is deleted itself, never the file it points at
    if full.is_symlink():
        name = relative(root, full.parent) + "/" + full.name if full.parent != Path(root).resolve() else full.name
        full.unlink()
        return f"Deleted the link {name}", {"path": name}
    if not full.is_file():
        raise ToolError(f"{relative(root, full) if full.exists() else path} is not a file")
    name = relative(root, full)
    full.unlink()
    return f"Deleted {name}", {"path": name}


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    """The start and (mostly) the end of a long output: tests report at the end."""
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 4], text[-(limit * 3 // 4):]
    skipped = text[len(head): len(text) - len(tail)].count("\n")
    return f"{head}\n… [{skipped} lines omitted] …\n{tail}"


async def run(root: Path, command: str, timeout_s: float = 120.0,
              isolation: system.Isolation | None = None) -> tuple[str, dict]:
    """One shell line in the folder (bash), stdout and stderr together, no input; `isolation` limits its reach."""
    if not str(command or "").strip():
        raise ToolError("Give a command to run")
    # No bytecode caches: an edit that keeps a file's size within the second of the last run would otherwise run the
    # old code (Python checks a .pyc by whole-second mtime and size).
    try:
        result = await execute([f"{{ {command}\n}} 2>&1"], Path(root), timeout_s=timeout_s, shell=True,
                               env={"PYTHONDONTWRITEBYTECODE": "1"}, clean=True, isolation=isolation)
    except OSError as error:  # isolation asked for where it cannot be had: refused, never run without it
        raise ToolError(f"Not run: {error}") from None
    output = clip(result.stdout.strip("\n"))
    if result.timed_out:
        status = f"timed out after {timeout_s:.0f}s (stopped)"
    else:
        status = f"exit code {result.code}"
    return (f"$ {command}\n{status} · {result.seconds:.1f}s\n{output or '(no output)'}"), {
        "command": command, "exit_code": result.code, "seconds": result.seconds, "timed_out": result.timed_out,
        "output": output}
