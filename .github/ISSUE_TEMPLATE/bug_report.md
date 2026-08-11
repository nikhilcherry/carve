---
name: Bug report
about: Something carve got wrong
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- What you ran, and what carve did that it should not have. -->

```console
$ carve ... -- ...
```

## The reduction

<!--
If carve produced a carve-out/, paste REPRO.md. That is the whole point of the
tool, and it is the fastest bug report anyone can file.

If carve did NOT produce a useful reduction, that is itself the bug — and
`carve check -- <your command>` usually explains why, because it prints the
failure carve locked onto:

    carve would keep cutting while
      - exit status is 1
      - output contains "..."

A signature that is too broad makes carve delete too much. One that is too
specific — a tally like "1 failed, 1 passed", or a line of your own source —
makes it delete too little.
-->

## Environment

- carve version: <!-- carve --version -->
- Python version:
- OS:
- The command being reduced:
