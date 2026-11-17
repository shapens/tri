"""Directory-level, content-addressed tree nodes.

A snapshot's tree is stored as one node per directory, ``{"d": {name: tree_id},
"f": {name: entry}}``. Unchanged directories hash to the same node, so an edit
writes only the nodes on the path from the changed file to the root, instead
of re-recording every path in the repository.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import Any, Callable

HIER_PREFIX = "TRIH1:"
HIER_COMPRESSED_PREFIX = "TRIH1Z:"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def is_hierarchical(stored: str) -> bool:
    return stored.startswith(HIER_PREFIX) or stored.startswith(HIER_COMPRESSED_PREFIX)


def encode_node(node: dict[str, Any]) -> tuple[str, str]:
    """Return ``(digest, stored_text)`` for a node payload."""
    raw = _dumps(node)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    compressed = base64.b64encode(zlib.compress(raw.encode())).decode()
    if len(compressed) + len(HIER_COMPRESSED_PREFIX) < len(raw) + len(HIER_PREFIX):
        return digest, HIER_COMPRESSED_PREFIX + compressed
    return digest, HIER_PREFIX + raw


def decode_node(stored: str) -> dict[str, Any]:
    if stored.startswith(HIER_COMPRESSED_PREFIX):
        return json.loads(zlib.decompress(base64.b64decode(stored[len(HIER_COMPRESSED_PREFIX) :])).decode())
    return json.loads(stored[len(HIER_PREFIX) :])


def write_tree(entries: dict[str, dict[str, Any]], store_node: Callable[[str, str, str], None]) -> tuple[str, str]:
    """Write ``entries`` as nested nodes bottom-up via ``store_node(tree_id, digest, stored)``.

    Returns the root ``(tree_id, digest)``. Entry ``hash`` fields are dropped
    (they are derivable from the blob row), matching flat-tree compaction.
    """
    root: dict[str, Any] = {"d": {}, "f": {}}
    for path, entry in entries.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node["d"].setdefault(part, {"d": {}, "f": {}})
        compact = dict(entry)
        compact.pop("hash", None)
        node["f"][parts[-1]] = compact

    def emit(node: dict[str, Any]) -> tuple[str, str]:
        payload = {"d": {name: emit(child)[0] for name, child in node["d"].items()}, "f": node["f"]}
        digest, stored = encode_node(payload)
        tree_id = f"tree_{digest[:16]}"
        store_node(tree_id, digest, stored)
        return tree_id, digest

    return emit(root)


def flatten(tree_id: str, load_node: Callable[[str], dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Return ``(entries, node_ids)`` for a hierarchical tree."""
    entries: dict[str, dict[str, Any]] = {}
    nodes: set[str] = set()

    def walk(node_id: str, prefix: str) -> None:
        nodes.add(node_id)
        node = load_node(node_id)
        for name, entry in node.get("f", {}).items():
            entries[prefix + name] = dict(entry)
        for name, child in node.get("d", {}).items():
            walk(child, prefix + name + "/")

    walk(tree_id, "")
    return dict(sorted(entries.items())), nodes
