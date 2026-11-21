"""Line-level three-way merge for text files.

Tri merges whole trees path by path. When both sides changed the same file,
this module decides whether the edits touch disjoint line ranges of the common
base. If they do, the merged content is returned; if any hunks overlap or touch,
the caller keeps reporting a conflict. Adjacent hunks are treated as conflicts,
matching Git's conservative behavior.
"""

from __future__ import annotations

import difflib

# Hunk: (base_start, base_end, replacement_lines)
Hunk = tuple[int, int, list[bytes]]

MAX_TEXT_MERGE_BYTES = 4 * 1024 * 1024


def _hunks(base: list[bytes], other: list[bytes]) -> list[Hunk]:
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _apply(base: list[bytes], start: int, end: int, hunks: list[Hunk]) -> list[bytes]:
    result: list[bytes] = []
    cursor = start
    for hunk_start, hunk_end, lines in hunks:
        result.extend(base[cursor:hunk_start])
        result.extend(lines)
        cursor = hunk_end
    result.extend(base[cursor:end])
    return result


def is_mergeable_text(data: bytes) -> bool:
    if len(data) > MAX_TEXT_MERGE_BYTES or b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def merge_three_way(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
    """Return merged bytes, or None when the two sides' edits overlap."""
    if not all(is_mergeable_text(data) for data in (base, ours, theirs)):
        return None
    base_lines = base.splitlines(keepends=True)
    ours_lines = ours.splitlines(keepends=True)
    theirs_lines = theirs.splitlines(keepends=True)
    tagged = sorted(
        [(start, end, lines, 0) for start, end, lines in _hunks(base_lines, ours_lines)]
        + [(start, end, lines, 1) for start, end, lines in _hunks(base_lines, theirs_lines)],
        key=lambda item: (item[0], item[1], item[3]),
    )
    output: list[bytes] = []
    position = 0
    index = 0
    while index < len(tagged):
        group = [tagged[index]]
        group_start, group_end = tagged[index][0], tagged[index][1]
        index += 1
        while index < len(tagged) and tagged[index][0] <= group_end:
            group.append(tagged[index])
            group_end = max(group_end, tagged[index][1])
            index += 1
        sides = {side for _, _, _, side in group}
        ours_region = _apply(base_lines, group_start, group_end, [(s, e, l) for s, e, l, side in group if side == 0])
        theirs_region = _apply(base_lines, group_start, group_end, [(s, e, l) for s, e, l, side in group if side == 1])
        if len(sides) == 2 and ours_region != theirs_region:
            return None
        output.extend(base_lines[position:group_start])
        output.extend(ours_region if 0 in sides else theirs_region)
        position = group_end
    output.extend(base_lines[position:])
    return b"".join(output)
