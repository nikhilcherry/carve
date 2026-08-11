"""Run the user's command and capture what happened.

Called by: carve/reduce.py (baseline), carve/workspace.py (every candidate).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Characters that mean the user handed us a shell fragment rather than argv.
_SHELL_CHARS = set("|&;<>$`(){}*?[]~!#\n")


@dataclass
class RunResult:
    """The observable behaviour of one command invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def output(self) -> str:
        """stderr first: that is where failures usually announce themselves."""
        if self.stderr and self.stdout:
            return self.stderr + "\n" + self.stdout
        return self.stderr or self.stdout


def wants_shell(command: Sequence[str]) -> bool:
    """`carve . -- 'make test 2>&1 | tail'` should go through /bin/sh."""
    if len(command) != 1:
        return False
    return any(char in _SHELL_CHARS for char in command[0])


def run(
    command: Sequence[str],
    cwd: str,
    timeout: float,
    env: Optional[Dict[str, str]] = None,
    shell: Optional[bool] = None,
) -> RunResult:
    """Execute `command` in `cwd`, never letting it outlive `timeout`."""
    use_shell = wants_shell(command) if shell is None else shell
    argv = command[0] if use_shell else list(command)

    child_env = dict(os.environ)
    child_env.update(env or {})
    # Deterministic output beats pretty output when we are diffing failures.
    child_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    child_env.setdefault("NO_COLOR", "1")
    child_env.setdefault("TERM", "dumb")
    child_env["CARVE"] = "1"

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=use_shell,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return RunResult(127, "", "carve: cannot start command: {0}".format(exc),
                         time.time() - started)

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - very rare
            proc.kill()
            out, err = b"", b""

    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        duration=time.time() - started,
        timed_out=timed_out,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """A test runner that spawns children must not leave them behind."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def describe(command: Sequence[str]) -> str:
    """A copy-pasteable rendering of the command, for reports."""
    if wants_shell(command):
        return command[0]
    parts: List[str] = []
    for token in command:
        if not token or any(c in token for c in " \t\"'\\|&;<>$`()*?[]"):
            parts.append("'" + token.replace("'", "'\\''") + "'")
        else:
            parts.append(token)
    return " ".join(parts)
