"""Gitignore-style path rules used to decide which new files a run, sync, or
salvage may import, and which directories a scan may skip."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class _Rule:
    base: str  # directory the rule is relative to ("" for root)
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


def _translate(glob: str) -> str:
    """Translate a gitignore glob (without anchoring) into a regex body."""
    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        if glob.startswith("**/", i) and (i == 0 or glob[i - 1] == "/"):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
            continue
        if glob.startswith("**", i) and i + 2 == n and (i == 0 or glob[i - 1] == "/"):
            out.append(".*")
            i += 2
            continue
        c = glob[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = glob.find("]", i + 2 if i + 1 < n and glob[i + 1] in "!^" else i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : end]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _parse_line(line: str, base: str) -> _Rule | None:
    line = line.rstrip("\n").rstrip("\r")
    if not line.strip() or line.startswith("#"):
        return None
    # Trailing spaces are ignored unless escaped.
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    negated = False
    if line.startswith("!"):
        negated = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.lstrip("/")
    body = _translate(line)
    if not anchored:
        body = "(?:.*/)?" + body
    return _Rule(base=base, regex=re.compile(body + r"\Z", re.DOTALL), negated=negated, dir_only=dir_only)


class IgnoreRules:
    """Ordered gitignore rules; the last matching rule decides."""

    def __init__(self, rules: Iterable[_Rule]) -> None:
        self.rules = list(rules)
        self._dir_cache: dict[str, bool] = {}

    @classmethod
    def from_patterns(cls, patterns: Iterable[str], base: str = "") -> "IgnoreRules":
        return cls(rule for rule in (_parse_line(p, base) for p in patterns) if rule is not None)

    @classmethod
    def from_entries(
        cls,
        entries: dict[str, dict],
        read_text: Callable[[str], str | None],
        extra_patterns: Iterable[str] = (),
    ) -> "IgnoreRules":
        """Build rules from every ``.gitignore`` in a snapshot plus extra root patterns.

        Extra (project-declared) patterns have the lowest precedence, like
        ``.git/info/exclude``. Shallower ``.gitignore`` files come before deeper ones.
        """
        rules = [rule for rule in (_parse_line(p, "") for p in extra_patterns) if rule is not None]
        ignore_files = sorted(
            (path for path in entries if posixpath.basename(path) == ".gitignore"),
            key=lambda p: (p.count("/"), p),
        )
        for path in ignore_files:
            text = read_text(path)
            if text is None:
                continue
            base = posixpath.dirname(path)
            for line in text.splitlines():
                rule = _parse_line(line, base)
                if rule is not None:
                    rules.append(rule)
        return cls(rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def _decide(self, path: str, is_dir: bool) -> bool:
        ignored = False
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.base:
                prefix = rule.base + "/"
                if not path.startswith(prefix):
                    continue
                rel = path[len(prefix) :]
            else:
                rel = path
            if rule.regex.match(rel):
                ignored = not rule.negated
        return ignored

    def dir_ignored(self, path: str) -> bool:
        """True when directory ``path`` (or any parent) is ignored."""
        cached = self._dir_cache.get(path)
        if cached is not None:
            return cached
        parent = posixpath.dirname(path)
        result = (bool(parent) and self.dir_ignored(parent)) or self._decide(path, True)
        self._dir_cache[path] = result
        return result

    def matches(self, path: str, is_dir: bool = False) -> bool:
        if not self.rules:
            return False
        parent = posixpath.dirname(path)
        if parent and self.dir_ignored(parent):
            return True
        if is_dir:
            return self.dir_ignored(path)
        return self._decide(path, False)
