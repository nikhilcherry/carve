"""Command line interface.

Called by: carve/__main__.py and the `carve` console script.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from typing import List, Optional, Sequence, Tuple

from . import __version__
from .reduce import LEVELS, carve, characterise
from .report import (print_summary, render_markdown, write_json,
                     write_markdown, write_tree)
from .runner import describe
from .term import Style, colour_enabled, human_time
from .workspace import BudgetExhausted

_EPILOG = """\
examples:
  carve -- pytest tests/test_upload.py       shrink the tree around a failing test
  carve ~/code/api -- make check             any command, any language
  carve check -- npm test                    show the failure carve would lock onto
  carve --level files -- ./run.sh            which files matter, skip line surgery
  carve --expect 'IndexError' -- ./run.sh    pin the failure to your own pattern

carve never writes to the directory you point it at.  Every candidate is
tested in a scratch copy, and the result lands in ./carve-out.
"""

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$")


def parse_duration(text: str) -> float:
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            "expected a duration like 90, 30s, 10m or 2h")
    value = float(match.group(1))
    return value * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def split_command(argv: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Everything after the first bare `--` is the user's command."""
    argv = list(argv)
    if "--" not in argv:
        return argv, []
    cut = argv.index("--")
    return argv[:cut], argv[cut + 1:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carve",
        description="Shrink a failing repository to the least that still "
                    "fails the same way.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="directory to reduce (default: .)")

    out = parser.add_argument_group("output")
    out.add_argument("-o", "--out", default="carve-out", metavar="DIR",
                     help="where to write the reduction (default: ./carve-out)")
    out.add_argument("--force", action="store_true",
                     help="overwrite the output directory if it exists")
    out.add_argument("--json", metavar="PATH",
                     help="also write machine-readable results here")
    out.add_argument("--stdout", action="store_true",
                     help="print REPRO.md to stdout instead of writing a tree")
    out.add_argument("--no-color", action="store_true",
                     help="disable ANSI colour")
    out.add_argument("-q", "--quiet", action="store_true",
                     help="no progress, just the summary")

    what = parser.add_argument_group("what carve may touch")
    what.add_argument("--keep", action="append", default=[], metavar="GLOB",
                      help="never remove or edit matching files (repeatable)")
    what.add_argument("--only", action="append", default=[], metavar="GLOB",
                      help="reduce only matching files (repeatable)")
    what.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                      help="pretend matching files do not exist (repeatable)")
    what.add_argument("--link", action="append", default=[], metavar="DIR",
                      help="symlink this directory into each scratch copy "
                           "instead of copying it, e.g. node_modules")
    what.add_argument("--with-git", action="store_true",
                      help="copy .git into the scratch trees as well")

    how = parser.add_argument_group("how hard to try")
    how.add_argument("--level", choices=LEVELS, default="lines",
                     help="how deep to cut (default: lines)")
    how.add_argument("--passes", type=int, default=3, metavar="N",
                     help="maximum reduction rounds (default: 3)")
    how.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                     help="candidates to test in parallel (default: 4)")
    how.add_argument("--timeout", type=parse_duration, metavar="DURATION",
                     help="per-run timeout (default: 5x the baseline run)")
    how.add_argument("--max-runs", type=int, metavar="N",
                     help="stop after this many test runs")
    how.add_argument("--time-budget", type=parse_duration, metavar="DURATION",
                     help="stop after this long, e.g. 10m")
    how.add_argument("--shrink-command", action="store_true",
                     help="also drop command arguments that make no "
                          "difference to the failure")
    how.add_argument("--work-dir", metavar="DIR",
                     help="where to put scratch trees (default: system temp)")

    same = parser.add_argument_group("what counts as the same failure")
    same.add_argument("--expect", action="append", default=[], metavar="REGEX",
                      help="output must match this (repeatable)")
    same.add_argument("--signature", action="append", default=[],
                      metavar="TEXT",
                      help="output must contain this line (repeatable)")
    same.add_argument("--expect-exit", type=int, metavar="N",
                      help="require this exit status")
    same.add_argument("--ignore-exit", action="store_true",
                      help="do not require any particular exit status")
    same.add_argument("--verify", type=int, default=2, metavar="N",
                      help="confirmation runs before and after (default: 2)")
    same.add_argument("--allow-flaky", action="store_true",
                      help="reduce even when the failure is intermittent")

    parser.add_argument("--version", action="version",
                        version="carve {0}".format(__version__))
    return parser


class Progress:
    """One self-updating status line, or nothing at all when piped."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, style: Style, enabled: bool, stream=None) -> None:
        self.style = style
        self.stream = stream or sys.stderr
        try:
            self.live = enabled and self.stream.isatty()
        except Exception:
            self.live = False
        self.enabled = enabled
        self.runs = 0
        self.phase = "starting"
        self.detail = ""
        self.frame = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def event(self, kind: str, **info) -> None:
        if not self.enabled:
            return
        if kind == "baseline":
            self.phase = "characterising the failure"
        elif kind == "round":
            self.phase = "round {0}/{1}".format(info["number"], info["of"])
        elif kind == "phase":
            if info["name"] == "files":
                self.detail = "{0} files".format(info["total"])
            else:
                self.detail = info.get("path", "")
        elif kind == "shrink":
            if info["unit"] == "files":
                self.detail = "{0} files left".format(info["size"])
            else:
                self.detail = "{0} · {1} lines".format(
                    info.get("path", ""), info["size"])
        elif kind == "run":
            self.runs = info["n"]
        self._draw()

    def _draw(self) -> None:
        if not self.live:
            return
        now = time.time()
        with self._lock:
            if now - self._last < 0.08:
                return
            self._last = now
            self.frame = (self.frame + 1) % len(self.FRAMES)
            text = "  {0} {1:<26} {2:>6} runs  {3}".format(
                self.FRAMES[self.frame], self.phase, self.runs,
                self.style.grey(self.detail[:44]))
        self.stream.write("\r\033[2K" + text)
        self.stream.flush()

    def done(self) -> None:
        if self.live:
            self.stream.write("\r\033[2K")
            self.stream.flush()


def run_check(args: argparse.Namespace, command: List[str],
              style: Style) -> int:
    from . import oracle as oracle_mod

    root = os.path.abspath(args.path)
    sys.stderr.write("  running once in a scratch copy of {0}\n".format(root))
    result = characterise(root, command, args.timeout or 300.0,
                          args.with_git, args.link, args.work_dir)
    the_oracle = oracle_mod.build(
        result, expect_exit=args.expect_exit, ignore_exit=args.ignore_exit,
        patterns=args.expect, signatures=args.signature)

    print()
    print("  " + style.bold("exit status ") + str(result.exit_code)
          + style.grey("   in " + human_time(result.duration)))
    if result.timed_out:
        print("  " + style.red("timed out"))
    print()
    print("  " + style.bold("carve would keep cutting while"))
    for clause in the_oracle.explain():
        print("    - " + clause)
    print()
    tail = result.output.strip().splitlines()[-20:]
    if tail:
        print("  " + style.bold("last of the output"))
        for line in tail:
            print("    " + style.grey(line[:150]))
        print()
    return 0 if (result.exit_code != 0 or result.timed_out) else 1


def entrypoint(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    options, command = split_command(argv)
    parser = build_parser()

    if not argv:
        parser.print_help()
        return 2

    # `carve check .` — a leading verb, not a path.
    mode = "reduce"
    if options and options[0] in ("check", "reduce"):
        mode = options.pop(0)
    args = parser.parse_args(options)
    style = Style(colour_enabled(args.no_color))

    if not command:
        parser.error("no command given — put it after `--`, for example:\n"
                     "  carve -- pytest tests/test_thing.py")
    if not os.path.isdir(args.path):
        sys.stderr.write("carve: {0} is not a directory\n".format(args.path))
        return 2
    if mode == "check":
        return run_check(args, command, style)

    jobs = args.jobs or min(4, (os.cpu_count() or 2))
    progress = Progress(style, not args.quiet)
    if not args.quiet:
        sys.stderr.write("  {0}\n  {1}\n".format(
            style.bold("carve"), style.grey("$ " + describe(command))))

    try:
        outcome = carve(
            args.path, command,
            only=args.only, keep=args.keep, exclude=args.exclude,
            level=args.level, passes=args.passes, jobs=jobs,
            timeout=args.timeout, with_git=args.with_git, links=args.link,
            work_dir=args.work_dir, max_runs=args.max_runs,
            time_budget=args.time_budget, expect_exit=args.expect_exit,
            ignore_exit=args.ignore_exit, patterns=args.expect,
            signatures=args.signature, verify=args.verify,
            allow_flaky=args.allow_flaky, shrink_command=args.shrink_command,
            on_event=progress.event,
        )
    except ValueError as exc:
        progress.done()
        sys.stderr.write("\ncarve: {0}\n".format(exc))
        return 1
    except KeyboardInterrupt:
        progress.done()
        sys.stderr.write("\ncarve: interrupted\n")
        return 130
    except BudgetExhausted as exc:                     # pragma: no cover
        progress.done()
        sys.stderr.write("\ncarve: {0}\n".format(exc))
        return 1
    progress.done()

    if args.stdout:
        sys.stdout.write(render_markdown(outcome))
        if args.json:
            write_json(outcome, args.json)
        return 0 if outcome.verified else 1

    try:
        out_dir = write_tree(outcome, args.out, args.force)
    except ValueError as exc:
        sys.stderr.write("carve: {0}\n".format(exc))
        return 1
    write_markdown(outcome, os.path.join(out_dir, "REPRO.md"))
    write_json(outcome, args.json or os.path.join(out_dir, "carve.json"))
    print_summary(outcome, style, out_dir)
    return 0 if outcome.verified else 1


def main() -> None:                                    # pragma: no cover
    sys.exit(entrypoint())
