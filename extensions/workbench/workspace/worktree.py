"""Each task works in its own git worktree on its own branch; nothing reaches the checkout until it lands.

    checkout (yours, on main)            worktree of task t-1 (branch workbench/t-1, from main's HEAD)
    ──────────────────────────           ───────────────────────────────────────────────────────────
    untouched while the task runs   ←──  edits, tests, the reviewer's diff
            ▲
            └── land: the change set becomes one commit on your branch (fast-forward, or a cherry-pick when
                your branch moved; a conflict or a dirty file it touches stops it with nothing applied)

The change set is everything in the worktree that differs from the task's base commit, new files included and
the borrowed `link` folders and the tester's `.probe/` left out. It is computed on a throwaway index built from
the base commit, so what is staged in the worktree (or not) never changes it, and looking at a task's changes never
disturbs a model working in it at the same time. Landing makes one commit of exactly that tree, with the base commit
as its parent (`commit-tree`), and never touches the worktree: a refused or failed land leaves the task as it was.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import system
from .git import GitError, git, out
from .projects import Project, is_repository

DEFAULT_IDENTITY = ("-c", "user.name=Workbench", "-c", "user.email=workbench@localhost")
BASE_FILE = "workbench-base"  # the task's base commit, kept in the worktree's own git directory
PROBE = ".probe"  # the tester's scratch folder in a worktree: never part of a change set


class LandError(RuntimeError):
    """Landing stopped before anything changed in the checkout; the message says why."""


@dataclass(frozen=True)
class ChangeSet:
    files: list[dict] = field(default_factory=list)  # {path, old_path, status, added, removed, binary}
    patch: str = ""
    fingerprint: str = ""
    added: int = 0
    removed: int = 0
    tree: str = ""  # the git tree of exactly these files (`restore` puts a worktree back to it)

    @property
    def empty(self) -> bool:
        return not self.files

    def stat(self) -> str:
        if self.empty:
            return "no changes"
        return f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} (+{self.added} −{self.removed})"

    def to_dict(self, patch: bool = True, limit: int = 400_000) -> dict:
        data = {"files": self.files, "fingerprint": self.fingerprint, "added": self.added, "removed": self.removed,
                "stat": self.stat(), "tree": self.tree}
        if patch:
            data["patch"] = self.patch[:limit]
            data["truncated"] = len(self.patch) > limit
        return data


def excludes(links) -> list[str]:
    return [f":(exclude){name}" for name in (*links, PROBE)]


async def identity(path: Path) -> tuple[str, ...]:
    """Commit as the checkout's own git identity, or as `Workbench` where none is configured."""
    email = (await git(path, "config", "user.email", check=False)).stdout.strip()
    return () if email else DEFAULT_IDENTITY


class Worktrees:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def path_for(self, task: str) -> Path:
        return self.root / task

    async def open(self, project: Project, task: str) -> dict:
        """The task's worktree on branch `workbench/<task>` from the checkout's current commit. Opening it again
        returns the existing one."""
        if not is_repository(project.path):
            raise GitError(("worktree",), 1, f"{project.path} is not a git repository")
        path, branch = self.path_for(task), f"workbench/{task}"
        if (path / ".git").exists():
            return {"path": str(path), "branch": branch, "base": await self._base(path, project), "reused": True}
        head = await git(project.path, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if head.code != 0:
            raise GitError(("rev-parse",), 1, f"{project.name} has no commits yet; commit something first")
        base = head.stdout.strip()
        self.root.mkdir(parents=True, exist_ok=True)
        await git(project.path, "worktree", "prune", check=False)
        await git(project.path, "worktree", "add", "-q", "-B", branch, str(path), base)
        (await self._gitdir(path) / BASE_FILE).write_text(base + "\n", encoding="utf-8")
        self._borrow(project, path)
        return {"path": str(path), "branch": branch, "base": base, "reused": False}

    @staticmethod
    def _borrow(project: Project, path: Path) -> None:
        """The checkout's git-ignored `link` folders (a venv, node_modules), borrowed into a worktree."""
        for name in project.link:
            source, target = project.path / name, path / name
            if source.exists() and not target.exists() and not target.is_symlink():
                system.borrow(source, target)

    async def base_copy(self, project: Project, task: str, base: str) -> Path:
        """A pristine checkout of the task's base commit next to its worktree (`<task>.base`, detached), so the
        tester can run the same probe on the old code and the new; removed with the task."""
        path = self.root / f"{task}.base"
        if (path / ".git").exists():
            return path
        self.root.mkdir(parents=True, exist_ok=True)
        await git(project.path, "worktree", "prune", check=False)
        await git(project.path, "worktree", "add", "-q", "--detach", str(path), base)
        self._borrow(project, path)
        return path

    @staticmethod
    async def _gitdir(path: Path) -> Path:
        """The worktree's own git directory (`.git/worktrees/<name>` of the checkout): removed with it."""
        return Path(await out(path, "rev-parse", "--path-format=absolute", "--git-dir"))

    async def _base(self, path: Path, project: Project) -> str:
        recorded = await self._gitdir(path) / BASE_FILE
        if recorded.is_file():
            return recorded.read_text(encoding="utf-8").strip()
        return await out(path, "merge-base", "HEAD", await out(project.path, "rev-parse", "HEAD"))

    async def change(self, path: Path, base: str, links=(), context: int = 3) -> ChangeSet:
        """Everything in the worktree that differs from `base`, new files included and `links` left out."""
        return (await self._staged(path, base, links, context))[0]

    async def _staged(self, path: Path, base: str, links=(), context: int = 3):
        """(the change set, the tree it makes): the worktree's files on a fresh index of `base`."""
        path = Path(path)
        with tempfile.TemporaryDirectory(prefix="wb-index-") as scratch:
            env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            await git(path, "read-tree", base, env=env)  # what is staged in the worktree never matters
            await git(path, "add", "-A", "--", ".", *excludes(links), env=env)
            plain = ("--no-ext-diff", "--no-textconv", "--no-color")  # whatever the user's git config says
            numstat = await out(path, "diff", "--cached", *plain, "--numstat", "-M", "-z", base, env=env)
            patch = (await git(path, "diff", "--cached", *plain, "-M", f"-U{context}", base, env=env)).stdout
            binary = (await git(path, "diff", "--cached", *plain, "-M", "--binary", "--full-index", base,
                                env=env)).stdout
            names = await out(path, "diff", "--cached", *plain, "--name-status", "-M", "-z", base, env=env)
            tree = await out(path, "write-tree", env=env)
        files = _files(numstat, names)
        change = ChangeSet(files, patch, hashlib.sha256(binary.encode()).hexdigest()[:16] if files else "",
                           sum(item["added"] for item in files), sum(item["removed"] for item in files), tree)
        return change, tree

    async def land(self, project: Project, path: Path, branch: str, base: str, message: str,
                   fingerprint: str) -> dict:
        """Make the reviewed change set one commit on the checkout's branch (or leave it on its own branch, for a
        project with `land: branch`). Every check happens before anything in the checkout changes, and the worktree
        itself is never changed: whatever fails, the task can land later as it is."""
        path = Path(path)
        change, tree = await self._staged(path, base, project.link)
        if change.empty:
            raise LandError("There is nothing to land: the task changed no files.")
        if change.fingerprint != fingerprint:
            raise LandError("The change set is different from the one that was reviewed; review it again.")
        checkout = project.path
        target = ""
        if project.land == "commit":
            target = (await git(checkout, "symbolic-ref", "--short", "-q", "HEAD", check=False)).stdout.strip()
            if not target:
                raise LandError(f"{project.name} is not on a branch (detached HEAD); check out a branch first.")
            touched = sorted({name for item in change.files for name in (item["path"], item.get("old_path")) if name})
            dirty = await out(checkout, "status", "--porcelain", "--untracked-files=all", "--", *touched,
                              env={"GIT_LITERAL_PATHSPECS": "1"})  # file names, never patterns
            if dirty:
                raise LandError("Your checkout has uncommitted changes in files this task changes:\n" + dirty)
            if (await git(checkout, "diff", "--cached", "--quiet", check=False)).code != 0:
                raise LandError("Your checkout has staged changes; commit or unstage them first.")
        names = await identity(checkout)
        # One commit of exactly the reviewed tree, its parent the base commit: whatever was committed in the worktree
        # meanwhile (an approved `git commit`, a script) does not change what lands.
        commit = await out(path, *names, "commit-tree", tree, "-p", base, input_text=message + "\n")
        if project.land == "branch":
            await git(checkout, "update-ref", f"refs/heads/{branch}", commit)
            await self.remove(project, path, branch, delete_branch=False)
            return {"mode": "branch", "branch": branch, "commit": commit, "stat": change.stat()}
        head = await out(checkout, "rev-parse", "HEAD")
        try:
            if head == base:
                await git(checkout, "merge", "--ff-only", "-q", commit)
            else:
                if await merge_tree_dry_run():
                    trial = await git(checkout, "merge-tree", "--write-tree", "--name-only", "--merge-base", base,
                                      head, commit, check=False)
                    if trial.code == 1:
                        raise LandError(f"{target} changed since the task started and now conflicts with it:\n"
                                        + "\n".join(trial.stdout.strip().splitlines()[1:])[:1500])
                picked = await git(checkout, *names, "cherry-pick", commit, check=False)
                if picked.code != 0:
                    await git(checkout, "cherry-pick", "--abort", check=False)
                    raise LandError("Applying the commit failed, and nothing was changed:\n" + picked.output[-1500:])
        except GitError as error:  # (an untracked file in the way, a branch that moved meanwhile)
            raise LandError(f"git refused to land it, and nothing was changed: {error.output.strip()[-1200:]}") from None
        landed = await out(checkout, "rev-parse", "HEAD")
        await self.remove(project, path, branch)
        return {"mode": "commit", "branch": target, "commit": landed, "stat": change.stat(),
                "fast_forward": head == base}

    async def restore(self, path: Path, base: str, tree: str, links=()) -> ChangeSet:
        """Put the worktree's files back to `tree` (a change set's own, from `change`): the files that differ are
        rewritten from it or removed, nothing else is touched (borrowed `links`, `.probe/`, ignored files)."""
        path = Path(path)
        now = (await self._staged(path, base, links))[1]
        if now != tree:
            listed = await out(path, "diff-tree", "-r", "-z", "--no-renames", "--name-only", tree, now)
            names = [name for name in listed.split("\0") if name]
            kept = set((await out(path, "ls-tree", "-r", "-z", "--name-only", tree, "--", *names,
                                  env={"GIT_LITERAL_PATHSPECS": "1"})).split("\0")) - {""} if names else set()
            for name in names:
                target = path / name
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            if kept:
                with tempfile.TemporaryDirectory(prefix="wb-index-") as scratch:
                    env = {"GIT_INDEX_FILE": str(Path(scratch) / "index"), "GIT_LITERAL_PATHSPECS": "1"}
                    await git(path, "read-tree", tree, env=env)
                    await git(path, "checkout-index", "-f", "--", *sorted(kept), env=env)
        return (await self._staged(path, base, links))[0]

    async def remove(self, project: Project, path: Path, branch: str, delete_branch: bool = True) -> None:
        path = Path(path)
        for each in (path, path.with_name(path.name + ".base")):  # the task's worktree and its base copy
            if is_repository(project.path) and each.exists():
                await git(project.path, "worktree", "remove", "--force", str(each), check=False)
            if each.exists() or each.is_symlink():
                shutil.rmtree(each, ignore_errors=True)
        if is_repository(project.path):
            await git(project.path, "worktree", "prune", check=False)
            if delete_branch:
                await git(project.path, "branch", "-D", branch, check=False)


_MERGE_TREE: list[bool] = []


async def merge_tree_dry_run() -> bool:
    """Whether this git can try a merge without touching anything (`merge-tree --write-tree --merge-base`, git 2.40);
    an older one goes straight to the cherry-pick, which stops cleanly on a conflict too."""
    if not _MERGE_TREE:
        version = (await git(Path.cwd(), "version", check=False)).stdout
        numbers = [int(part) for part in re.findall(r"\d+", version)[:2]] or [0, 0]
        _MERGE_TREE.append(tuple(numbers) >= (2, 40))
    return _MERGE_TREE[0]


def _files(numstat: str, names: str) -> list[dict]:
    """Join `--numstat -z` and `--name-status -z` (renames carry two paths in both)."""
    statuses: dict[str, tuple[str, str | None]] = {}
    parts = names.split("\0")
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index]
        if status[:1] in ("R", "C"):
            old, new = parts[index + 1], parts[index + 2]
            statuses[new] = (status[:1], old)
            index += 3
        else:
            statuses[parts[index + 1]] = (status[:1], None)
            index += 2
    files = []
    parts = numstat.split("\0")
    index = 0
    while index < len(parts) and parts[index]:
        added, removed, rest = parts[index].split("\t", 2)
        if rest == "":  # a rename: the two paths follow as their own fields
            old, new = parts[index + 1], parts[index + 2]
            index += 3
        else:
            old, new = None, rest
            index += 1
        status, renamed_from = statuses.get(new, ("M", old))
        binary = added == "-"
        files.append({"path": new, "old_path": renamed_from or old, "status": status,
                      "added": 0 if binary else int(added), "removed": 0 if binary else int(removed),
                      "binary": binary})
    return files
