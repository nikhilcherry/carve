"""Delta debugging, kept deliberately free of any idea of files or lines.

`ddmin` shrinks a list of opaque units to a 1-minimal subset: one where
removing any single remaining unit stops the failure.  Zeller and Hildebrandt's
algorithm, with the subset and complement probes at each granularity handed to
the caller as a batch so they can be run in parallel.

Called by: carve/reduce.py.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, TypeVar

T = TypeVar("T")

# evaluate(candidates) -> index of the first candidate that still reproduces,
# or None if none of them do.
Evaluate = Callable[[List[List[T]]], Optional[int]]


def split(units: Sequence[T], parts: int) -> List[List[T]]:
    """Split into `parts` near-equal chunks, dropping empties."""
    total = len(units)
    parts = max(1, min(parts, total))
    chunks: List[List[T]] = []
    start = 0
    for index in range(parts):
        end = ((index + 1) * total) // parts
        if end > start:
            chunks.append(list(units[start:end]))
        start = end
    return chunks


def ddmin(
    units: Sequence[T],
    evaluate: Evaluate,
    allow_empty: bool = True,
    on_progress: Optional[Callable[[int], None]] = None,
) -> List[T]:
    """Return a 1-minimal sublist of `units` that `evaluate` still accepts.

    `units` must already be known to reproduce; ddmin never re-tests it whole.
    """
    current: List[T] = list(units)
    if not current:
        return current

    if allow_empty and evaluate([[]]) is not None:
        _report(on_progress, 0)
        return []

    granularity = 2
    while len(current) > 1:
        chunks = split(current, granularity)
        if len(chunks) < 2:
            break

        # Does any single chunk reproduce on its own?  When it does we drop
        # everything else in one step, which is the whole point of ddmin.
        hit = evaluate(chunks)
        if hit is not None:
            current = chunks[hit]
            _report(on_progress, len(current))
            granularity = 2
            continue

        complements = [
            [unit for other, chunk in enumerate(chunks) if other != index
             for unit in chunk]
            for index in range(len(chunks))
        ]
        complements = [c for c in complements if c]
        hit = evaluate(complements)
        if hit is not None:
            current = complements[hit]
            _report(on_progress, len(current))
            granularity = max(granularity - 1, 2)
            continue

        if granularity >= len(current):
            break
        granularity = min(granularity * 2, len(current))

    return current


def _report(callback: Optional[Callable[[int], None]], size: int) -> None:
    if callback is not None:
        callback(size)
