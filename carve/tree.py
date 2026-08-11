"""Work out which files are fair game for removal.

Called by: carve/reduce.py (to build the universe), carve/workspace.py (to copy).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Directories that are build output, dependency caches or version-control
# internals.  Deleting these from a reproduction proves nothing, and walking
# them costs real time on a large tree.
PRUNE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".bzr",
        "node_modules", "bower_components", "vendor",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        ".venv", "venv", "env", ".eggs", "*.egg-info",
        "target", "dist", "build", "out", ".next", ".nuxt", ".svelte-kit",
        ".gradle", ".idea", ".vscode", ".terraform", ".serverless",
        ".cache", "coverage", ".nyc_output", ".dart_tool", "Pods",
    }
)

# A file bigger than this is kept whole: line-level reduction of a 5 MB blob
# costs far more than it can possibly save.
DEFAULT_MAX_TEXT_BYTES = 1024 * 1024

_TEXT_PROBE = 8192


@dataclass
class Entry:
    """One file in the universe."""

    path: str          # POSIX-style, relative to the root
    size: int
    is_text: bool
    pinned: bool = False   # never removed, never edited

    @property
    def reducible(self) -> bool:
        return self.is_text and not self.pinned


def _looks_binary(blob: bytes) -> bool:
    if b"\x00" in blob:
        return True
    try:
        blob.decode("utf-8")
    except UnicodeDecodeError:
        # A truncated multi-byte character at the probe boundary is not
        # evidence of a binary file; anything else is.
        try:
            blob[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return True
    return False


def _git_files(root: str) -> Optional[List[str]]:
    """Tracked plus untracked-but-not-ignored files, or None if not a repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]


def _pruned(name: str) -> bool:
    if name in PRUNE_DIRS:
        return True
    return any(p.startswith("*") and name.endswith(p[1:]) for p in PRUNE_DIRS)


def _walk_files(root: str) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _pruned(d))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    base = os.path.basename(path)
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
            return True
        # `--keep src` should keep everything under src/.
        if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def discover(
    root: str,
    only: Sequence[str] = (),
    keep: Sequence[str] = (),
    exclude: Sequence[str] = (),
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
) -> List[Entry]:
    """Every file under `root` that carve is allowed to touch.

    `keep` pins files (present, never edited).  `only` restricts reduction to
    matching files — everything else is pinned.  `exclude` drops files from the
    universe entirely, as though carve had never seen them.
    """
    root = os.path.abspath(root)
    names = _git_files(root)
    if names is None:
        names = _walk_files(root)
    else:
        names = [n for n in names if not any(_pruned(part) for part in n.split("/"))]

    entries: List[Entry] = []
    for name in sorted(set(names)):
        full = os.path.join(root, name)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        if exclude and _matches_any(name, exclude):
            continue
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as handle:
                probe = handle.read(_TEXT_PROBE)
        except OSError:
            continue
        is_text = size <= max_text_bytes and not _looks_binary(probe)
        pinned = bool(keep) and _matches_any(name, keep)
        if only and not _matches_any(name, only):
            pinned = True
        entries.append(Entry(path=name, size=size, is_text=is_text, pinned=pinned))
    return entries


def read_all(root: str, entries: Sequence[Entry]) -> Dict[str, bytes]:
    """Slurp the universe into memory once, so candidates are cheap to build."""
    contents: Dict[str, bytes] = {}
    for entry in entries:
        try:
            with open(os.path.join(root, entry.path), "rb") as handle:
                contents[entry.path] = handle.read()
        except OSError:
            continue
    return contents


def count_lines(blob: bytes) -> int:
    if not blob:
        return 0
    count = blob.count(b"\n")
    return count if blob.endswith(b"\n") else count + 1
