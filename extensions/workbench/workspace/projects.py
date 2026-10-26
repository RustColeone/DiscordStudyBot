"""Projects the club may work in: a git checkout, how to test it, and where its changes land."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
SAMPLE = Path(__file__).resolve().parents[1] / "sample"


@dataclass(frozen=True)
class Project:
    """`test` is a shell line run in a task's worktree; `{checkout}` stands for this checkout's path and `{python}`
    for its virtualenv's Python (or python3). `link` names git-ignored folders (a venv, node_modules) that a
    worktree borrows from the checkout as symlinks, so tests run without reinstalling. `land` is `commit` (the
    change becomes one commit on the checkout's current branch) or `branch` (it stays on its own branch)."""

    name: str
    path: Path
    test: str | None = None
    link: tuple[str, ...] = ()
    land: str = "commit"
    description: str = ""
    source: str = "config"  # config, added (in the web page), sample
    detected: bool = False  # the test line was guessed, not configured

    def python(self) -> str:
        """The checkout's own Python (its venv, on POSIX or Windows), else the system's. A path bash reads alike on
        every system (forward slashes)."""
        for folder in ("venv", ".venv"):
            for candidate in ("bin/python", "Scripts/python.exe"):
                if (self.path / folder / candidate).exists():
                    return (self.path / folder / candidate).as_posix()
        return "python" if os.name == "nt" else "python3"

    def test_line(self) -> str | None:
        if not self.test:
            return None
        return self.test.replace("{checkout}", str(self.path)).replace("{python}", self.python())

    def to_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "test": self.test, "test_line": self.test_line(),
                "link": list(self.link),
                "land": self.land, "description": self.description, "source": self.source,
                "detected": self.detected}


def detect_test(path: Path) -> str | None:
    """A best guess at the project's test line, from the files it has. None when nothing looks like tests."""
    if (path / "package.json").is_file():
        try:
            scripts = json.loads((path / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        if "test" in scripts and "no test specified" not in str(scripts["test"]):
            return "npm test --silent"
    if (path / "Cargo.toml").is_file():
        return "cargo test -q"
    if (path / "go.mod").is_file():
        return "go test ./..."
    pyproject = path / "pyproject.toml"
    pytest_config = (path / "pytest.ini").is_file() or (path / "conftest.py").is_file() or (
        pyproject.is_file() and "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="replace"))
    tests = path / "tests"
    if pytest_config:
        return "{python} -m pytest -q"
    if tests.is_dir() and any(tests.rglob("test*.py")):
        if (tests / "__init__.py").is_file():
            return "{python} -m unittest discover -s tests -t ."
        return "{python} -m unittest discover -s tests"
    if any(path.glob("test_*.py")):
        return "{python} -m unittest discover"
    makefile = path / "Makefile"
    if makefile.is_file() and re.search(r"^test:", makefile.read_text(encoding="utf-8", errors="replace"), re.M):
        return "make test"
    return None


def is_repository(path: Path) -> bool:
    return (path / ".git").exists()


def project_from(name: str, settings: Mapping[str, Any], source: str = "config") -> Project:
    if not NAME.match(name):
        raise ValueError(f"Project names are lowercase letters, digits, '.', '_' or '-': {name!r}")
    path = Path(str(settings.get("path") or "")).expanduser()
    if not str(settings.get("path") or "").strip():
        raise ValueError(f"Project {name!r} needs a path")
    test = settings.get("test")
    detected = False
    if test is None and path.is_dir():
        test, detected = detect_test(path), True
    land = str(settings.get("land") or "commit")
    if land not in ("commit", "branch"):
        raise ValueError(f"Project {name!r}: land is commit or branch")
    link = tuple(str(item).strip("/") for item in settings.get("link") or () if str(item).strip("/"))
    if any(".." in Path(item).parts or Path(item).is_absolute() for item in link):
        raise ValueError(f"Project {name!r}: link names folders inside the project")
    if not link:  # a Python checkout's venv, so `{python}` works in the worktree too
        link = tuple(name for name in ("venv", ".venv") if (path / name).is_dir())[:1]
    return Project(name, path.resolve() if path.exists() else path, str(test) if test else None, link, land,
                   str(settings.get("description") or ""), source, detected)


def make_sample(target: Path) -> Path:
    """A small practice project (a club's meeting schedule, with tests) copied from `sample/` into its own git
    repository, so the club can be tried without touching real code. Kept if it already exists."""
    if is_repository(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    initial_commit(target, "Start the club's schedule tool")
    return target


def initial_commit(path: Path, message: str) -> None:
    """`git init` and one commit of what is there (an empty one for an empty folder)."""
    identity = ["-c", "user.name=Workbench", "-c", "user.email=workbench@localhost"]
    run = lambda *args: subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    if not is_repository(path):
        run("init", "-q", "-b", "main")
    run("add", "-A")
    run(*identity, "commit", "-q", "--allow-empty", "-m", message)


def create_project(path: Path, name: str) -> Path:
    """A new, empty project: a folder with a README, in its own repository."""
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} already exists and is not empty")
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\nvenv/\n.venv/\nnode_modules/\n", encoding="utf-8")
    initial_commit(path, f"Start {name}")
    return path


def with_test(project: Project, test: str | None) -> Project:
    return replace(project, test=test, detected=False)



class Registry:
    """Every project the club may work in: `projects:` in the config, those added in the web page, and the
    practice project (`sample`, made on first use in the module's data folder unless `sample: false`)."""

    def __init__(self, config: Mapping[str, Any], board, data_dir: Path):
        self.config, self.board, self.data_dir = config, board, Path(data_dir)
        self.folder = Path(str(config.get("projects_dir") or "")).expanduser() if config.get("projects_dir") else None

    def all(self) -> dict[str, Project]:
        projects: dict[str, Project] = {}
        if self.config.get("sample", True) is not False:
            path = make_sample(self.data_dir / "sample")
            projects["sample"] = project_from("sample", {"path": str(path), "description": "The practice project"},
                                              "sample")
        for name, settings in self.board.added_projects().items():
            try:
                projects[name] = project_from(name, settings, "added")
            except ValueError:
                continue
        for name, settings in dict(self.config.get("projects") or {}).items():
            projects[str(name)] = project_from(str(name), dict(settings or {}))
        return projects

    def get(self, name: str) -> Project | None:
        return self.all().get(name)

    def add(self, name: str, path: str, test: str | None = None) -> Project:
        name = name.strip().lower()
        folder = Path(path).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a folder")
        if not is_repository(folder):
            raise ValueError(f"{folder} is not a git repository (run git init there, or start a new project)")
        if name in self.all() and self.all()[name].source != "added":
            raise ValueError(f"A project named {name!r} already exists")
        settings = {"path": str(folder)}
        if test:
            settings["test"] = test
        project = project_from(name, settings, "added")
        self.board.add_project(name, settings)
        return project

    def create(self, name: str, path: str | None = None) -> Project:
        name = name.strip().lower()
        if not NAME.match(name):
            raise ValueError("Use lowercase letters, digits, '.', '_' or '-' for the name")
        if name in self.all():
            raise ValueError(f"A project named {name!r} already exists")
        folder = Path(path).expanduser() if path else self.default_folder(name)
        create_project(folder.resolve(), name)
        return self.add(name, str(folder))

    def default_folder(self, name: str) -> Path:
        return (self.folder or self.data_dir / "projects") / name
