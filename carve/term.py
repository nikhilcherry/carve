"""Terminal colour and small formatting helpers.

Called by: carve/cli.py, carve/report.py, carve/reduce.py.
"""

from __future__ import annotations

import os
import sys


def colour_enabled(no_colour: bool = False, stream=None) -> bool:
    """True when ANSI escapes are worth emitting."""
    if no_colour or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except Exception:
        return False


class Style:
    """A tiny colour palette that degrades to plain text."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return "\033[{0}m{1}\033[0m".format(code, text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def magenta(self, text: str) -> str:
        return self._wrap("35", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def grey(self, text: str) -> str:
        return self._wrap("90", text)


def plural(count: int, singular: str, plural_form: str = None) -> str:
    """`3 files`, `1 file`."""
    word = singular if count == 1 else (plural_form or singular + "s")
    return "{0:,} {1}".format(count, word)


def human_bytes(size: int) -> str:
    step = float(size)
    for unit in ("B", "KiB", "MiB"):
        if step < 1024:
            if unit == "B":
                return "{0:.0f} B".format(step)
            return "{0:.1f} {1}".format(step, unit)
        step /= 1024
    return "{0:.1f} GiB".format(step)


def human_time(seconds: float) -> str:
    if seconds < 1:
        return "{0:.0f} ms".format(seconds * 1000)
    if seconds < 60:
        return "{0:.1f} s".format(seconds)
    minutes, rest = divmod(seconds, 60)
    return "{0:.0f} m {1:.0f} s".format(minutes, rest)


def percent_drop(before: int, after: int) -> str:
    if before <= 0:
        return "0%"
    return "{0:.0f}%".format(100.0 * (before - after) / before)


def truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").replace("\t", " ").strip()
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"
