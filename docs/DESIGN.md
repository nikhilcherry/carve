# How carve works

carve answers one question — *which parts of this tree are load-bearing for
this failure?* — the only way it can be answered honestly: delete something,
run the command, and see whether the bug survives.

Everything below is in service of making that loop fast enough to be practical
and strict enough to be trusted.

## The loop

```
                 ┌──────────────────────────────────────┐
   your tree ───▶│ scratch copy (one per parallel job)  │
                 └───────────────┬──────────────────────┘
                                 │  apply candidate state
                                 ▼
                        run your command
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ oracle: same failure? │
                     └─────┬───────────┬─────┘
                       yes │           │ no
                           ▼           ▼
                    keep the cut   put it back
```

## The oracle is the whole game

A reducer that only asks *did the command fail?* is worse than useless: it will
delete a closing brace, watch the command fail with a syntax error, call that
success, and hand you a tree that reproduces nothing.

`carve/oracle.py` fingerprints the original failure and requires every
candidate to match it. Two things make that workable.

**Normalisation.** Deleting lines moves every line number below the deletion,
and each parallel job runs in a different temp directory. So before matching,
carve rewrites digits to `N`, hex addresses to `<addr>`, temp paths to `<tmp>`,
UUIDs, durations and ANSI escapes. `file.py:214: boom at 0xdeadbeef after 1.5s`
becomes `file.py:N: boom at <addr> after <dur>`, which is stable across every
run of the same bug.

**Signature scoring.** Given a page of output, which line *is* the failure?
`score_line()` ranks the candidates, and three classes are penalised hard
because each one silently over-constrains the reduction:

| Rejected | Example | Why it hurts |
| --- | --- | --- |
| tallies | `1 failed, 1 passed` | deleting an unrelated *passing* test would break the match, so carve keeps it forever |
| echoed source | `> assert x == y` | freezes the exact line you are trying to delete |
| generic preambles | `Traceback (most recent call last):` | matches every failure of that language, constrains nothing, and displaces a real signature |

A named condition with a message — `IndexError: list index out of range`,
`java.lang.NullPointerException: ...` — scores highest. When the guess is
wrong, `--expect` and `--signature` replace it outright.

One subtlety worth remembering: the hint pattern must **not** use `\b` word
boundaries. `\berror\b` never matches `IndexError`, and getting that wrong made
carve pick the useless `Traceback` line every time.

## Five granularities

Reduction is a fixed-point loop over increasingly fine cuts. Each level only
runs on what survived the level above, which is what keeps the probe count
manageable.

1. **Files.** `ddmin` over the file set. The universe is sorted by path, so
   chunks are directory-coherent and a whole `docs/` tree can vanish in one
   probe.
2. **Blocks.** Within a file, ranges found by indentation (a line plus
   everything indented under it) and by braces (`{` at end of line, to its
   match). Deleted greedily, biggest first, several *disjoint* blocks tested in
   parallel per round. Braces inside strings are miscounted on purpose: a wrong
   guess costs one rejected probe, and the alternative is a parser per language.
3. **Lines.** `ddmin` again, to 1-minimality — removing any single surviving
   line stops the failure.
4. **Unwrap.** Deletion alone cannot get past `if debug:` when the statement
   underneath it is the one that matters — the header is load-bearing only
   because its body is. Removing the header and dedenting its body breaks that
   deadlock, and needs no grammar: it is true of every language that indents.
5. **Tokens** (`--level chars`). `ddmin` over identifiers, numbers, whitespace
   runs and single characters within each line. This is what turns
   `render(a, b, c, timeout=30)` into `render(c)`.

Before any of that, one cheap probe empties the file entirely and one strips
every blank and comment line at once. Both frequently collapse a file for the
price of a single run.

## Delta debugging

`carve/ddmin.py` is Zeller and Hildebrandt's algorithm over an opaque list of
units. At each granularity it asks two questions — *does one chunk reproduce on
its own?* and *does removing one chunk still reproduce?* — and hands the whole
batch of candidates to the caller at once rather than testing them one at a
time. That is what lets the workspace pool run them in parallel while keeping
the result deterministic: `first_accepted` always returns the lowest-indexed
candidate that reproduces, no matter which worker finished first.

## Why it stays fast

- **Parallel probes.** One scratch tree per job; candidates dispatched in index
  order and resolved by index.
- **A content-addressed cache.** Every candidate state is hashed by path and
  content, so a state reached twice is free. Cache hits routinely outnumber
  real runs.
- **Delta application.** Workspaces are reused, and applying a candidate writes
  only the files that differ from what is already on disk.
- **Relevance ordering.** Files the failure output names are reduced first, so
  a run that hits `--time-budget` still returns the interesting file minimised.

## Why it stays correct

- The source directory is opened read-only. Always.
- Scratch trees are reused but never polluted: anything a run creates is
  deleted before the next candidate, and so is any managed file the command
  recreated after carve deleted it. A build cache from run N must not decide
  run N+1.
- The baseline is confirmed twice before reduction starts. carve refuses to
  reduce a flaky failure unless told to, because reducing against a coin flip
  produces confident nonsense.
- The final tree is re-verified before it is written, and the report says so
  loudly when that fails.
- `Ctrl-C`, the run budget and the time budget all land on a state that
  reproduces, because carve never holds anything else.
