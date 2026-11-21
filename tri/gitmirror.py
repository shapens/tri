"""Git plumbing for the two-way mirror between a Tri target and a Git branch.

The mirror keeps a private bare repository per mirror under the Tri home. It
fetches the remote branch into `refs/tri/remote`, reads commits with
`ls-tree`/`diff-tree`/`cat-file`, writes outbound commits in one
`git fast-import` stream, and pushes without force so a concurrent push from a
Git user is detected instead of overwritten.

This module knows nothing about Tri's store; `tri.cli` owns policy, snapshots,
and the sync algorithm.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REMOTE_REF = "refs/tri/remote"
OUTBOUND_REF = "refs/tri/outbound"
SUBMODULE_MODE = "160000"


class GitMirrorError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, **fields: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fields = fields


def git(git_dir: Path, args: list[str], *, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    result = subprocess.run(["git", f"--git-dir={git_dir}", *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if check and result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise GitMirrorError("git_mirror_failed", stderr or f"git {args[0]} failed.", command=args[0])
    return result


def git_text(git_dir: Path, args: list[str]) -> str:
    return git(git_dir, args).stdout.decode().strip()


def ensure_bare(git_dir: Path) -> None:
    if (git_dir / "HEAD").exists():
        return
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "init", "--bare", "--quiet", str(git_dir)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise GitMirrorError("git_mirror_failed", result.stderr.decode(errors="replace").strip() or "git init failed.")


def remote_branch_tip(git_dir: Path, remote: str, branch: str) -> str | None:
    result = git(git_dir, ["ls-remote", "--heads", remote, f"refs/heads/{branch}"], check=False)
    if result.returncode != 0:
        raise GitMirrorError("git_mirror_unreachable", result.stderr.decode(errors="replace").strip() or "Cannot reach the Git remote.", retryable=True, remote=remote)
    for line in result.stdout.decode().splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def fetch(git_dir: Path, remote: str, branch: str) -> str | None:
    """Fetch the remote branch; returns its tip, or None when the branch does not exist yet."""
    if remote_branch_tip(git_dir, remote, branch) is None:
        return None
    result = git(git_dir, ["fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{branch}:{REMOTE_REF}"], check=False)
    if result.returncode != 0:
        raise GitMirrorError("git_mirror_unreachable", result.stderr.decode(errors="replace").strip() or "git fetch failed.", retryable=True, remote=remote)
    return git_text(git_dir, ["rev-parse", REMOTE_REF])


def is_ancestor(git_dir: Path, ancestor: str, descendant: str) -> bool:
    return git(git_dir, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def has_commit(git_dir: Path, sha: str) -> bool:
    return git(git_dir, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0


def first_parent_commits(git_dir: Path, tip: str, since: str) -> list[str]:
    """Commits on tip's first-parent chain after `since`, oldest first."""
    out = git_text(git_dir, ["rev-list", "--reverse", "--first-parent", tip, f"^{since}"])
    return [line for line in out.splitlines() if line]


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    oid: str


def ls_tree(git_dir: Path, commit: str) -> dict[str, TreeEntry]:
    raw = git(git_dir, ["ls-tree", "-r", "-z", "--full-tree", commit]).stdout
    entries: dict[str, TreeEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, kind, oid = header.decode().split(" ")
        if kind != "blob":
            continue
        entries[path.decode()] = TreeEntry(mode, oid)
    return entries


@dataclass(frozen=True)
class TreeChange:
    status: str  # "A", "M", "D", or "T"
    path: str
    mode: str | None
    oid: str | None


def diff_tree(git_dir: Path, old: str, new: str) -> list[TreeChange]:
    raw = git(git_dir, ["diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", old, new]).stdout
    parts = raw.split(b"\0")
    changes: list[TreeChange] = []
    index = 0
    while index < len(parts) - 1:
        meta = parts[index].decode()
        if not meta.startswith(":"):
            index += 1
            continue
        _old_mode, new_mode, _old_oid, new_oid, status = meta[1:].split(" ")
        path = parts[index + 1].decode()
        index += 2
        if status.startswith("D"):
            changes.append(TreeChange("D", path, None, None))
        else:
            changes.append(TreeChange(status[0], path, new_mode, new_oid))
    return changes


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    tree: str
    author_name: str
    author_email: str
    author_date: str
    message: str


def commit_info(git_dir: Path, sha: str) -> CommitInfo:
    raw = git(git_dir, ["show", "-s", "--format=%T%x00%an%x00%ae%x00%aI%x00%B", sha]).stdout.decode(errors="replace")
    tree, name, email, date, message = (raw.split("\0", 4) + [""] * 5)[:5]
    return CommitInfo(sha, tree, name, email, date, message.strip())


def blob_oid(data: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


_UNSAFE_IDENT = re.compile(r"[<>\n\r]")


def _ident(name: str, email: str, timestamp: int) -> bytes:
    name = _UNSAFE_IDENT.sub("", name).strip() or "tri"
    email = _UNSAFE_IDENT.sub("", email).strip() or "tri@tri.invalid"
    return f"{name} <{email}> {timestamp} +0000".encode()


def _quote_path(path: str) -> bytes:
    if not any(ch in path for ch in ('"', "\n", "\\")) and not path.startswith('"'):
        return path.encode()
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'.encode()


@dataclass
class OutboundCommit:
    """One commit for fast-import. Parents are SHAs or `mark:<n>` for an earlier commit in the batch."""

    parents: list[str]
    message: str
    author: tuple[str, str, int]
    # path -> None (delete), bytes (inline content), or (mode, oid) for an existing blob
    changes: dict[str, object] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)


def fast_import(git_dir: Path, commits: list[OutboundCommit]) -> list[str]:
    """Write commits in one fast-import run; returns their SHAs in order."""
    if not commits:
        return []
    stream = bytearray()

    def ref(parent: str) -> bytes:
        return (":" + parent.split(":", 1)[1]).encode() if parent.startswith("mark:") else parent.encode()

    for number, commit in enumerate(commits, start=1):
        name, email, timestamp = commit.author
        ident = _ident(name, email, timestamp)
        message = commit.message.encode()
        stream += f"reset {OUTBOUND_REF}\n".encode()
        stream += f"commit {OUTBOUND_REF}\nmark :{number}\n".encode()
        stream += b"author " + ident + b"\ncommitter " + ident + b"\n"
        stream += b"data %d\n" % len(message) + message + b"\n"
        if commit.parents:
            stream += b"from " + ref(commit.parents[0]) + b"\n"
            for parent in commit.parents[1:]:
                stream += b"merge " + ref(parent) + b"\n"
        for path in sorted(commit.changes):
            change = commit.changes[path]
            quoted = _quote_path(path)
            if change is None:
                stream += b"D " + quoted + b"\n"
            elif isinstance(change, bytes):
                mode = commit.modes.get(path, "100644")
                stream += f"M {mode} inline ".encode() + quoted + b"\n"
                stream += b"data %d\n" % len(change) + change + b"\n"
            else:
                mode, oid = change  # type: ignore[misc]
                stream += f"M {mode} {oid} ".encode() + quoted + b"\n"
        stream += b"\n"
    stream += b"done\n"
    with tempfile.TemporaryDirectory(prefix="tri-mirror-") as tmp:
        marks = Path(tmp) / "marks"
        result = git(git_dir, ["fast-import", "--quiet", "--done", "--force", f"--export-marks={marks}"], input=bytes(stream), check=False)
        if result.returncode != 0:
            raise GitMirrorError("git_mirror_failed", result.stderr.decode(errors="replace").strip() or "git fast-import failed.")
        by_mark: dict[int, str] = {}
        for line in marks.read_text().splitlines():
            mark, _, sha = line.partition(" ")
            by_mark[int(mark.lstrip(":"))] = sha
    return [by_mark[number] for number in range(1, len(commits) + 1)]


def push(git_dir: Path, remote: str, sha: str, branch: str) -> None:
    """Push without force; a concurrent Git push surfaces as a retryable rejection."""
    result = git(git_dir, ["push", "--porcelain", "--quiet", remote, f"{sha}:refs/heads/{branch}"], check=False)
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).decode(errors="replace")
    if any(marker in output for marker in ("rejected", "non-fast-forward", "fetch first", "stale info")):
        raise GitMirrorError("mirror_push_rejected", "The Git branch moved during sync; fetch and retry.", retryable=True, remote=remote, branch=branch)
    raise GitMirrorError("git_mirror_unreachable", output.strip() or "git push failed.", retryable=True, remote=remote)
