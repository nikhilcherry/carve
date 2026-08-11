"""Write the reduced tree, the paste-ready report, and the terminal summary.

Called by: carve/cli.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import List, Optional

from . import __version__
from .reduce import Outcome
from .runner import describe
from .term import Style, human_bytes, human_time, percent_drop, plural
from .tree import count_lines

# Extension -> markdown fence language, for the inlined sources.
FENCE = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".go": "go",
    ".rs": "rust", ".rb": "ruby", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".sql": "sql", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".md": "markdown", ".html": "html", ".css": "css",
    ".xml": "xml", ".tf": "hcl", ".lua": "lua", ".pl": "perl",
    ".scala": "scala", ".dart": "dart", ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
}

# Beyond this a file is summarised rather than inlined; a report nobody can
# read is not a report.
INLINE_LIMIT = 20_000


def fence_for(path: str) -> str:
    name = os.path.basename(path).lower()
    if name == "dockerfile":
        return "dockerfile"
    if name in ("makefile", "justfile"):
        return "make"
    return FENCE.get(os.path.splitext(name)[1], "")


def _safe_output_dir(out_dir: str, root: str) -> str:
    out_dir = os.path.abspath(out_dir)
    root = os.path.abspath(root)
    if out_dir == root:
        raise ValueError("the output directory must not be the source tree")
    if root == os.path.dirname(out_dir) and os.path.basename(out_dir) == "":
        raise ValueError("bad output directory")
    if root.startswith(out_dir.rstrip("/") + "/"):
        raise ValueError(
            "the output directory {0} contains the source tree".format(out_dir))
    if out_dir in ("/", os.path.expanduser("~")):
        raise ValueError("refusing to write the reduction to {0}".format(out_dir))
    return out_dir


def write_tree(outcome: Outcome, out_dir: str, force: bool = False) -> str:
    """Materialise the minimal reproduction on disk."""
    out_dir = _safe_output_dir(out_dir, outcome.root)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        if not force:
            raise ValueError(
                "{0} already exists and is not empty — pass --force to "
                "overwrite it".format(out_dir))
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    for path in sorted(outcome.state):
        target = os.path.join(out_dir, path)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(outcome.state[path])
    return out_dir


def free_name(outcome: Outcome, out_dir: str, name: str) -> str:
    """A path for carve's own output that does not clobber a reduced file.

    A repository containing its own `REPRO.md` is not far-fetched, and quietly
    overwriting it would corrupt the one thing carve promises still runs.
    """
    kept = set(outcome.state)
    stem, ext = os.path.splitext(name)
    candidate = name
    suffix = 0
    while candidate in kept:
        suffix += 1
        candidate = "{0}.carve{1}{2}".format(
            stem, "" if suffix == 1 else "-{0}".format(suffix), ext)
    return os.path.join(out_dir, candidate)


def as_dict(outcome: Outcome) -> dict:
    return {
        "carve_version": __version__,
        "root": outcome.root,
        "command": outcome.command,
        "command_line": describe(outcome.command),
        "original_command": outcome.original_command,
        "oracle": outcome.oracle.to_dict(),
        "before": outcome.before.to_dict(),
        "after": outcome.after.to_dict(),
        "runs": outcome.runs,
        "cache_hits": outcome.cache_hits,
        "seconds": round(outcome.seconds, 2),
        "verified": outcome.verified,
        "truncated": outcome.truncated,
        "notes": outcome.notes,
        "files": [
            {"path": path,
             "lines": count_lines(outcome.state[path]),
             "bytes": len(outcome.state[path])}
            for path in outcome.kept_files
        ],
        "removed_files": outcome.removed_files,
    }


def write_json(outcome: Outcome, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(as_dict(outcome), handle, indent=2)
        handle.write("\n")


def _decode(blob: bytes) -> Optional[str]:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def render_markdown(outcome: Outcome, inline: bool = True) -> str:
    before, after = outcome.before, outcome.after
    lines: List[str] = []
    add = lines.append

    add("# Minimal reproduction")
    add("")
    add("Reduced by [carve](https://github.com/nikhilcherry/carve) v{0} "
        "from `{1}`.".format(__version__, os.path.basename(outcome.root)))
    add("")
    add("## The command")
    add("")
    add("```console")
    add("$ " + describe(outcome.command))
    add("```")
    add("")
    if outcome.command != outcome.original_command:
        add("Shortened from `{0}` — the arguments carve dropped made no "
            "difference to the failure.".format(describe(
                outcome.original_command)))
        add("")
    add("## Still fails the same way")
    add("")
    for clause in outcome.oracle.explain():
        add("- {0}".format(clause))
    add("")
    add("## What was left")
    add("")
    add("| | files | lines | bytes |")
    add("| --- | ---: | ---: | ---: |")
    add("| before | {0:,} | {1:,} | {2:,} |".format(
        before.files, before.lines, before.bytes))
    add("| after | {0:,} | {1:,} | {2:,} |".format(
        after.files, after.lines, after.bytes))
    add("| removed | {0} | {1} | {2} |".format(
        percent_drop(before.files, after.files),
        percent_drop(before.lines, after.lines),
        percent_drop(before.bytes, after.bytes)))
    add("")
    add("{0} test runs in {1}.".format(outcome.runs,
                                       human_time(outcome.seconds)))
    add("")
    if not outcome.verified:
        add("> **The final tree failed re-verification.** Check it by hand "
            "before trusting it.")
        add("")
    if outcome.truncated:
        add("> Reduction stopped before it finished, so this is not "
            "guaranteed minimal.")
        add("")
    for note in outcome.notes:
        add("> {0}".format(note))
        add("")

    add("## The files")
    add("")
    for path in outcome.kept_files:
        blob = outcome.state[path]
        add("### `{0}`  ({1}, {2})".format(
            path, plural(count_lines(blob), "line"), human_bytes(len(blob))))
        add("")
        if not inline:
            continue
        if len(blob) > INLINE_LIMIT:
            add("_{0} — too large to inline._".format(human_bytes(len(blob))))
            add("")
            continue
        text = _decode(blob)
        if text is None:
            add("_binary, {0}_".format(human_bytes(len(blob))))
            add("")
            continue
        if not text.strip():
            add("_empty — the file has to exist, but nothing in it matters._")
            add("")
            continue
        add("```{0}".format(fence_for(path)))
        add(text.rstrip("\n"))
        add("```")
        add("")

    add("## Original failure")
    add("")
    add("```")
    lines.extend(outcome.baseline.output.strip().splitlines()[-40:])
    add("```")
    return "\n".join(lines) + "\n"


def write_markdown(outcome: Outcome, path: str, inline: bool = True) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(outcome, inline))


# -- terminal --------------------------------------------------------------


def print_summary(outcome: Outcome, style: Style, out_dir: Optional[str] = None,
                  stream=None) -> None:
    stream = stream or sys.stdout
    before, after = outcome.before, outcome.after

    def line(text: str = "") -> None:
        stream.write(text + "\n")

    def row(label: str, was: str, now: str, drop: str) -> None:
        line("    {0:<7}{1:>12}  →  {2}{3}".format(
            label, was, style.bold(now), style.grey("   " + drop + " gone")))

    line()
    line(style.bold("  carved down to"))
    line()
    row("files", "{0:,}".format(before.files), "{0:,}".format(after.files),
        percent_drop(before.files, after.files))
    row("lines", "{0:,}".format(before.lines), "{0:,}".format(after.lines),
        percent_drop(before.lines, after.lines))
    row("bytes", human_bytes(before.bytes), human_bytes(after.bytes),
        percent_drop(before.bytes, after.bytes))
    line()

    for path in outcome.kept_files:
        blob = outcome.state[path]
        count = count_lines(blob)
        marker = style.yellow("○") if not blob.strip() else style.grey("·")
        detail = "empty" if not blob.strip() else "{0:,} lines".format(count)
        line("    {0} {1} {2}".format(marker, path.ljust(48)[:48],
                                      style.grey(detail)))
    if outcome.command != outcome.original_command:
        line("    " + style.grey("command  ") + "$ " + describe(outcome.command))
    line()
    line(style.grey("    {0} runs · {1} cached · {2}".format(
        outcome.runs, outcome.cache_hits, human_time(outcome.seconds))))
    if outcome.truncated:
        line(style.yellow("    stopped early — not guaranteed minimal"))
    if not outcome.verified:
        line(style.red("    the reduced tree did not re-verify; check it by hand"))
    for note in outcome.notes:
        line(style.yellow("    note: " + note))
    if out_dir:
        line()
        line("    " + style.cyan(out_dir))
    line()
