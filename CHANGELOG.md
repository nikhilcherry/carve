# Changelog

## 0.2.0

Everything here came out of pointing carve at real code rather than fixtures.

### Cuts deeper

- **`--level chars`** takes surviving lines apart token by token, so
  `render(a, b, c, timeout=30, retries=5)` becomes `render(c)`. Opt-in: it
  costs roughly fifty times the probes of `--level lines`.
- **An unwrap pass** lifts a body out of the wrapper holding it. Deletion alone
  cannot remove `if debug:` when the statement underneath is the one that
  matters — dropping the header leaves an indentation error, so carve used to
  keep a wrapper that contributed nothing.
- **`--shrink-command`** reduces the invocation too. A reproduction is the tree
  *and* the command, and `pytest -vv --tb=long -p no:cacheprovider tests/` is
  not a command anyone wants in a bug report.

### Cuts more accurately

- Signature scoring no longer picks `Traceback (most recent call last):` over
  `IndexError: list index out of range`. The hint pattern used `\b` word
  boundaries, and `\berror\b` never matches `IndexError`.
- Tallies (`1 failed, 1 passed`) are rejected as signatures: pinning to one
  stops carve deleting unrelated *passing* tests.
- Echoed source (`> assert x == y`) is rejected too: pinning to it freezes the
  very line being reduced.
- Signature choice is now pinned by tests against real output from go test,
  cargo test, jest, gcc, a Java stack trace and a bare segfault.

### Cuts more safely

- **Scratch trees are no longer polluted between candidates.** Anything a run
  creates is removed before the next one, as is any managed file the command
  recreated after carve deleted it. A cache written by run N could previously
  make run N+1 pass for reasons unrelated to the deletion under test.
- The report no longer overwrites a reduced file that happens to be called
  `REPRO.md` or `carve.json`.

### Smaller things

- Files named in the failure output are reduced first, so a run that hits
  `--time-budget` still returns the interesting file minimised.
- `carve <dir>` no longer mistakes the directory for a subcommand.
- The summary no longer claims "100% gone" while listing what it kept.

## 0.1.0

First release. Files, blocks and lines; the oracle; the workspace pool with a
content-addressed cache; `carve check`; `REPRO.md` and `carve.json`.
