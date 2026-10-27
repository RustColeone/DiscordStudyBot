"""Which shell lines run in a worktree without asking: tests, linters, builds and looking around.

A line is split into pieces at `;`, `&&`, `||`, `|`, `|&`, `&` and newlines (outside quotes, backslash escapes
respected); it runs on its own only when every piece is allowed:

- it starts with an allowed program (and subcommand, for git, npm and the like), after leading `VAR=value`
  assignments and wrappers that only run what follows (`timeout 60`, `env`, `nice`, `npx`, `uv run`), which is
  then checked the same way;
- no word points outside the folder or at secrets: absolute paths, `..`, `~`, `$VAR`/`${VAR}` (unknown values),
  attached forms included (`>../out`, `2>/tmp/x`, `-o../x`, `--out=../x`), Windows forms too (`C:\\`, `..\\`);
- programs that run other programs or write files are allowed only in their reading forms: `find` without
  `-exec`/`-delete`/`-ok`/`-fprint`, `sed` without `-i` and `w`/`e` commands, `awk` without `system(`, pipes or
  redirections inside its script, `git branch` only listing.

Command substitution (`$(…)`, backticks), process substitution and heredocs with an unquoted end marker always ask,
since what they run cannot be read off the line. A heredoc whose marker is quoted (`python3 - <<'PY' … PY`) is only
text for the program before it (the shell expands nothing in it), so the line is checked without its text.
Redirecting to `/dev/null` (`2>/dev/null`) stays inside. `python3 -c "…"` counts as running Python, like `python3 file.py`: the list states intent (build,
test, look), it is not a sandbox. A project's own test line, and lines the creator lists in `allow:`, run as written,
alone or as one piece among others.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Iterable

# Programs that only read, build or test inside the current folder (`cd` and `source` take an inside path).
PROGRAMS = {
    "python", "python3", "pytest", "py.test", "node", "deno", "bun", "ruby", "php", "java", "javac", "tsc", "jest",
    "vitest", "mocha", "ruff", "flake8", "pylint", "mypy", "pyright", "black", "isort", "eslint", "prettier",
    "shellcheck", "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "sort", "uniq", "diff", "cmp", "echo",
    "printf", "pwd", "which", "file", "stat", "du", "tree", "true", "false", "test", "[", "awk", "cut", "tr",
    "basename", "dirname", "realpath", "date", "sleep", "jq", "nl", "column", "less", "more", "cd", "source", ".",
    "sed",
}
# Programs allowed only with some subcommands.
SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "ls-files", "grep", "blame", "rev-parse", "branch", "describe",
            "shortlog", "cat-file", "ls-tree"},
    "npm": {"test", "run", "ls", "ci"}, "pnpm": {"test", "run", "ls"}, "yarn": {"test", "run"},
    "cargo": {"build", "test", "check", "fmt", "clippy", "run", "bench", "doc"},
    "go": {"build", "test", "vet", "fmt", "run", "list"},
    "make": {"test", "check", "lint", "build", "all"},
    "pip": {"list", "show", "freeze", "check"}, "pip3": {"list", "show", "freeze", "check"},
}
# Wrappers that run the rest of the line: how many words of their own they take before it (options aside).
WRAPPERS = {"timeout": 1, "nice": 0, "env": 0, "npx": 0, "command": 0}
GIT_LISTING = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "--show-current", "--verbose", "--no-color",
               "--color", "--merged", "--no-merged", "--contains"}
DYNAMIC = re.compile(r"\$\(|`|<\(|>\(|<<")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|<)(?:&\d)?")          # >, >>, 2>, &>, <, 2>&1
VARIABLE = re.compile(r"\$\{?[A-Za-z_]")                         # $HOME, ${HOME} (not $? or $1)


def pieces(line: str) -> list[str]:
    """The line split at `;`, `&&`, `||`, `|`, `|&`, `&` and newlines outside quotes (`python3 -c "a; b"` is one
    piece; `2>&1` and `&>` are redirections, not separators); backslash escapes stay with what they escape."""
    found, current, quote, index, text = [], [], "", 0, line.strip()
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                current.append(text[index + 1])
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\" and index + 1 < len(text):  # an escaped character (a quote, a separator): just a character
            current.append(text[index:index + 2])
            index += 2
            continue
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in ";\n|" or (char == "&" and not (current and current[-1] in "<>")
                                 and text[index + 1:index + 2] != ">"):
            found.append("".join(current))
            current = []
            index += 2 if text[index:index + 2] in ("&&", "||", "|&") else 1
            continue
        else:
            current.append(char)
        index += 1
    found.append("".join(current))
    return [piece.strip() for piece in found if piece.strip()]


QUOTED_HEREDOC = re.compile(r"<<(-?)\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2")
NOWHERE = "/dev/null"  # the one absolute path a line may name: output thrown away


def without_heredocs(line: str) -> str | None:
    """`line` with each quoted heredoc's text taken out (it is data for the program before it); None when an end
    marker never comes."""
    while True:
        match = QUOTED_HEREDOC.search(line)
        if match is None:
            return line
        tabs, marker = match.group(1) == "-", match.group(3)
        start = line.find("\n", match.end())
        if start < 0:
            return None
        rest = line[start + 1:].split("\n")
        for index, body_line in enumerate(rest):
            if (body_line.lstrip("\t") if tabs else body_line) == marker:
                after = "\n".join(rest[index + 1:])
                line = line[:match.start()] + line[match.end():start] + ("\n" + after if after else "")
                break
        else:
            return None


def allowed(line: str, extra: Iterable[str] = ()) -> bool:
    """Whether every piece of `line` is allowed. `extra`: lines allowed as written (a project's own test line, or
    ones the creator listed), alone or as one piece of the line."""
    line, extra = line.strip(), [item.strip() for item in extra if item.strip()]
    if not line:
        return False
    if line in extra:
        return True
    line = without_heredocs(line)
    if line is None:
        return False
    if DYNAMIC.search(line):
        return False
    return all(_piece_allowed(piece, extra) for piece in pieces(line))


def _piece_allowed(piece: str, extra: list[str]) -> bool:
    if piece in extra:
        return True
    try:
        words = shlex.split(piece)
    except ValueError:
        return False
    kept, target_next = [], False
    for word in words:  # every word stays inside: redirection targets and option values included
        redirect = REDIRECT.match(word)
        target = word[redirect.end():] if redirect else word
        if target_next or redirect:  # a redirection's target: inside, or thrown away
            if target and target != NOWHERE and _outside(target):
                return False
            target_next = bool(redirect) and not target and not word.endswith("&1") and not word.endswith("&2")
            continue
        if _outside(word):
            return False
        kept.append(word)
    words = kept
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)  # FOO=1 pytest
    return _command_allowed(words, extra)


def _command_allowed(words: list[str], extra: list[str]) -> bool:
    if not words:
        return False
    joined = " ".join(words)
    if joined in extra or any(joined.startswith(item + " ") for item in extra):
        return True  # a listed line, with arguments of its own (already checked to stay inside)
    program = PurePosixPath(words[0]).name  # venv/bin/python counts as python
    if program in WRAPPERS:  # it runs what follows: check that instead
        rest = words[1:]
        while rest and (rest[0].startswith("-") or ASSIGNMENT.match(rest[0])):
            rest = rest[1:]
        return _command_allowed(rest[WRAPPERS[program]:], extra)
    if program == "uv":
        return words[1:2] == ["run"] and _command_allowed(words[2:], extra)
    if program == "git":
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]  # -C <inside path>, -c name=value
        if not rest or rest[0] not in SUBCOMMANDS["git"]:
            return False
        if rest[0] == "branch":  # listing only: never delete, move or create a branch the checkout shares
            return all(word in GIT_LISTING for word in rest[1:])
        return True
    if program in SUBCOMMANDS:
        rest = [word for word in words[1:] if not word.startswith("-")]
        return bool(rest) and rest[0] in SUBCOMMANDS[program]
    if program == "find":
        return not any(word in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls")
                       for word in words[1:])
    if program == "sed":
        script = next((word for word in words[1:] if not word.startswith("-")), "")
        writes = re.search(r"(?:^|[;{}\n])\s*[0-9,$/^]*\s*[wWe](?:\s|$)|/[gpI0-9]*[we](?:\s|$)", script)
        return not any(word.startswith("-i") or word == "--in-place" for word in words[1:]) and not writes
    if program == "awk":
        script = next((word for word in words[1:] if not word.startswith("-")), "")
        return not re.search(r"system\s*\(|\||>|getline", script)
    # Tests run code the model wrote anyway; the list states intent (build, test, look), it is not a sandbox.
    return program in PROGRAMS or bool(re.fullmatch(r"python3(\.\d+)?", program))


def _outside(word: str) -> bool:
    """A word that points outside the folder the line runs in (or at a secrets file), in any of its forms: plain,
    `--opt=value`, `-oVALUE`, a variable whose value nobody can read off the line, a Windows path."""
    candidates = [word]
    if word.startswith("--") and "=" in word:
        candidates.append(word.split("=", 1)[1])
    elif re.match(r"^-[A-Za-z]", word) and len(word) > 2:
        candidates.append(word[2:])  # -o../out, -C..
    for text in candidates:
        if VARIABLE.search(text):
            return True
        if text.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
            return True
        if re.search(r"(^|[\\/])\.\.($|[\\/])", text):
            return True
        if re.search(r"(^|[\\/])\.env(\.|$)|id_rsa|id_ed25519|\.ssh([\\/]|$)|credentials", text):
            return True
    return False
