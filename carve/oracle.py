"""Decide whether a candidate still fails *the same way*.

This is the part that makes reduction trustworthy.  A reducer that only checks
"did it fail?" happily hands you a tree that fails for a completely different
reason — often a syntax error the reducer introduced itself.  The oracle pins
the failure to its fingerprint instead.

Called by: carve/reduce.py, carve/workspace.py, carve/cli.py (the `check` path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .runner import RunResult

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_TMP = re.compile(r"/tmp/[^\s:'\"]+|/var/folders/[^\s:'\"]+|\\Temp\\[^\s:'\"]+")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TIME = re.compile(r"\b\d+(\.\d+)?\s?(ms|s|sec|secs|seconds|us|ns)\b")
_NUM = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")

# Lines that tend to name the actual failure rather than describe scaffolding.
# Deliberately not anchored on word boundaries: the most useful token in the
# whole output is often glued to its neighbours, as in `IndexError`.
_HINT = re.compile(
    r"(?i)("
    r"error|exception|traceback|assert|fail|panic|fatal|"
    r"abort|segmentation fault|core dumped|undefined|unresolved|"
    r"cannot|can't|no such|not found|unexpected|invalid|missing|"
    r"refused|denied|mismatch|conflict|expected"
    r")"
)

# `IndexError: list index out of range`, `java.lang.NullPointerException: ...`
# — a named condition with a message is the best signature there is.
_NAMED = re.compile(
    r"(?i)^[\s>|]*[\w.$]*(error|exception|fault|panic|failure)\b[^:]{0,40}:"
)

# Boilerplate that precedes every failure of its kind and names none of them.
_GENERIC = re.compile(
    r"(?i)^(traceback \(most recent call last\)|stack ?trace|"
    r"during handling of the above|the above exception was|"
    r"exception in thread|unhandled (exception|rejection)$)"
)

# Lines that are pure scaffolding: progress chatter and advisory notes.
_NOISE = re.compile(
    r"(?i)^(warning|note|hint|info|deprecat|collecting|running|building|"
    r"compiling|downloading|installing|\W*$)"
)

# Tallies — "N failed, N passed", "Tests: N failed, N total".  These mention
# the failure but are really a census of everything in the tree, so pinning to
# one stops carve from deleting unrelated passing tests.  Poor signatures.
_TALLY = re.compile(
    r"(?i)(^|\s)N\s+(passed|failed|error|errors|skipped|warnings?|tests?|"
    r"total|assertions?|examples?|specs?|problems?)\b"
)

# Lines naming a location are unusually specific, and so unusually good.
_LOCATED = re.compile(r"(::|\.\w{1,5}:N|/\w|\bat\s)")

# Source echoed back by the test runner.  Pinning to one of these freezes the
# very line carve is trying to delete, so they make terrible signatures.
_ECHO = re.compile(r"^(>\s|\|\s|N\s*\||\.\.\.)")

# Signatures shorter than this match by accident.
_MIN_SIGNATURE = 12


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def normalise(text: str, root: Optional[str] = None) -> str:
    """Erase everything that legitimately changes between two runs.

    Line numbers move as carve deletes lines, temp directories differ per
    worker, and durations differ per run.  None of that is the bug.
    """
    text = strip_ansi(text)
    if root:
        text = text.replace(root.rstrip("/"), "<root>")
    text = _TMP.sub("<tmp>", text)
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<addr>", text)
    text = _TIME.sub("<dur>", text)
    text = _NUM.sub("N", text)
    return text


def normalise_line(line: str, root: Optional[str] = None) -> str:
    return _SPACE.sub(" ", normalise(line, root)).strip()


def _alpha_ratio(line: str) -> float:
    if not line:
        return 0.0
    return sum(char.isalnum() for char in line) / float(len(line))


def score_line(line: str) -> int:
    """How well this line identifies one specific failure."""
    score = 0
    if _alpha_ratio(line) < 0.45:
        # A banner of `=====` or `-----` says a failure happened, never which.
        score -= 7
    if _HINT.search(line):
        score += 6
    if _NAMED.match(line):
        score += 5
    if _GENERIC.match(line):
        score -= 9
    if _LOCATED.search(line):
        score += 3
    if _NOISE.match(line):
        score -= 5
    if _TALLY.search(line):
        # A census of the whole tree, not a description of the bug.
        score -= 8
    if _ECHO.match(line):
        score -= 6
    score += min(len(line), 120) // 40
    return score


def extract_signatures(
    result: RunResult,
    root: Optional[str] = None,
    limit: int = 2,
) -> List[str]:
    """Pick the few normalised lines that identify *this* failure.

    Later lines win ties: a stack trace ends with the thing that actually blew
    up, and the frames above it are scenery.
    """
    lines = [normalise_line(line, root) for line in result.output.splitlines()]
    lines = [line for line in lines if len(line) >= _MIN_SIGNATURE]
    if not lines:
        return []

    ranked = sorted(
        ((score_line(line), index, line) for index, line in enumerate(lines)),
        key=lambda item: (-item[0], -item[1]),
    )

    best = ranked[0][0]
    chosen: List[str] = []
    for score, _, line in ranked:
        # A clearly best line stands alone.  Adding a weaker second signature
        # only freezes more of the tree than the bug actually needs.
        if chosen and score < best - 2:
            break
        # Two signatures where one contains the other is one signature.
        if any(line in kept or kept in line for kept in chosen):
            continue
        chosen.append(line)
        if len(chosen) >= limit:
            break
    return chosen


@dataclass
class Oracle:
    """The definition of "still the same bug"."""

    exit_code: Optional[int] = None
    signatures: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    allow_timeout: bool = False
    root: Optional[str] = None

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.patterns]

    def holds(self, result: RunResult) -> bool:
        if result.timed_out:
            # A hang is only "the same bug" when the original hung too, and
            # then nothing else about the output can be trusted.
            return self.allow_timeout
        if self.allow_timeout:
            return False
        if self.exit_code is not None and result.exit_code != self.exit_code:
            return False
        raw = result.output
        for pattern in self._compiled:
            if not pattern.search(raw):
                return False
        if self.signatures:
            hay = _SPACE.sub(" ", normalise(raw, self.root))
            for signature in self.signatures:
                if signature not in hay:
                    return False
        return True

    def explain(self) -> List[str]:
        """Human-readable clauses, for the banner and the report."""
        clauses: List[str] = []
        if self.allow_timeout:
            return ["the command still times out"]
        if self.exit_code is not None:
            clauses.append("exit status is {0}".format(self.exit_code))
        for pattern in self.patterns:
            clauses.append("output matches /{0}/".format(pattern))
        for signature in self.signatures:
            clauses.append("output contains “{0}”".format(signature))
        if not clauses:
            clauses.append("the command fails at all")
        return clauses

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "signatures": list(self.signatures),
            "patterns": list(self.patterns),
            "allow_timeout": self.allow_timeout,
        }


def build(
    baseline: RunResult,
    root: Optional[str] = None,
    expect_exit: Optional[int] = None,
    ignore_exit: bool = False,
    patterns: Sequence[str] = (),
    signatures: Sequence[str] = (),
    max_signatures: int = 2,
) -> Oracle:
    """Derive an oracle from the failure the user already has."""
    exit_code: Optional[int]
    if ignore_exit:
        exit_code = None
    elif expect_exit is not None:
        exit_code = expect_exit
    else:
        exit_code = baseline.exit_code

    if signatures:
        chosen = [normalise_line(s, root) for s in signatures]
    elif patterns:
        # An explicit regex is the whole contract; do not add guesses to it.
        chosen = []
    else:
        chosen = extract_signatures(baseline, root, limit=max_signatures)

    return Oracle(
        exit_code=exit_code,
        signatures=chosen,
        patterns=list(patterns),
        allow_timeout=baseline.timed_out,
        root=root,
    )
