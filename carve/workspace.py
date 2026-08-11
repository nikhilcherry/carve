"""Disposable copies of the tree, and the budget that governs testing them.

Every candidate is tested in a scratch copy.  The directory the user pointed
carve at is opened read-only and never written to.

Called by: carve/reduce.py.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import shutil
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from .oracle import Oracle
from .runner import RunResult, run

# State: relative path -> exact bytes.  A path missing from the mapping is a
# file that does not exist in that candidate.
State = Dict[str, bytes]

# Never worth copying into a scratch tree: stale caches that only slow it down.
COPY_SKIP = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}
)


class BudgetExhausted(Exception):
    """Raised when the run or time budget runs out mid-reduction."""


class Workspace:
    """One scratch tree, reused across candidates."""

    def __init__(self, root: str, managed: Sequence[str], seed: State) -> None:
        self.root = root
        self.managed = list(managed)
        # What is physically on disk right now, so `apply` only writes deltas.
        self.on_disk: State = dict(seed)
        # Everything the pristine copy contained.  Anything else appearing
        # later was made by the command, and must not survive into the next
        # candidate: a build artefact or a cache left behind by run N can make
        # run N+1 pass for a reason that has nothing to do with the deletion
        # being tested.
        self._managed_set = set(self.managed)
        self.pristine, self.pristine_dirs = self._scan()

    def _walk(self, topdown: bool = True):
        """Every (directory, prefix, filenames) pair, never following --link."""
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=topdown):
            # Never descend into a --link symlink: that is the user's real
            # node_modules, not ours to tidy.
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d))]
            relative = os.path.relpath(dirpath, self.root)
            prefix = ("" if relative == "."
                      else relative.replace(os.sep, "/") + "/")
            yield dirpath, prefix, filenames

    def _scan(self):
        files, dirs = set(), set()
        for _, prefix, filenames in self._walk():
            if prefix:
                dirs.add(prefix)
            for name in filenames:
                files.add(prefix + name)
        return files, dirs

    def purge_strays(self) -> None:
        """Delete anything the last run left behind."""
        for dirpath, prefix, filenames in self._walk(topdown=False):
            for name in filenames:
                path = prefix + name
                stray = path not in self.pristine
                # A managed file the command recreated after we deleted it is
                # just as much of a leak as a brand-new artefact.
                revived = path in self._managed_set and path not in self.on_disk
                if not (stray or revived):
                    continue
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass
            if prefix and prefix not in self.pristine_dirs:
                try:
                    os.rmdir(dirpath)          # only succeeds when now empty
                except OSError:
                    pass

    def apply(self, state: State) -> None:
        for path in self.managed:
            want = state.get(path)
            have = self.on_disk.get(path)
            if want is have or want == have:
                continue
            full = os.path.join(self.root, path)
            if want is None:
                try:
                    os.remove(full)
                except FileNotFoundError:
                    pass
                self.on_disk.pop(path, None)
            else:
                parent = os.path.dirname(full)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(full, "wb") as handle:
                    handle.write(want)
                self.on_disk[path] = want


def copy_tree(source: str, target: str, with_git: bool = False,
              links: Sequence[str] = ()) -> None:
    """Clone `source` into `target`, optionally symlinking heavy directories."""
    skip = set(COPY_SKIP)
    if not with_git:
        skip.add(".git")
    linked = {name.strip("/") for name in links if name.strip("/")}

    def ignore(directory: str, names: List[str]) -> List[str]:
        rel = os.path.relpath(directory, source).replace(os.sep, "/")
        prefix = "" if rel == "." else rel + "/"
        return [n for n in names
                if n in skip or n in linked or (prefix + n) in linked]

    shutil.copytree(source, target, symlinks=True, ignore=ignore)

    for name in sorted(linked):
        origin = os.path.join(source, name)
        if not os.path.exists(origin):
            continue
        destination = os.path.join(target, name)
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.lexists(destination):
            os.symlink(os.path.abspath(origin), destination)


class Pool:
    """A set of workspaces plus the cache, the budget and the dispatcher."""

    def __init__(
        self,
        source: str,
        managed: Sequence[str],
        seed: State,
        command: Sequence[str],
        oracle: Oracle,
        timeout: float,
        jobs: int = 1,
        with_git: bool = False,
        links: Sequence[str] = (),
        work_dir: Optional[str] = None,
        max_runs: Optional[int] = None,
        time_budget: Optional[float] = None,
        on_run: Optional[Callable[[int, bool], None]] = None,
    ) -> None:
        self.source = os.path.abspath(source)
        self.managed = list(managed)
        self.seed = dict(seed)
        self.command = list(command)
        self.oracle = oracle
        self.timeout = timeout
        self.jobs = max(1, jobs)
        self.with_git = with_git
        self.links = list(links)
        self.max_runs = max_runs
        self.time_budget = time_budget
        self.on_run = on_run

        self._base = tempfile.mkdtemp(prefix="carve-", dir=work_dir)
        self._lock = threading.Lock()
        self._slots: List[Optional[Workspace]] = []
        self._free: List[Workspace] = []
        self._cache: Dict[str, bool] = {}
        self._started = time.time()

        self.runs = 0
        self.cache_hits = 0
        self.run_seconds = 0.0
        self.copy_seconds = 0.0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Pool":
        self._release(self._acquire())      # pay for the first copy up front
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        shutil.rmtree(self._base, ignore_errors=True)

    @property
    def elapsed(self) -> float:
        return time.time() - self._started

    # -- workspace checkout ------------------------------------------------

    def _acquire(self) -> Workspace:
        while True:
            with self._lock:
                if self._free:
                    return self._free.pop()
                if len(self._slots) < self.jobs:
                    index = len(self._slots)
                    self._slots.append(None)
                    break
            time.sleep(0.005)

        started = time.time()
        root = os.path.join(self._base, "w{0}".format(index))
        copy_tree(self.source, root, self.with_git, self.links)
        workspace = Workspace(root, self.managed, self.seed)
        with self._lock:
            self._slots[index] = workspace
            self.copy_seconds += time.time() - started
        return workspace

    def _release(self, workspace: Workspace) -> None:
        with self._lock:
            self._free.append(workspace)

    # -- testing -----------------------------------------------------------

    @staticmethod
    def key(state: State) -> str:
        digest = hashlib.sha1()
        for path in sorted(state):
            digest.update(path.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(hashlib.sha1(state[path]).digest())
        return digest.hexdigest()

    def _charge(self) -> None:
        if self.max_runs is not None and self.runs >= self.max_runs:
            raise BudgetExhausted(
                "run budget of {0} reached".format(self.max_runs))
        if self.time_budget is not None and self.elapsed >= self.time_budget:
            raise BudgetExhausted("time budget reached")

    def test(self, state: State) -> bool:
        """Does this candidate still reproduce?  Cached and budgeted."""
        cache_key = self.key(state)
        with self._lock:
            if cache_key in self._cache:
                self.cache_hits += 1
                return self._cache[cache_key]
            self._charge()

        workspace = self._acquire()
        try:
            workspace.apply(state)
            result = run(self.command, workspace.root, self.timeout)
            workspace.purge_strays()
        finally:
            self._release(workspace)

        verdict = self.oracle.holds(result)
        with self._lock:
            self._cache[cache_key] = verdict
            self.runs += 1
            self.run_seconds += result.duration
            runs = self.runs
        if self.on_run is not None:
            self.on_run(runs, verdict)
        return verdict

    def prime(self, state: State, verdict: bool) -> None:
        """Record a verdict reached outside `test`, so it is not re-run."""
        with self._lock:
            self._cache[self.key(state)] = verdict

    def run_raw(self, state: State,
                command: Optional[Sequence[str]] = None) -> RunResult:
        """Execute a candidate and hand back the whole result, uncached."""
        workspace = self._acquire()
        try:
            workspace.apply(state)
            result = run(command or self.command, workspace.root, self.timeout)
            workspace.purge_strays()
        finally:
            self._release(workspace)
        with self._lock:
            self.runs += 1
            self.run_seconds += result.duration
        return result

    def test_all(self, states: Sequence[State]) -> List[bool]:
        """Verdict for every candidate, computed in parallel."""
        if not states:
            return []
        if self.jobs == 1 or len(states) == 1:
            return [self.test(state) for state in states]
        verdicts: List[bool] = [False] * len(states)
        failure: Optional[BaseException] = None
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.jobs, len(states))) as executor:
            futures = {executor.submit(self.test, state): index
                       for index, state in enumerate(states)}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    verdicts[index] = future.result()
                except BaseException as exc:            # noqa: BLE001
                    failure = failure or exc
        if failure is not None and not any(verdicts):
            raise failure
        return verdicts

    def first_accepted(self, states: Sequence[State]) -> Optional[int]:
        """Index of the earliest candidate that reproduces, or None.

        Later candidates may be tested speculatively in parallel, but the
        answer never depends on which worker finished first.
        """
        if not states:
            return None

        verdicts: Dict[int, bool] = {}
        undecided: List[int] = []
        for index, state in enumerate(states):
            with self._lock:
                hit = self._cache.get(self.key(state))
            if hit is None:
                undecided.append(index)
            else:
                self.cache_hits += 1
                verdicts[index] = hit

        while True:
            frontier = None
            for index in range(len(states)):
                if index not in verdicts:
                    frontier = index
                    break
                if verdicts[index]:
                    return index
            if frontier is None:
                return None

            batch = [i for i in undecided if i >= frontier][: self.jobs]
            if not batch:
                return None
            undecided = [i for i in undecided if i not in batch]

            if len(batch) == 1:
                verdicts[batch[0]] = self.test(states[batch[0]])
                continue

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(batch)) as executor:
                futures = {executor.submit(self.test, states[i]): i
                           for i in batch}
                failure: Optional[BaseException] = None
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    try:
                        verdicts[index] = future.result()
                    except BaseException as exc:        # noqa: BLE001
                        verdicts[index] = False
                        failure = failure or exc
            if failure is not None and not any(
                    verdicts.get(i) for i in batch):
                raise failure
