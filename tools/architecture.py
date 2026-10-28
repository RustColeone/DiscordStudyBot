"""Build docs/architecture/data.js: the code's architecture as data for the browsable page in docs/architecture/.

    python tools/architecture.py            # write docs/architecture/data.js
    python tools/architecture.py --check    # exit 1 when it is out of date (tests/test_architecture.py runs this)

Everything comes from the code itself: git's list of Python files, and Python's `ast` for each file's docstring,
imports, classes, functions, which of them call which, and the source. A module's data files (its `.yml` and `.md`
files other than READMEs: settings, a student's prompt) are listed too, with their text and the files they name.
Which modules fill which slot (interfaces, engines, capabilities, personas, watchers) is read from their `extension.py`. Only the top map's boxes are named here (CORE_PARTS), after
docs/core/ARCHITECTURE.md; change them when the architecture changes. The output has no dates or commit IDs, so it
changes only when the code does.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "architecture" / "data.js"
REPO = "https://github.com/RustColeone/DiscordStudyBot"
BRANCH = "master"
ROOTS = ("contracts", "core", "extensions", "plugins", "tools", "tests")  # the folders the page shows, in order
SLOTS = ("interfaces", "engines", "capabilities", "personas", "watchers")
FIELDS = {"interfaces", "engines", "capabilities", "personas", "profiles", "assignments", "admission", "permissions",
          "providers", "adapters", "prompt_parts", "watchers"}  # contracts.Contributions
DATA = ("yml", "yaml", "md")  # a module's data files: settings, prompts (READMEs are the folders' own text)
# The core's blocks on the top map (docs/core/ARCHITECTURE.md): id, label, what it owns or does, where it lives.
CORE_PARTS = [
    ("GW", "Gateway", "admission · order · dedupe · routes · routines", "core/gateway"),
    ("MG", "Manager", "registries · permissions · durable runs · approvals · stamina", "core/manager"),
    ("PR", "Providers", "model profiles · fallback · adapters · prices", "core/providers"),
    ("BOT", "Bottle", "gateway.db", "core/storage/bottle.py"),
    ("TANK", "Tank", "tank.db", "core/storage/tank.py"),
    ("START", "Startup", "config · loader · app · setup", "core"),
]
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # a Markdown link, [text](target): the pane shows its text
SECTION = re.compile(r"^\s*# -{2,}\s*(.+?)\s*-{2,}\s*$")  # "# ---- pages and files ------" groups methods


def tracked_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "--", *(f"{root}/*.py" for root in ROOTS),
                             *(f"{root}/*.{suffix}" for root in ("extensions", "plugins") for suffix in DATA)],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return sorted(path for path in listed if (ROOT / path).is_file() and not path.endswith("/README.md"))


def module_path(module: str, known: set[str]) -> str | None:
    """`core.manager.policy` → core/manager/policy.py (or the package's __init__.py), when it is in the repository."""
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in known:
            return candidate
    return None


def resolve_from(path: str, node: ast.ImportFrom) -> str:
    """The absolute module an import names, relative ones (`from .engine import X`) included."""
    if not node.level:
        return node.module or ""
    package = path.split("/")[:-1]
    package = package[:len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*package, *([node.module] if node.module else [])])


def signature(node) -> str:
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return f"class {node.name}" + (f"({bases})" if bases else "")
    arguments = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({arguments}){returns}"


def sections(lines: list[str], body: list[ast.stmt]) -> dict[int, str]:
    """For each method's first line, the `# ---- name ----` comment above it in its class (or "")."""
    found, current, last = {}, "", 0
    for statement in body:
        start = statement.lineno - len(getattr(statement, "decorator_list", []) or [])
        for text in lines[last:start - 1]:
            match = SECTION.match(text)
            if match:
                current = match.group(1).strip(" .:")
        found[statement.lineno] = current
        last = getattr(statement, "end_lineno", statement.lineno)
    return found


class FileReader:
    """One file's docstring, imports, symbols and calls."""

    def __init__(self, path: str, known: set[str]):
        self.path, self.known = path, known
        self.text = (ROOT / path).read_text(encoding="utf-8")
        self.tree = ast.parse(self.text, filename=path)
        self.lines = self.text.splitlines()
        self.imported: dict[str, tuple[str, str]] = {}  # local name → (path, name there; "" for a module)
        self.imports: list[dict] = []

    def read(self) -> dict:
        self._imports()
        top = {node.name: node for node in self.tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        symbols = []
        for node in top.values():
            symbols.append(self._symbol(node, "", top))
            if isinstance(node, ast.ClassDef):
                grouped = sections(self.lines, node.body)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(self._symbol(child, node.name, top, grouped.get(child.lineno, ""), node))
        return {"doc": ast.get_docstring(self.tree) or "", "lines": len(self.lines), "imports": self.imports,
                "symbols": symbols, "text": self.text}

    def _imports(self) -> None:
        seen = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = module_path(alias.name, self.known)
                    self.imported[alias.asname or alias.name.split(".")[0]] = (target or "", "")
                    self._record(alias.name, target, seen)
            elif isinstance(node, ast.ImportFrom):
                module = resolve_from(self.path, node)
                base = module_path(module, self.known)
                for alias in node.names:
                    submodule = module_path(f"{module}.{alias.name}", self.known)  # `from core import prompts`
                    if submodule:
                        self.imported[alias.asname or alias.name] = (submodule, "")
                    elif base:
                        self.imported[alias.asname or alias.name] = (base, alias.name)
                    self._record(module if not submodule else f"{module}.{alias.name}", submodule or base, seen)

    def _record(self, module: str, target: str | None, seen: set) -> None:
        key = target or module
        if key and key not in seen and target != self.path:
            seen.add(key)
            self.imports.append({"module": module, "path": target or ""})

    def _symbol(self, node, parent: str, top: dict, section: str = "", owner: ast.ClassDef | None = None) -> dict:
        start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        name = f"{parent}.{node.name}" if parent else node.name
        symbol = {"id": name, "name": node.name, "kind": "class" if isinstance(node, ast.ClassDef)
                  else "method" if parent else "function", "parent": parent, "line": start, "end": node.end_lineno,
                  "signature": signature(node), "doc": ast.get_docstring(node) or "",
                  "decorators": [ast.unparse(item) for item in node.decorator_list]}
        if parent:
            symbol["section"] = section
        if isinstance(node, ast.ClassDef):
            symbol["bases"] = [ast.unparse(base) for base in node.bases]
            return symbol
        methods = {item.name for item in owner.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))} \
            if owner is not None else set()
        calls, uses = [], []
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            function = call.func
            target = None
            if isinstance(function, ast.Name):
                if function.id in top and (function.id != node.name or parent):  # (not itself, recursively)
                    target = function.id
                elif function.id in self.imported and self.imported[function.id][0]:
                    path, there = self.imported[function.id]
                    uses.append({"path": path, "name": there or function.id})
            elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                holder, attribute = function.value.id, function.attr
                if holder in ("self", "cls") and attribute in methods:
                    target = f"{parent}.{attribute}"
                elif holder in top and isinstance(top[holder], ast.ClassDef):
                    target = f"{holder}.{attribute}" if any(
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == attribute
                        for item in top[holder].body) else holder
                elif holder in self.imported and self.imported[holder][0]:
                    path, there = self.imported[holder]
                    uses.append({"path": path, "name": f"{there}.{attribute}" if there else attribute})
            if target and target != name and target not in calls:
                calls.append(target)
        symbol["calls"] = calls
        symbol["uses"] = [dict(item) for item in {(use["path"], use["name"]): use for use in uses}.values()]
        return symbol


class DataReader:
    """One data file: its text, its first comment (or line) as its doc, and the files it names by a relative path
    (a student's `prompt:` files, say)."""

    NAME = re.compile(r"[\w./-]+\.(?:md|ya?ml|py)\b")

    def __init__(self, path: str, known: set[str]):
        self.path, self.known = path, known
        self.text = (ROOT / path).read_text(encoding="utf-8")

    def read(self) -> dict:
        lines = self.text.splitlines()
        if self.path.endswith(".md"):
            doc = next((line.lstrip("# ").strip() for line in lines if line.strip()), "")
        else:
            doc = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), "")
        imports, folder = [], Path(self.path).parent
        if not self.path.endswith(".md"):
            for name in dict.fromkeys(self.NAME.findall(self.text)):
                target = posixpath.normpath(f"{folder.as_posix()}/{name}")
                if target in self.known and target != self.path:
                    imports.append({"module": name, "path": target})
        return {"doc": doc, "lines": len(lines), "imports": imports, "symbols": [], "text": self.text, "data": True}


def handed_over(files: dict[str, dict], folder: str) -> set[str]:
    """The slots a module's other files fill: a `contributions(...)` function returning them as a dict, which its
    setup passes on (the students' memory.py)."""
    filled = set()
    for path in files:
        if not path.startswith(f"{folder}/") or not path.endswith(".py") or path.endswith("/extension.py"):
            continue
        for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name == "contributions":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                        keys = {key.value for key in inner.value.keys if isinstance(key, ast.Constant)}
                        if keys and keys <= FIELDS:
                            filled |= keys & set(SLOTS)
    return filled


def slots(files: dict[str, dict]) -> dict[str, dict]:
    """Each module (a folder with extension.py): its description, the slots it fills (with what its other files hand
    over), the modules it requires and uses (contracts 1.10) and the names it had (`formerly`, 1.11), read from that
    file."""
    modules = {}
    for path in files:
        if not path.endswith("/extension.py") or path.count("/") != 2:
            continue
        folder = path.rsplit("/", 1)[0]
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        filled, description, declared = handed_over(files, folder), "", {"requires": [], "uses": [], "formerly": []}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Contributions":
                filled |= {keyword.arg for keyword in node.keywords if keyword.arg in SLOTS}
            keys = {key.value for key in getattr(node, "keys", None) or () if isinstance(key, ast.Constant)}
            if isinstance(node, ast.Dict) and keys and keys <= FIELDS:  # contributions built as a dict first (whale_chan)
                filled |= keys & set(SLOTS)  # (an assignment's {"engine", "personas", ...} is not one)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "EngineSpec":  # an engine's former names
                for keyword in node.keywords:
                    if keyword.arg == "formerly" and isinstance(keyword.value, (ast.Tuple, ast.List)):
                        declared["formerly"] += [item.value for item in keyword.value.elts if isinstance(item, ast.Constant)]
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Extension":
                for keyword in node.keywords:
                    if keyword.arg == "description" and isinstance(keyword.value, ast.Constant):
                        description = str(keyword.value.value)
                    if keyword.arg in declared and isinstance(keyword.value, (ast.Tuple, ast.List)):
                        declared[keyword.arg] += [item.value for item in keyword.value.elts if isinstance(item, ast.Constant)]
        declared["formerly"] = list(dict.fromkeys(declared["formerly"]))
        modules[folder] = {"description": description, "slots": [slot for slot in SLOTS if slot in filled], **declared}
    return modules


def folders(files: dict[str, dict]) -> dict[str, dict]:
    tree: dict[str, dict] = {}
    for path in files:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            entry = tree.setdefault(folder, {"children": [], "readme": ""})
            child = "/".join(parts[:depth + 1])
            if child not in entry["children"]:
                entry["children"].append(child)
    for folder, entry in tree.items():
        readme = ROOT / folder / "README.md"
        if readme.is_file():
            entry["readme"] = first_paragraph(readme.read_text(encoding="utf-8"))
        entry["children"].sort(key=lambda child: (child in files, child))  # folders first
    return tree


def first_paragraph(text: str) -> str:
    """A README's first paragraph of prose (after its title), for the side pane."""
    paragraph: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or line.startswith("<!--"):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return LINK.sub(r"\1", " ".join(paragraph))[:1200]


def build() -> str:
    paths = tracked_files()
    known = set(paths)
    files = {path: (FileReader if path.endswith(".py") else DataReader)(path, known).read() for path in paths}
    data = {"repo": REPO, "branch": BRANCH, "roots": [root for root in ROOTS if any(p.startswith(root + "/") for p in paths)],
            "core": [{"id": part, "label": label, "note": note, "target": target} for part, label, note, target in CORE_PARTS],
            "modules": slots(files), "folders": folders(files), "files": files}
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ("// Generated by tools/architecture.py from the code; do not edit (run it again instead).\n"
            f"window.ARCH = {body};\n")


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run `python tools/architecture.py`", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
