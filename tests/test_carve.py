"""Tests for carve.  Stdlib only: `python -m unittest discover -s tests`."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carve import cli, oracle, report, tree                        # noqa: E402
from carve.ddmin import ddmin, split                               # noqa: E402
from carve.reduce import (brace_blocks, candidate_blocks, carve,    # noqa: E402
                          indent_blocks, measure, named_in)
from carve.runner import RunResult, describe, run, wants_shell     # noqa: E402
from carve.workspace import Pool, Workspace, copy_tree             # noqa: E402


def result(exit_code=1, stdout="", stderr="", duration=0.01, timed_out=False):
    return RunResult(exit_code, stdout, stderr, duration, timed_out)


class TempTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="carve-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, relpath, text):
        full = os.path.join(self.root, relpath)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(text).lstrip("\n"))
        return full


# -- ddmin -----------------------------------------------------------------


class DdminTests(unittest.TestCase):
    def evaluator(self, predicate):
        def evaluate(candidates):
            for index, candidate in enumerate(candidates):
                if predicate(candidate):
                    return index
            return None
        return evaluate

    def test_finds_the_single_culprit(self):
        keep = self.evaluator(lambda c: 7 in c)
        self.assertEqual(ddmin(list(range(40)), keep), [7])

    def test_finds_a_pair_that_must_travel_together(self):
        keep = self.evaluator(lambda c: 3 in c and 21 in c)
        self.assertEqual(sorted(ddmin(list(range(30)), keep)), [3, 21])

    def test_keeps_everything_when_everything_matters(self):
        units = list(range(6))
        keep = self.evaluator(lambda c: len(c) == 6)
        self.assertEqual(ddmin(units, keep), units)

    def test_empty_candidate_is_offered(self):
        self.assertEqual(ddmin([1, 2, 3], self.evaluator(lambda c: True)), [])

    def test_split_drops_empty_chunks(self):
        self.assertEqual(split([1, 2, 3], 5), [[1], [2], [3]])
        self.assertEqual(split([1, 2, 3, 4], 2), [[1, 2], [3, 4]])
        self.assertEqual(split([], 3), [])


# -- oracle ----------------------------------------------------------------


class NormaliseTests(unittest.TestCase):
    def test_erases_line_numbers_and_addresses(self):
        text = "file.py:214: boom at 0xdeadbeef after 1.5s"
        self.assertEqual(oracle.normalise_line(text),
                         "file.py:N: boom at <addr> after <dur>")

    def test_strips_ansi(self):
        self.assertEqual(oracle.normalise_line("\x1b[31mred\x1b[0m error here"),
                         "red error here")

    def test_survives_shifting_line_numbers(self):
        first = oracle.normalise('Traceback\n  File "a.py", line 40\nBoom')
        second = oracle.normalise('Traceback\n  File "a.py", line 3\nBoom')
        self.assertEqual(first, second)


class SignatureTests(unittest.TestCase):
    def test_prefers_the_specific_failure_over_the_tally(self):
        output = (
            "=================== FAILURES ===================\n"
            "FAILED tests/test_parser.py::test_empty - IndexError: list index\n"
            "1 failed, 1 passed in 0.03s\n"
        )
        picked = oracle.extract_signatures(result(stdout=output))
        self.assertTrue(picked)
        self.assertIn("IndexError", picked[0])
        self.assertFalse(any("passed" in signature for signature in picked))

    def test_ignores_echoed_source(self):
        output = ('> assert thing.summarise("") == ""\n'
                  "E   IndexError: list index out of range\n")
        picked = oracle.extract_signatures(result(stdout=output))
        self.assertIn("IndexError", picked[0])

    def test_no_output_means_no_signature(self):
        self.assertEqual(oracle.extract_signatures(result()), [])


class RealWorldSignatureTests(unittest.TestCase):
    """Signature choice is the most fragile heuristic in carve.

    Each of these is output from a real toolchain.  The requirement is always
    the same: pick the line that names *this* failure, never the line that
    counts how many failures there were.
    """

    def check(self, output, must_contain, must_avoid=()):
        picked = oracle.extract_signatures(result(stdout=output))
        self.assertTrue(picked, "no signature chosen from:\n" + output)
        joined = " || ".join(picked)
        self.assertIn(must_contain, joined)
        for banned in must_avoid:
            self.assertNotIn(banned, joined)

    def test_go_test(self):
        self.check(
            "=== RUN   TestSummarise\n"
            "--- FAIL: TestSummarise (0.00s)\n"
            "panic: runtime error: index out of range [0] with length 0\n"
            "FAIL\texample.com/pkg\t0.012s\n",
            "index out of range",
        )

    def test_cargo_test(self):
        self.check(
            "running 2 tests\n"
            "test tests::ok ... ok\n"
            "test tests::boom ... FAILED\n"
            "thread 'tests::boom' panicked at src/lib.rs:12:5:\n"
            "assertion `left == right` failed\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored\n",
            "panicked at",
            must_avoid=["N passed"],
        )

    def test_jest(self):
        self.check(
            "FAIL  src/parse.test.js\n"
            "  ● parse › handles empty input\n"
            "    TypeError: Cannot read properties of undefined "
            "(reading 'length')\n"
            "Tests:       1 failed, 2 passed, 3 total\n",
            "Cannot read properties of undefined",
            must_avoid=["total"],
        )

    def test_c_compiler(self):
        self.check(
            "gcc -c main.c\n"
            "main.c:5:12: error: 'widget' undeclared "
            "(first use in this function)\n"
            "make: *** [Makefile:4: main.o] Error 1\n",
            "undeclared",
        )

    def test_java_stack_trace(self):
        self.check(
            "Tests run: 3, Failures: 1, Errors: 0, Skipped: 0\n"
            "java.lang.NullPointerException: Cannot invoke "
            '"String.length()" because "s" is null\n'
            "\tat com.example.Widget.render(Widget.java:41)\n",
            "NullPointerException",
            must_avoid=["Failures"],
        )

    def test_segfault_with_no_message(self):
        self.check(
            "running suite\nSegmentation fault (core dumped)\n",
            "Segmentation fault",
        )


class OracleTests(unittest.TestCase):
    def test_requires_the_same_exit_code(self):
        subject = oracle.build(result(exit_code=3, stderr="ValueError: nope"))
        self.assertTrue(subject.holds(result(exit_code=3,
                                             stderr="ValueError: nope")))
        self.assertFalse(subject.holds(result(exit_code=1,
                                              stderr="ValueError: nope")))

    def test_requires_the_signature(self):
        subject = oracle.build(result(stderr="ValueError: bad widget id 4"))
        self.assertTrue(
            subject.holds(result(stderr="ValueError: bad widget id 9")))
        self.assertFalse(subject.holds(result(stderr="SyntaxError: invalid")))

    def test_user_regex_replaces_the_guess(self):
        subject = oracle.build(result(stderr="anything"), patterns=[r"KaBoom"])
        self.assertEqual(subject.signatures, [])
        self.assertTrue(subject.holds(result(stderr="a KaBoom happened")))
        self.assertFalse(subject.holds(result(stderr="a whimper happened")))

    def test_timeout_only_matches_a_timeout(self):
        subject = oracle.build(result(timed_out=True, exit_code=-1))
        self.assertTrue(subject.holds(result(timed_out=True, exit_code=-1)))
        self.assertFalse(subject.holds(result(exit_code=-1)))

    def test_ignore_exit(self):
        subject = oracle.build(result(exit_code=2, stderr="segfault happened"),
                               ignore_exit=True)
        self.assertIsNone(subject.exit_code)
        self.assertTrue(subject.holds(result(exit_code=139,
                                             stderr="segfault happened")))

    def test_explain_is_human_readable(self):
        subject = oracle.build(result(exit_code=1, stderr="ValueError: nope"))
        self.assertIn("exit status is 1", subject.explain())


# -- block detection -------------------------------------------------------


class BlockTests(unittest.TestCase):
    def lines(self, text):
        return textwrap.dedent(text).lstrip("\n").encode().splitlines(True)

    def test_indent_block_covers_a_function_body(self):
        lines = self.lines("""
            def outer():
                a = 1
                b = 2
            after = 3
        """)
        self.assertIn((0, 3), indent_blocks(lines))

    def test_brace_block_covers_a_c_function(self):
        lines = self.lines("""
            int main(void) {
                puts("hi");
                return 0;
            }
        """)
        self.assertIn((0, 4), brace_blocks(lines))

    def test_candidate_blocks_are_biggest_first(self):
        lines = self.lines("""
            def outer():
                def inner():
                    a = 1
                    b = 2
                c = 3
        """)
        sizes = [len(block) for block in candidate_blocks(lines)]
        self.assertTrue(sizes)
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_no_blocks_in_flat_text(self):
        self.assertEqual(indent_blocks(self.lines("a\nb\nc\n")), [])


class NamedInTests(unittest.TestCase):
    def test_matches_full_path_and_basename(self):
        output = ('  File "/tmp/x/app/core.py", line 5\n'
                  "    at handler.js:12\n")
        hot = named_in(output, ["app/core.py", "web/handler.js", "other.py"])
        self.assertEqual(sorted(hot), ["app/core.py", "web/handler.js"])

    def test_nothing_named_is_nothing_hot(self):
        self.assertEqual(named_in("boom", ["a.py"]), [])


# -- discovery -------------------------------------------------------------


class DiscoverTests(TempTree):
    def test_finds_files_and_prunes_caches(self):
        self.write("a.py", "x = 1\n")
        self.write("pkg/b.py", "y = 2\n")
        self.write("__pycache__/a.pyc", "junk\n")
        self.write("node_modules/dep/index.js", "module.exports = 1\n")
        paths = [entry.path for entry in tree.discover(self.root)]
        self.assertEqual(paths, ["a.py", "pkg/b.py"])

    def test_binary_files_are_not_reducible(self):
        with open(os.path.join(self.root, "blob.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02binary")
        entry = tree.discover(self.root)[0]
        self.assertFalse(entry.is_text)
        self.assertFalse(entry.reducible)

    def test_keep_pins_a_file(self):
        self.write("a.py", "x = 1\n")
        self.write("b.py", "y = 2\n")
        entries = {e.path: e for e in tree.discover(self.root, keep=["a.py"])}
        self.assertTrue(entries["a.py"].pinned)
        self.assertFalse(entries["b.py"].pinned)

    def test_only_pins_everything_else(self):
        self.write("a.py", "x = 1\n")
        self.write("b.py", "y = 2\n")
        entries = {e.path: e for e in tree.discover(self.root, only=["a.py"])}
        self.assertFalse(entries["a.py"].pinned)
        self.assertTrue(entries["b.py"].pinned)

    def test_exclude_removes_from_the_universe(self):
        self.write("a.py", "x = 1\n")
        self.write("skip/c.py", "z = 3\n")
        paths = [e.path for e in tree.discover(self.root, exclude=["skip/*"])]
        self.assertEqual(paths, ["a.py"])

    def test_count_lines(self):
        self.assertEqual(tree.count_lines(b""), 0)
        self.assertEqual(tree.count_lines(b"a\n"), 1)
        self.assertEqual(tree.count_lines(b"a\nb"), 2)


# -- runner ----------------------------------------------------------------


class RunnerTests(TempTree):
    def test_captures_exit_code_and_output(self):
        outcome = run([sys.executable, "-c",
                       "import sys; sys.stderr.write('boom'); sys.exit(3)"],
                      self.root, timeout=30)
        self.assertEqual(outcome.exit_code, 3)
        self.assertIn("boom", outcome.stderr)
        self.assertFalse(outcome.timed_out)

    def test_timeout_is_enforced(self):
        outcome = run([sys.executable, "-c", "import time; time.sleep(30)"],
                      self.root, timeout=1)
        self.assertTrue(outcome.timed_out)
        self.assertLess(outcome.duration, 20)

    def test_missing_binary_does_not_raise(self):
        outcome = run(["definitely-not-a-real-binary-xyz"], self.root, 5)
        self.assertNotEqual(outcome.exit_code, 0)

    def test_shell_detection(self):
        self.assertTrue(wants_shell(["make test | tail -1"]))
        self.assertFalse(wants_shell(["make", "test"]))
        self.assertFalse(wants_shell(["pytest"]))

    def test_describe_quotes_when_needed(self):
        self.assertEqual(describe(["pytest", "-k", "a b"]), "pytest -k 'a b'")


# -- workspace -------------------------------------------------------------


class WorkspaceTests(TempTree):
    def scratch(self, name):
        base = tempfile.mkdtemp(prefix="carve-copy-")
        self.addCleanup(shutil.rmtree, base, True)
        return os.path.join(base, name)

    def test_apply_writes_and_deletes(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "two\n")
        target = self.scratch("tree")
        copy_tree(self.root, target)

        seed = {"a.txt": b"one\n", "b.txt": b"two\n"}
        workspace = Workspace(target, list(seed), seed)
        workspace.apply({"a.txt": b"ONE\n"})

        with open(os.path.join(target, "a.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"ONE\n")
        self.assertFalse(os.path.exists(os.path.join(target, "b.txt")))

    def test_copy_tree_skips_git(self):
        self.write("a.txt", "one\n")
        self.write(".git/config", "[core]\n")
        target = self.scratch("tree")
        copy_tree(self.root, target)
        self.assertTrue(os.path.exists(os.path.join(target, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(target, ".git")))

    def test_purge_removes_what_a_run_left_behind(self):
        self.write("a.txt", "one\n")
        self.write("kept/b.txt", "two\n")
        target = self.scratch("tree")
        copy_tree(self.root, target)
        workspace = Workspace(target, ["a.txt", "kept/b.txt"],
                              {"a.txt": b"one\n", "kept/b.txt": b"two\n"})

        # Pretend the command wrote a cache file and a build directory.
        with open(os.path.join(target, "cache.bin"), "w") as handle:
            handle.write("junk")
        os.makedirs(os.path.join(target, "build", "deep"))
        with open(os.path.join(target, "build", "deep", "out.o"), "w") as fh:
            fh.write("junk")

        workspace.purge_strays()
        self.assertFalse(os.path.exists(os.path.join(target, "cache.bin")))
        self.assertFalse(os.path.exists(os.path.join(target, "build")))
        # Everything that was there to begin with survives.
        self.assertTrue(os.path.exists(os.path.join(target, "a.txt")))
        self.assertTrue(os.path.exists(os.path.join(target, "kept", "b.txt")))

    def test_purge_removes_a_deleted_file_the_command_recreated(self):
        self.write("a.txt", "one\n")
        target = self.scratch("tree")
        copy_tree(self.root, target)
        workspace = Workspace(target, ["a.txt"], {"a.txt": b"one\n"})
        workspace.apply({})                      # carve deletes it
        with open(os.path.join(target, "a.txt"), "w") as handle:
            handle.write("the command put it back\n")
        workspace.purge_strays()
        self.assertFalse(os.path.exists(os.path.join(target, "a.txt")))

    def test_key_is_content_addressed(self):
        self.assertEqual(Pool.key({"a": b"1"}), Pool.key({"a": b"1"}))
        self.assertNotEqual(Pool.key({"a": b"1"}), Pool.key({"a": b"2"}))
        self.assertNotEqual(Pool.key({"a": b"1"}), Pool.key({"b": b"1"}))


# -- end to end ------------------------------------------------------------


class EndToEndTests(TempTree):
    def build_project(self):
        for index in range(4):
            self.write("noise/mod_{0}.py".format(index), """
                VALUE = {0}


                def helper(x):
                    total = 0
                    for step in range(x):
                        total += step
                    return total
            """.format(index))
        self.write("app/core.py", """
            PREFIX = "id-"


            def label(items):
                return PREFIX + items[0]


            def unused(items):
                return len(items)
        """)
        self.write("main.py", """
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from app import core

            print("starting up")
            core.label([])
        """)

    def test_reduces_to_the_files_that_matter(self):
        self.build_project()
        outcome = carve(self.root, [sys.executable, "main.py"], jobs=2,
                        verify=1, passes=2)
        self.assertTrue(outcome.verified)
        self.assertEqual(sorted(outcome.kept_files), ["app/core.py", "main.py"])
        self.assertLess(outcome.after.lines, outcome.before.lines)
        self.assertGreater(outcome.runs, 0)
        # The bug is indexing an empty list, so that line has to survive.
        self.assertIn(b"items[0]", outcome.state["app/core.py"])
        # ...and the irrelevant helper must not.
        self.assertNotIn(b"unused", outcome.state["app/core.py"])

    def test_chars_level_cuts_inside_a_line(self):
        self.write("main.py", """
            def render(name, width, height, timeout=30, verbose=False):
                box = {"name": name, "w": width}
                return box["missing_key"]


            render("panel", 80, 24, timeout=15, verbose=True)
        """)
        shallow = carve(self.root, [sys.executable, "main.py"], jobs=4,
                        verify=1, passes=2, level="lines")
        deep = carve(self.root, [sys.executable, "main.py"], jobs=4,
                     verify=1, passes=2, level="chars")
        self.assertTrue(deep.verified)
        self.assertIn(b"missing_key", deep.state["main.py"])
        # Same lines, but less of each of them.
        self.assertLess(deep.after.bytes, shallow.after.bytes)
        self.assertNotIn(b"timeout=30", deep.state["main.py"])

    def test_shrink_command_drops_pointless_arguments(self):
        self.write("main.py", "raise ValueError('kaboom')\n")
        outcome = carve(self.root,
                        [sys.executable, "-B", "-u", "main.py"],
                        jobs=1, verify=1, passes=1, shrink_command=True)
        self.assertEqual(outcome.original_command[-1], "main.py")
        # -B and -u change nothing about the traceback, so they go.
        self.assertNotIn("-B", outcome.command)
        self.assertNotIn("-u", outcome.command)
        # The script itself is what produces the failure, so it stays.
        self.assertIn("main.py", outcome.command)
        self.assertTrue(outcome.verified)

    def test_command_is_untouched_by_default(self):
        self.write("main.py", "raise ValueError('kaboom')\n")
        outcome = carve(self.root, [sys.executable, "-B", "main.py"],
                        jobs=1, verify=1, passes=1)
        self.assertEqual(outcome.command, outcome.original_command)

    def test_a_command_that_poisons_its_own_workspace(self):
        # Fails once, then passes forever because of the marker it wrote.
        # Workspaces are reused, so without purging strays the second
        # candidate tested in a given workspace would silently pass.
        self.write("main.py", """
            import os

            if os.path.exists("marker.txt"):
                raise SystemExit(0)
            open("marker.txt", "w").write("x")
            raise ValueError("kaboom")
        """)
        self.write("extra.py", "UNUSED = 1\n")
        outcome = carve(self.root, [sys.executable, "main.py"], jobs=1,
                        verify=2, passes=2)
        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.kept_files, ["main.py"])

    def test_refuses_a_command_that_already_passes(self):
        self.write("main.py", "print('fine')\n")
        with self.assertRaises(ValueError) as caught:
            carve(self.root, [sys.executable, "main.py"], jobs=1, verify=1)
        self.assertIn("already succeeds", str(caught.exception))

    def test_keep_is_respected(self):
        self.build_project()
        outcome = carve(self.root, [sys.executable, "main.py"], jobs=2,
                        verify=1, passes=1, keep=["noise/mod_0.py"])
        self.assertIn("noise/mod_0.py", outcome.kept_files)

    def test_run_budget_stops_early(self):
        self.build_project()
        outcome = carve(self.root, [sys.executable, "main.py"], jobs=1,
                        verify=1, max_runs=4)
        self.assertTrue(outcome.truncated)
        self.assertTrue(outcome.notes)

    def test_source_tree_is_never_modified(self):
        self.build_project()

        def snapshot():
            seen = {}
            for folder, _, names in os.walk(self.root):
                for name in names:
                    full = os.path.join(folder, name)
                    with open(full, "rb") as handle:
                        seen[full] = handle.read()
            return seen

        before = snapshot()
        carve(self.root, [sys.executable, "main.py"], jobs=2, verify=1,
              passes=1)
        self.assertEqual(before, snapshot())


# -- report ----------------------------------------------------------------


class ReportTests(TempTree):
    def make_outcome(self):
        self.write("main.py", "raise SystemExit(1)\n")
        return carve(self.root, [sys.executable, "main.py"], jobs=1, verify=1,
                     passes=1)

    def scratch(self, name):
        base = tempfile.mkdtemp(prefix="carve-out-")
        self.addCleanup(shutil.rmtree, base, True)
        return os.path.join(base, name)

    def test_writes_a_runnable_tree_and_a_report(self):
        outcome = self.make_outcome()
        out_dir = self.scratch("r")
        report.write_tree(outcome, out_dir)
        report.write_markdown(outcome, os.path.join(out_dir, "REPRO.md"))
        report.write_json(outcome, os.path.join(out_dir, "carve.json"))

        self.assertTrue(os.path.exists(os.path.join(out_dir, "main.py")))
        with open(os.path.join(out_dir, "REPRO.md"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("Minimal reproduction", body)
        self.assertIn("main.py", body)

    def test_refuses_to_clobber_without_force(self):
        outcome = self.make_outcome()
        out_dir = tempfile.mkdtemp(prefix="carve-out-")
        self.addCleanup(shutil.rmtree, out_dir, True)
        with open(os.path.join(out_dir, "keepme"), "w") as handle:
            handle.write("precious\n")
        with self.assertRaises(ValueError):
            report.write_tree(outcome, out_dir)
        report.write_tree(outcome, out_dir, force=True)

    def test_refuses_to_write_over_the_source(self):
        outcome = self.make_outcome()
        with self.assertRaises(ValueError):
            report.write_tree(outcome, self.root)

    def test_report_never_overwrites_a_reduced_file(self):
        outcome = self.make_outcome()
        outcome.state["REPRO.md"] = b"# the project's own readme\n"
        out_dir = self.scratch("r")
        report.write_tree(outcome, out_dir)
        target = report.free_name(outcome, out_dir, "REPRO.md")
        self.assertEqual(os.path.basename(target), "REPRO.carve.md")
        report.write_markdown(outcome, target)
        with open(os.path.join(out_dir, "REPRO.md"), "rb") as handle:
            self.assertEqual(handle.read(), b"# the project's own readme\n")

    def test_free_name_passes_through_when_clear(self):
        outcome = self.make_outcome()
        self.assertEqual(
            os.path.basename(report.free_name(outcome, "/o", "carve.json")),
            "carve.json")

    def test_fence_language(self):
        self.assertEqual(report.fence_for("a/b.py"), "python")
        self.assertEqual(report.fence_for("Dockerfile"), "dockerfile")
        self.assertEqual(report.fence_for("weird.zzz"), "")

    def test_percent_drop_never_claims_a_total_wipe(self):
        from carve.term import percent_drop
        self.assertEqual(percent_drop(7929, 8), "99%")
        self.assertEqual(percent_drop(100, 0), "100%")
        self.assertEqual(percent_drop(4, 2), "50%")
        self.assertEqual(percent_drop(0, 0), "0%")

    def test_measure(self):
        stats = measure({"a": b"one\ntwo\n", "b": b""})
        self.assertEqual(stats.files, 2)
        self.assertEqual(stats.lines, 2)
        self.assertEqual(stats.bytes, 8)


# -- cli -------------------------------------------------------------------


class CliTests(TempTree):
    def test_split_command(self):
        self.assertEqual(cli.split_command(["-j", "2", "--", "make", "test"]),
                         (["-j", "2"], ["make", "test"]))
        self.assertEqual(cli.split_command(["-j", "2"]), (["-j", "2"], []))

    def test_parse_duration(self):
        self.assertEqual(cli.parse_duration("90"), 90)
        self.assertEqual(cli.parse_duration("30s"), 30)
        self.assertEqual(cli.parse_duration("10m"), 600)
        self.assertEqual(cli.parse_duration("2h"), 7200)

    def test_no_arguments_prints_help(self):
        self.assertEqual(cli.entrypoint([]), 2)

    def test_missing_directory(self):
        self.assertEqual(cli.entrypoint(["/no/such/place", "--", "true"]), 2)

    def test_end_to_end_through_the_cli(self):
        self.write("main.py", "raise ValueError('kaboom')\n")
        self.write("extra.py", "UNUSED = 1\n")
        base = tempfile.mkdtemp(prefix="carve-cli-")
        self.addCleanup(shutil.rmtree, base, True)
        out_dir = os.path.join(base, "out")
        code = cli.entrypoint([
            self.root, "-o", out_dir, "--quiet", "--no-color", "--verify", "1",
            "--passes", "1", "--", sys.executable, "main.py",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(out_dir, "main.py")))
        self.assertFalse(os.path.exists(os.path.join(out_dir, "extra.py")))


if __name__ == "__main__":
    unittest.main()
