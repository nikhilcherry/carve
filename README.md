# carve

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-4ab5e8?style=flat-square" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-none-3fb950?style=flat-square" alt="No dependencies">
  <img src="https://img.shields.io/badge/works%20with-any%20language-a371f7?style=flat-square" alt="Any language">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="MIT license">
</p>

**A bug nobody can reproduce in ten lines is a bug nobody will fix.**

Point `carve` at a directory and a command that fails. It hands back the
smallest set of files — and the smallest set of lines inside them — that still
fails *the exact same way*.

```bash
carve -- pytest tests/test_upload.py
```

![A dark terminal window titled "carve -- pytest tests -q". It reads "carved down to", then three rows: files 25 to 2, 92% gone; lines 314 to 14, 96% gone; bytes 5.1 KiB to 409 B, 92% gone. Below, the two surviving files are listed — src/parser.py at 9 lines and tests/test_parser.py at 5 lines — then "129 runs · 92 cached · 17.2 s" and the output path](docs/images/carved.svg)

Everything in `./carve-out` still fails. Everything deleted is now *proven* not
to matter. Your own directory is never written to.

## On a real codebase

A 28-file, 7,929-line Python project with one bad helper buried in it, and a
test suite that catches it:

```console
$ carve ~/code/strata -- python -m unittest discover -s tests

    files             28  →  2     93% gone
    lines          7,929  →  8     99% gone
    bytes      950.6 KiB  →  287 B  99% gone

    382 runs · 262 cached · 13.6 s
```

What survived, in full:

```python
# strata/complexity.py
def _worst_line(scores):
    ordered = sorted(scores)
    return ordered[len(ordered) - 1]

# tests/test_strata.py
import unittest
class BuggyHelperTests(unittest.TestCase):
    def test_worst_line_of_nothing(self):
        from strata.complexity import _worst_line
        self.assertEqual(_worst_line([]), 0)
```

Fourteen seconds, and the docstring, the imports, the package `__init__`, the
other thirteen modules and the other thirty-nine tests are all gone — each one
individually proven irrelevant by deleting it and watching the bug survive.

## Install

```bash
pip install carve-cli
```

No dependencies, nothing to configure, Python 3.9+.

## Why this is not `git bisect`

Every tool in this space answers a different question:

| Tool | Answers |
| --- | --- |
| `git bisect` | **which commit** introduced it |
| coverage | **which lines ran** |
| a profiler | **which lines were slow** |
| C-Reduce | how small **one C file** can get, if you have clang |
| **carve** | **which files and which lines are load-bearing for this failure** |

Coverage tells you what executed, and most of what executes is irrelevant.
carve tells you what *matters*, by the only test that settles it: deleting
something and seeing whether the bug survives.

It works on Python, Go, Rust, TypeScript, YAML, Terraform, Dockerfiles and
anything else, because it never parses your code. It deletes text and runs your
command.

## What it is for

**Filing a bug report someone will act on.** `carve-out/REPRO.md` is a single
markdown file with the command, the failure, and every remaining file inlined
in a fenced block. Paste it into an issue.

**Pasting into an LLM.** A 400-file repository does not fit in a context
window. Fourteen load-bearing lines do, and they contain the whole bug.

**Understanding your own code.** When carve reports that a 2,000-line service
class reduces to four lines and a config key, you have learned something real
about that service class.

**Isolating a flake.** `carve --allow-flaky --verify 5` keeps only the runs
that reproduce, and throws away the tree that never mattered.

## How it decides what "the same failure" means

This is what makes reduction trustworthy, and it is what most reducers get
wrong. A reducer that only asks *did it fail?* will happily hand you a tree
that fails because the reducer broke the syntax.

carve runs your command once, fingerprints the failure, and rejects any
candidate that does not match it:

![A dark terminal window titled "carve check -- pytest tests -q", showing "exit status 1 in 307 ms" and then, under the heading "carve would keep cutting while", two conditions: exit status is 1, and output contains "FAILED tests/test_parser.py::test_summarise_empty - IndexError: list index ou..."](docs/images/check.svg)

Signatures are normalised before matching, so shifting line numbers, temporary
paths, memory addresses and durations never count as a different bug. carve
also deliberately refuses to latch onto:

- **tallies** — `1 failed, 1 passed`, because pinning to one stops carve
  deleting unrelated *passing* tests
- **echoed source** — the `> assert x == y` line, because pinning to it freezes
  the very line you want deleted
- **generic preambles** — `Traceback (most recent call last):` says that a
  failure happened, never which one

If the guess is wrong, say so yourself:

```bash
carve --expect 'IndexError: list index' -- ./run.sh    # regex over the output
carve --signature 'Segmentation fault'   -- ./run.sh   # literal line
carve --expect-exit 139                  -- ./run.sh   # exit status
```

## How it works

1. **Copy.** The tree is cloned into scratch directories, one per parallel job.
   `.git` and stale caches are left behind.
2. **Fingerprint.** One run establishes the failure; a second confirms it is
   not a flake. carve refuses to reduce an intermittent failure unless you pass
   `--allow-flaky`, because reducing against a coin flip produces nonsense.
3. **Cut files.** Delta debugging (`ddmin`) over the file set, so whole
   directories can disappear in a single probe.
4. **Cut blocks.** Inside each surviving file, indentation-delimited and
   brace-delimited blocks are deleted wholesale — several disjoint ones per
   round, in parallel.
5. **Cut lines.** `ddmin` again, down to 1-minimal: removing any single
   remaining line stops the failure.
6. **Cut tokens**, at `--level chars`: the same treatment applied inside each
   surviving line.
7. **Repeat** until a full round changes nothing, then verify the result.

Every probe is content-addressed and cached, so no candidate is ever run twice.

## Options worth knowing

```
carve [DIR] -- COMMAND...
carve check [DIR] -- COMMAND...    just show the failure carve would lock onto

  --level files|blocks|lines|chars   how deep to cut (default: lines)
  -j, --jobs N                 candidates tested in parallel (default: 4)
  --time-budget 10m            stop after this long, keep the best result
  --max-runs 500               stop after this many probes
  --shrink-command             drop command arguments that change nothing
  --keep 'conftest.py'         never delete or edit these
  --only 'src/**'              only reduce these
  --link node_modules          symlink instead of copying heavy directories
  --stdout                     print REPRO.md instead of writing a tree
```

`--level files` is the fast one: it answers "which files matter?" in a fraction
of the probes, which is often all you needed.

`--level chars` is the thorough one. It keeps cutting *inside* each surviving
line, which is the difference between a repro you can read and one you can
publish:

```python
# --level lines                                    (14 probes)
def render(name, width, height, timeout=30, retries=5, verbose=False):
    box = {"name": name, "w": width, "h": height}
    return box["missing_key"]
render("panel", 80, 24, timeout=15, retries=2, verbose=True)

# --level chars                                    (652 probes)
def render(name,height,verbose):
    box={"":name,"":height}
    return box["missing_key"]
render("",2,True)
```

Every argument that survived is one whose removal stops the bug. Budget it with
`--time-budget`; carve keeps the best result it reached.

`--shrink-command` turns the reducer on your own invocation. The same oracle
applies, so an argument only goes if the failure is bit-for-bit unmoved:

```console
$ carve --shrink-command -- pytest -vv --tb=long -p no:cacheprovider tests -q
    command  $ pytest -vv -q
```

Interrupting with Ctrl-C is safe. carve always holds a state that reproduces,
and reports the best one it reached.

## Safety

- The directory you point carve at is **opened read-only**. Every candidate is
  tested in a scratch copy under your temp directory, removed on exit.
- carve runs the command you gave it, many times over. That command should be
  one you are happy to run in a loop — a test suite, a build, a script. carve
  is a reducer, not a sandbox.
- `carve-out` is never overwritten without `--force`, and carve refuses to
  write into the source tree.

## License

MIT
