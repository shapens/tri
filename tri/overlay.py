"""Linux overlayfs backend for ephemeral runs.

A run's directory is an overlay mount that exists only inside a private mount
namespace created for the command: lower layers are a shared, read-only base
tree (hard links into the checkout cache) plus any environment layers; the
upper layer receives only what the command writes. Nothing needs root: on
kernels >= 5.11 an unprivileged user namespace may mount overlayfs with the
``userxattr`` option. When namespaces are blocked (common in containers) the
probe fails and callers fall back to clone or copy.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from tri.materialize import (
    FIXED_MTIME,
    Attachment,
    CheckoutCache,
    Materialization,
    MaterializeError,
    _entry_same,
    _parent_dirs,
    _under,
    remove_tree,
    stat_key,
    symlink_target_escapes,
)

_FIXED_NS = FIXED_MTIME * 1_000_000_000
_OVERLAY_SCRIPT = (
    'set -e; mount -t overlay overlay -o "$1" "$2"; '
    'while IFS= read -r p; do [ -n "$p" ] && rm -rf -- "$2/$p"; done < "$3"; '
    'cd "$2"; shift 3; exec "$@"'
)

_probe_cache: dict[int, dict[str, Any] | None] = {}


def overlay_platform() -> bool:
    return sys.platform.startswith("linux") and shutil.which("unshare") is not None


def _namespace_prefix(mode: str) -> list[str]:
    if mode == "root":
        return ["unshare", "--mount", "--propagation", "private", "--"]
    return ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private", "--"]


def _mount_options(lowers: Iterable[Path], upper: Path, workdir: Path, userxattr: bool) -> str:
    options = f"lowerdir={':'.join(str(p) for p in lowers)},upperdir={upper},workdir={workdir}"
    if userxattr:
        options += ",userxattr"
    return options


def _unsafe_for_options(path: Path) -> bool:
    text = str(path)
    return any(ch in text for ch in (",", ":", "\n", "\\"))


def probe_overlay(directory: Path) -> dict[str, Any] | None:
    """Return ``{"mode": "root"|"userns", "userxattr": bool}`` if overlay runs work here."""
    if not overlay_platform():
        return None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        dev = directory.stat().st_dev
    except OSError:
        return None
    if dev in _probe_cache:
        return _probe_cache[dev]
    forced = os.environ.get("TRI_OVERLAY_PROBE")
    if forced == "0":
        _probe_cache[dev] = None
        return None
    candidates: list[tuple[str, bool]] = []
    if os.geteuid() == 0:
        candidates.append(("root", False))
    candidates += [("userns", True), ("userns", False)]
    result: dict[str, Any] | None = None
    for mode, userxattr in candidates:
        probe = directory / f".tri-overlay-probe-{uuid.uuid4().hex}"
        try:
            lower, upper, work, merged = (probe / name for name in ("lower", "upper", "work", "merged"))
            for p in (lower, upper, work, merged):
                p.mkdir(parents=True)
            (lower / "keep").write_text("k")
            (lower / "gone").write_text("g")
            deletions = probe / "deletions"
            deletions.write_text("gone\n")
            if any(_unsafe_for_options(p) for p in (lower, upper, work)):
                break
            argv = _namespace_prefix(mode) + [
                "sh",
                "-c",
                _OVERLAY_SCRIPT,
                "tri-overlay",
                _mount_options([lower], upper, work, userxattr),
                str(merged),
                str(deletions),
                "sh",
                "-c",
                "echo new > new && cat keep >/dev/null",
            ]
            proc = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
            if proc.returncode == 0 and (upper / "new").is_file() and _is_whiteout(upper / "gone") and not (lower / "new").exists():
                result = {"mode": mode, "userxattr": userxattr}
        except (OSError, subprocess.TimeoutExpired):
            result = None
        finally:
            remove_tree(probe)
        if result is not None:
            break
    _probe_cache[dev] = result
    return result


def _is_whiteout(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
        return True
    if stat.S_ISREG(st.st_mode) and st.st_size == 0:
        for name in ("user.overlay.whiteout", "trusted.overlay.whiteout"):
            try:
                os.getxattr(path, name, follow_symlinks=False)
                return True
            except OSError:
                continue
    return False


def _is_opaque(path: Path) -> bool:
    for name in ("user.overlay.opaque", "trusted.overlay.opaque"):
        try:
            if os.getxattr(path, name, follow_symlinks=False) in (b"y", b"x"):
                return True
        except OSError:
            continue
    return False


@dataclass
class OverlayMaterialization(Materialization):
    run_root: Path = field(default_factory=Path)
    upper: Path = field(default_factory=Path)
    workdir: Path = field(default_factory=Path)
    lowers: list[str] = field(default_factory=list)
    deletions_file: Path = field(default_factory=Path)
    mode: str = "userns"
    userxattr: bool = True
    prepopulated: dict[str, list[int]] = field(default_factory=dict)

    @property
    def writable_root(self) -> Path:
        """Where Tri may drop files (e.g. generated configs) before the command starts."""
        return self.upper

    @property
    def content_root(self) -> Path:
        return self.upper

    def wrap(self, argv: list[str]) -> list[str]:
        return _namespace_prefix(self.mode) + [
            "sh",
            "-c",
            _OVERLAY_SCRIPT,
            "tri-overlay",
            _mount_options([Path(p) for p in self.lowers], self.upper, self.workdir, self.userxattr),
            str(self.path),
            str(self.deletions_file),
            *argv,
        ]

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["overlay"] = {"mode": self.mode, "userxattr": self.userxattr, "layers": len(self.lowers)}
        return data

    def save_baseline(self, file: Path) -> None:
        file = Path(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "backend": "overlay",
            "degraded": False,
            "clone_method": None,
            "skip_prefixes": self.skip_prefixes,
            "skipped": self.skipped,
            "baseline": {},
            "overlay": {
                "run_root": str(self.run_root),
                "upper": str(self.upper),
                "workdir": str(self.workdir),
                "lowers": self.lowers,
                "deletions_file": str(self.deletions_file),
                "mode": self.mode,
                "userxattr": self.userxattr,
                "prepopulated": self.prepopulated,
            },
        }
        tmp = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, file)

    def refresh_baseline(self) -> None:
        return None

    def scan(self, entries: dict[str, dict[str, Any]], ignore: Any = None) -> tuple[list[str], list[str]]:
        return scan_upper(self.upper, entries, self.prepopulated, ignore=ignore, skip_prefixes=self.skip_prefixes, skipped=self.skipped)

    def release(self) -> None:
        remove_tree(self.run_root)


def overlay_from_payload(path: Path, payload: dict[str, Any]) -> OverlayMaterialization:
    info = payload["overlay"]
    return OverlayMaterialization(
        path=Path(path),
        backend="overlay",
        degraded=False,
        skip_prefixes=list(payload.get("skip_prefixes", [])),
        skipped=list(payload.get("skipped", [])),
        run_root=Path(info["run_root"]),
        upper=Path(info["upper"]),
        workdir=Path(info["workdir"]),
        lowers=list(info["lowers"]),
        deletions_file=Path(info["deletions_file"]),
        mode=info.get("mode", "userns"),
        userxattr=bool(info.get("userxattr", True)),
        prepopulated={k: list(v) for k, v in info.get("prepopulated", {}).items()},
    )


def scan_upper(
    upper: Path,
    entries: dict[str, dict[str, Any]],
    prepopulated: dict[str, list[int]],
    *,
    ignore: Any = None,
    skip_prefixes: Iterable[str] = (),
    skipped: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Changes of an overlay run, read from its upper layer only."""
    skip = set(skip_prefixes)
    skipped_set = set(skipped)
    tracked_dirs = _parent_dirs(entries)
    changed: list[str] = []
    deleted: set[str] = set()
    present: set[str] = set()

    def drop_under(rel: str) -> None:
        for path in entries:
            if (path == rel or path.startswith(rel + "/")) and path not in present:
                deleted.add(path)

    def walk(directory: Path, rel: str) -> None:
        try:
            it = os.scandir(directory)
        except OSError:
            return
        with it:
            for item in it:
                child = f"{rel}/{item.name}" if rel else item.name
                if item.name == ".git" or child in skip:
                    continue
                full = Path(item.path)
                if _is_whiteout(full):
                    drop_under(child)
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        if child in entries:
                            deleted.add(child)
                        if ignore is not None and child not in tracked_dirs and ignore.dir_ignored(child):
                            continue
                        walk(full, child)
                        if _is_opaque(full):
                            drop_under(child)
                        continue
                    if not (item.is_file(follow_symlinks=False) or item.is_symlink()):
                        continue
                    present.add(child)
                    if child in tracked_dirs:
                        drop_under(child)
                    key = prepopulated.get(child)
                    if key is not None and key == stat_key(item.stat(follow_symlinks=False)):
                        continue
                    if child not in entries and ignore is not None and ignore.matches(child):
                        continue
                    changed.append(child)
                except OSError:
                    continue

    walk(Path(upper), "")
    deleted -= present
    final_deleted = sorted(p for p in deleted if p not in skipped_set and not _under(p, skip))
    return sorted(changed), final_deleted


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EMLINK, errno.EPERM, errno.ENOTSUP, errno.EACCES):
            raise
        shutil.copy2(src, dst)


def ensure_link_base(bases: Path, cache: CheckoutCache, read_blob: Callable[[str], bytes], tree_id: str, entries: dict[str, dict[str, Any]]) -> Path:
    """A read-only lower layer for ``tree_id``: hard links into the checkout cache."""
    final = bases / f"{tree_id}.links"
    if final.is_dir():
        try:
            os.utime(final)
        except OSError:
            pass
        return final
    bases.mkdir(parents=True, exist_ok=True)
    tmp = bases / f".{tree_id}.links.{uuid.uuid4().hex}.tmp"
    tmp.mkdir()
    try:
        for path, entry in entries.items():
            target = tmp / path
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = entry.get("mode", "100644")
            if mode == "120000":
                link = entry.get("symlink_target")
                if not isinstance(link, str):
                    link = read_blob(entry["blob"]).decode(errors="replace")
                os.symlink(link, target)
                continue
            digest = entry.get("hash")
            if isinstance(digest, str):
                _link_or_copy(cache.path_for(digest, entry["blob"], mode == "100755"), target)
            else:
                target.write_bytes(read_blob(entry["blob"]))
                os.chmod(target, 0o755 if mode == "100755" else 0o644)
                os.utime(target, ns=(_FIXED_NS, _FIXED_NS))
        os.rename(tmp, final)
    except OSError:
        remove_tree(tmp)
        if not final.is_dir():
            raise
    return final


def prepare_overlay_run(
    *,
    run_root: Path,
    bases: Path,
    cache: CheckoutCache,
    read_blob: Callable[[str], bytes],
    entries: dict[str, dict[str, Any]],
    base: tuple[str, dict[str, dict[str, Any]]],
    attachments: Iterable[Attachment],
    info: dict[str, Any],
) -> OverlayMaterialization:
    """Lay out lower/upper/work dirs for an overlay run of ``entries`` (the head)."""
    base_id, base_entries = base
    run_root = Path(run_root)
    merged = run_root / "work"
    upper = run_root / "upper"
    workdir = run_root / "ovl-work"
    for p in (merged, upper, workdir):
        p.mkdir(parents=True, exist_ok=True)
    for path, entry in entries.items():
        if entry.get("mode") != "120000":
            continue
        link = entry.get("symlink_target")
        if not isinstance(link, str):
            link = read_blob(entry["blob"]).decode(errors="replace")
        if symlink_target_escapes(merged, merged / path, link):
            raise MaterializeError("path_escaped_mount", "Stored symlink target escapes the materialized workspace root.", object=path, target=link)
    # Type conflicts (file <-> directory) between base and head cannot be expressed
    # as simple upper files plus deletions; callers fall back to clone/copy.
    head_dirs = _parent_dirs(entries)
    base_dirs = _parent_dirs(base_entries)
    if any(path in head_dirs for path in base_entries) or any(path in base_dirs for path in entries):
        raise MaterializeError("overlay_unsuitable", "Base and head disagree on file vs directory paths.")
    base_dir = ensure_link_base(bases, cache, read_blob, base_id, base_entries)
    lowers: list[str] = []
    attached: list[str] = []
    notes: list[dict[str, Any]] = []
    for attachment in attachments:
        if any(p == attachment.rel or p.startswith(attachment.rel + "/") for p in entries):
            notes.append({"code": "attachment_conflict", "path": attachment.rel, "message": "Snapshot already has files at this path; attachment skipped."})
            continue
        layer_root = Path(attachment.source)
        for _ in attachment.rel.split("/"):
            layer_root = layer_root.parent
        lowers.append(str(layer_root))
        attached.append(attachment.rel)
    lowers.append(str(base_dir))
    if any(_unsafe_for_options(Path(p)) for p in [*lowers, str(upper), str(workdir)]):
        raise MaterializeError("overlay_unsuitable", "Store path contains characters overlay mount options cannot carry.")
    deletions = [path for path, entry in base_entries.items() if path not in entries]
    prepopulated: dict[str, list[int]] = {}
    for path, entry in entries.items():
        if _entry_same(entry, base_entries.get(path)):
            continue
        target = upper / path
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = entry.get("mode", "100644")
        if mode == "120000":
            link = entry.get("symlink_target")
            if not isinstance(link, str):
                link = read_blob(entry["blob"]).decode(errors="replace")
            os.symlink(link, target)
        else:
            target.write_bytes(read_blob(entry["blob"]))
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
            os.utime(target, ns=(_FIXED_NS, _FIXED_NS))
        prepopulated[path] = stat_key(os.lstat(target))
    deletions_file = run_root / "deletions.txt"
    deletions_file.write_text("".join(f"{p}\n" for p in deletions))
    return OverlayMaterialization(
        path=merged,
        backend="overlay",
        degraded=False,
        skip_prefixes=attached,
        notes=notes,
        run_root=run_root,
        upper=upper,
        workdir=workdir,
        lowers=lowers,
        deletions_file=deletions_file,
        mode=info["mode"],
        userxattr=bool(info["userxattr"]),
        prepopulated=prepopulated,
    )
