"""The reduction itself: files first, then blocks, then lines.

Called by: carve/cli.py; the `Outcome` it returns is rendered by carve/report.py.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Set, Tuple

from . import oracle as oracle_mod
from .ddmin import ddmin
from .runner import RunResult, run
from .tree import Entry, count_lines, discover, read_all
from .workspace import BudgetExhausted, Pool, State, copy_tree

# Line starts that are comments in enough languages to be worth one probe.
_COMMENT = re.compile(rb"^[ \t]*(#|//|--|;;|/\*|\*/|<!--)")
_BLANK = re.compile(rb"^[ \t\r\n]*$")

# Identifiers, numbers, runs of whitespace, and every other character alone.
# Enough structure to delete one argument or one clause at a time, with no
# idea of any particular grammar.
_TOKEN = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*|\d+|[ \t]+|.")

# Below this a line has nothing worth taking apart; above the token cap it
# would cost more probes than the result is worth.
MIN_TOKEN_LINE = 20
MAX_LINE_TOKENS = 80

LEVELS = ("files", "blocks", "lines", "chars")


@dataclass
class Stats:
    files: int
    lines: int
    bytes: int

    def to_dict(self) -> dict:
        return {"files": self.files, "lines": self.lines, "bytes": self.bytes}


@dataclass
class Outcome:
    root: str
    command: List[str]
    oracle: oracle_mod.Oracle
    baseline: RunResult
    before: Stats
    after: Stats
    state: State
    removed_files: List[str]
    runs: int
    cache_hits: int
    seconds: float
    verified: bool
    truncated: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def kept_files(self) -> List[str]:
        return sorted(self.state)


def measure(state: State) -> Stats:
    return Stats(
        files=len(state),
        lines=sum(count_lines(blob) for blob in state.values()),
        bytes=sum(len(blob) for blob in state.values()),
    )


# -- block detection -------------------------------------------------------


def indent_blocks(lines: Sequence[bytes]) -> List[Tuple[int, int]]:
    """Half-open ranges covering a line and everything indented under it."""
    total = len(lines)
    indents: List[Optional[int]] = []
    for line in lines:
        if not line.strip():
            indents.append(None)
        else:
            expanded = line.replace(b"\t", b"    ")
            indents.append(len(expanded) - len(expanded.lstrip()))

    blocks: List[Tuple[int, int]] = []
    for start in range(total):
        depth = indents[start]
        if depth is None:
            continue
        end = start + 1
        while end < total and (indents[end] is None or indents[end] > depth):
            end += 1
        while end - 1 > start and indents[end - 1] is None:
            end -= 1
        if end - start >= 2:
            blocks.append((start, end))
    return blocks


def brace_blocks(lines: Sequence[bytes]) -> List[Tuple[int, int]]:
    """Half-open ranges spanning a `{ ... }` that opens at the end of a line.

    Braces inside strings and comments are miscounted on purpose: a wrong guess
    costs one rejected probe, and the heuristic needs no parser.
    """
    blocks: List[Tuple[int, int]] = []
    total = len(lines)
    for start in range(total):
        if not lines[start].rstrip().endswith(b"{"):
            continue
        depth = 0
        for end in range(start, total):
            depth += lines[end].count(b"{") - lines[end].count(b"}")
            if depth <= 0:
                if end - start >= 2:
                    blocks.append((start, end + 1))
                break
    return blocks


def candidate_blocks(lines: Sequence[bytes]) -> List[Set[int]]:
    """Every block worth trying to delete, biggest first."""
    seen: Set[Tuple[int, int]] = set()
    unique: List[Tuple[int, int]] = []
    for span in indent_blocks(lines) + brace_blocks(lines):
        if span not in seen:
            seen.add(span)
            unique.append(span)
    unique.sort(key=lambda span: (span[0] - span[1], span[0]))
    return [set(range(a, b)) for a, b in unique]


# -- the reducer -----------------------------------------------------------


class Reducer:
    """Owns the current best-known reproducing state."""

    def __init__(
        self,
        pool: Pool,
        originals: State,
        pinned: Sequence[str],
        reducible: Sequence[str],
        level: str = "lines",
        passes: int = 3,
        on_event: Optional[Callable[..., None]] = None,
    ) -> None:
        self.pool = pool
        self.originals = originals
        self.pinned = set(pinned)
        self.reducible = set(reducible)
        self.level = level
        self.passes = max(1, passes)
        self.on_event = on_event or (lambda *a, **k: None)
        self.current: State = dict(originals)

    # -- candidate construction -------------------------------------------

    def _with_files(self, kept: Sequence[str]) -> State:
        state = {path: blob for path, blob in self.current.items()
                 if path in self.pinned}
        for path in kept:
            state[path] = self.current[path]
        return state

    def _with_content(self, path: str, blob: bytes) -> State:
        state = dict(self.current)
        state[path] = blob
        return state

    def _join(self, path: str, lines: Sequence[bytes],
              keep: Sequence[int]) -> State:
        return self._with_content(path, b"".join(lines[i] for i in keep))

    # -- pass one: which files matter at all -------------------------------

    def reduce_files(self) -> None:
        removable = [p for p in sorted(self.current) if p not in self.pinned]
        if len(removable) < 2:
            return
        self.on_event("phase", name="files", total=len(removable))

        def evaluate(candidates: List[List[str]]) -> Optional[int]:
            return self.pool.first_accepted(
                [self._with_files(c) for c in candidates])

        kept = ddmin(
            removable, evaluate, allow_empty=True,
            on_progress=lambda size: self.on_event("shrink", unit="files",
                                                   size=size),
        )
        self.current = self._with_files(kept)

    # -- pass two: what inside each file matters ---------------------------

    def reduce_contents(self) -> None:
        targets = [p for p in self.current
                   if p in self.reducible and self.current[p]]
        targets.sort(key=lambda p: (-len(self.current[p]), p))
        for path in targets:
            self.reduce_file(path)

    def reduce_file(self, path: str) -> None:
        lines = self.current[path].splitlines(keepends=True)
        if not lines:
            return
        self.on_event("phase", name="file", path=path, total=len(lines))

        # The cheapest probe first: it often collapses a whole file.
        if self.pool.test(self._with_content(path, b"")):
            self.current[path] = b""
            self.on_event("shrink", unit="lines", size=0, path=path)
            return

        alive = list(range(len(lines)))
        cosmetic = {
            i for i in alive
            if _BLANK.match(lines[i])
            or (_COMMENT.match(lines[i])
                and not (i == 0 and lines[i].startswith(b"#!")))
        }
        if cosmetic and len(cosmetic) < len(alive):
            trial = [i for i in alive if i not in cosmetic]
            if self.pool.test(self._join(path, lines, trial)):
                alive = trial

        if self.level in ("blocks", "lines", "chars"):
            groups = [g for g in candidate_blocks(lines) if len(g) > 1]
            alive = self._greedy(path, lines, alive, groups)

        if self.level in ("lines", "chars"):
            def evaluate(candidates: List[List[int]]) -> Optional[int]:
                return self.pool.first_accepted(
                    [self._join(path, lines, c) for c in candidates])

            alive = ddmin(
                alive, evaluate, allow_empty=True,
                on_progress=lambda size: self.on_event(
                    "shrink", unit="lines", size=size, path=path),
            )

        self.current[path] = b"".join(lines[i] for i in alive)
        if self.level == "chars":
            self.reduce_chars(path)

    def reduce_chars(self, path: str) -> None:
        """Take surviving lines apart token by token.

        This is what turns `render(a, b, c, timeout=30, retries=5)` into
        `render(c)`.  Expensive, so it only runs at `--level chars`.
        """
        lines = self.current[path].splitlines(keepends=True)
        for index, line in enumerate(lines):
            body = line.rstrip(b"\r\n")
            tail = line[len(body):]
            if len(body) < MIN_TOKEN_LINE:
                continue
            tokens = _TOKEN.findall(body)
            if not 4 <= len(tokens) <= MAX_LINE_TOKENS:
                continue

            def evaluate(candidates: List[List[int]],
                         at: int = index) -> Optional[int]:
                states = []
                for candidate in candidates:
                    trial = list(lines)
                    trial[at] = b"".join(tokens[k] for k in candidate) + tail
                    states.append(
                        self._with_content(path, b"".join(trial)))
                return self.pool.first_accepted(states)

            kept = ddmin(list(range(len(tokens))), evaluate, allow_empty=False)
            if len(kept) < len(tokens):
                lines[index] = b"".join(tokens[k] for k in kept) + tail
                self.on_event("shrink", unit="lines", size=len(lines),
                              path=path)
        self.current[path] = b"".join(lines)

    def _greedy(self, path: str, lines: Sequence[bytes], alive: List[int],
                groups: List[Set[int]]) -> List[int]:
        """Try deleting whole blocks, several disjoint ones per round."""
        index = 0
        jobs = max(1, self.pool.jobs)
        while index < len(groups):
            live = set(alive)
            batch: List[Set[int]] = []
            claimed: Set[int] = set()
            while index < len(groups) and len(batch) < jobs:
                group = groups[index] & live
                index += 1
                if not group or group & claimed:
                    continue
                batch.append(group)
                claimed |= group
            if not batch:
                continue

            states = [self._join(path, lines, sorted(live - group))
                      for group in batch]
            accepted = [g for g, ok in zip(batch, self.pool.test_all(states))
                        if ok]
            if not accepted:
                continue

            union: Set[int] = set()
            for group in accepted:
                union |= group
            combined = sorted(live - union)
            if len(accepted) == 1 or self.pool.test(
                    self._join(path, lines, combined)):
                alive = combined
            else:
                # Individually safe deletions are not always jointly safe.
                for group in accepted:
                    trial = sorted(set(alive) - group)
                    if self.pool.test(self._join(path, lines, trial)):
                        alive = trial
            self.on_event("shrink", unit="lines", size=len(alive), path=path)
        return alive

    # -- the loop ----------------------------------------------------------

    def run(self) -> None:
        """Reduce until a whole round changes nothing."""
        for round_number in range(self.passes):
            before = self.pool.key(self.current)
            self.on_event("round", number=round_number + 1, of=self.passes)
            self.reduce_files()
            if self.level != "files":
                self.reduce_contents()
            if self.pool.key(self.current) == before:
                return


# -- top level -------------------------------------------------------------


def characterise(
    root: str,
    command: Sequence[str],
    timeout: float,
    with_git: bool = False,
    links: Sequence[str] = (),
    work_dir: Optional[str] = None,
) -> RunResult:
    """Run the command once in a scratch copy, to see what failure we have."""
    scratch = tempfile.mkdtemp(prefix="carve-probe-", dir=work_dir)
    target = os.path.join(scratch, "tree")
    try:
        copy_tree(root, target, with_git, links)
        return run(command, target, timeout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def carve(
    root: str,
    command: Sequence[str],
    *,
    only: Sequence[str] = (),
    keep: Sequence[str] = (),
    exclude: Sequence[str] = (),
    level: str = "lines",
    passes: int = 3,
    jobs: int = 1,
    timeout: Optional[float] = None,
    with_git: bool = False,
    links: Sequence[str] = (),
    work_dir: Optional[str] = None,
    max_runs: Optional[int] = None,
    time_budget: Optional[float] = None,
    expect_exit: Optional[int] = None,
    ignore_exit: bool = False,
    patterns: Sequence[str] = (),
    signatures: Sequence[str] = (),
    verify: int = 1,
    allow_flaky: bool = False,
    on_event: Optional[Callable[..., None]] = None,
) -> Outcome:
    """Shrink `root` to the least that still fails `command` the same way."""
    emit = on_event or (lambda *a, **k: None)
    root = os.path.abspath(root)
    notes: List[str] = []

    entries: List[Entry] = discover(root, only=only, keep=keep, exclude=exclude)
    originals = read_all(root, entries)
    if not originals:
        raise ValueError("no files found under {0}".format(root))

    emit("baseline", files=len(originals))
    baseline = characterise(root, command,
                            timeout if timeout is not None else 300.0,
                            with_git, links, work_dir)
    if timeout is None:
        timeout = max(10.0, min(300.0, baseline.duration * 5 + 5.0))

    if (baseline.exit_code == 0 and not baseline.timed_out and not patterns
            and not signatures and expect_exit is None and not ignore_exit):
        raise ValueError(
            "the command already succeeds (exit 0) — carve needs a failure.\n"
            "  If success is the interesting behaviour, pass --expect-exit 0 "
            "together with --signature or --expect.")

    the_oracle = oracle_mod.build(
        baseline, root=None, expect_exit=expect_exit, ignore_exit=ignore_exit,
        patterns=patterns, signatures=signatures,
    )
    emit("oracle", oracle=the_oracle, baseline=baseline, timeout=timeout)

    if any(root in token for token in command):
        notes.append("the command mentions the source path, so it may be "
                     "reading the original tree instead of the reduced copy")

    pinned = [e.path for e in entries if e.pinned]
    reducible = [e.path for e in entries if e.reducible]

    with Pool(
        source=root, managed=[e.path for e in entries], seed=originals,
        command=command, oracle=the_oracle, timeout=timeout, jobs=jobs,
        with_git=with_git, links=links, work_dir=work_dir,
        max_runs=max_runs, time_budget=time_budget,
        on_run=lambda n, ok: emit("run", n=n, ok=ok),
    ) as pool:
        if not pool.test(dict(originals)):
            raise ValueError(
                "the failure did not reproduce in a clean copy of the tree.\n"
                "  It may depend on absolute paths, on files carve skipped, "
                "or on state outside the directory.")
        for _ in range(max(0, verify - 1)):
            if not pool.oracle.holds(pool.run_raw(dict(originals))):
                message = ("the failure is flaky: the untouched tree did not "
                           "reproduce on a repeat run")
                if not allow_flaky:
                    raise ValueError(
                        message + ".\n  Re-run with --allow-flaky to reduce "
                        "anyway, or pin it down with --signature.")
                notes.append(message)
                break

        reducer = Reducer(pool, originals, pinned, reducible, level, passes,
                          emit)
        truncated = False
        try:
            reducer.run()
        except BudgetExhausted as exc:
            truncated = True
            notes.append("stopped early: {0}".format(exc))
        except KeyboardInterrupt:
            truncated = True
            notes.append("interrupted — reporting the best reduction so far")

        final = dict(reducer.current)
        verified = True
        for _ in range(max(1, verify)):
            if not pool.oracle.holds(pool.run_raw(final)):
                verified = False
                break

        return Outcome(
            root=root,
            command=list(command),
            oracle=pool.oracle,
            baseline=baseline,
            before=measure(originals),
            after=measure(final),
            state=final,
            removed_files=sorted(set(originals) - set(final)),
            runs=pool.runs,
            cache_hits=pool.cache_hits,
            seconds=pool.elapsed,
            verified=verified,
            truncated=truncated,
            notes=notes,
        )
