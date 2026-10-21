"""The operator command confirms through a real terminal, not the fake one of the other tests.

Found in ATT1 on 2026-10-21 (v1.16.0): ``ssh -t contabo '... registrar att1/carrito ...'`` answered
"sin terminal no se confirma". ``open("/dev/tty", "r+")`` in text mode raises UnsupportedOperation
(not seekable), an OSError that was reported as "no terminal". The tests that replace Terminal could
not see it, so these run ``Terminal.ask`` in a child process whose controlling terminal is a real pty.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/flow_change_operator.py"

CHILD = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("op", {str(SCRIPT)!r})
op = importlib.util.module_from_spec(spec)
sys.modules["op"] = op
spec.loader.exec_module(op)
try:
    answer = op.Terminal().ask("codigo: ")
    print("RESULT=" + answer, flush=True)
except op.Refused as error:
    print("REFUSED=" + str(error), flush=True)
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")


def _run_in_pty(typed: bytes) -> str:
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child: its controlling terminal is the pty
        os.execv(sys.executable, [sys.executable, "-c", CHILD])
    output = b""
    try:
        while b"codigo: " not in output and b"REFUSED=" not in output:
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            output += chunk
        if b"codigo: " in output:
            os.write(fd, typed)
        while True:
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            output += chunk
    except OSError:  # the pty closes when the child exits
        pass
    finally:
        os.waitpid(pid, 0)
        os.close(fd)
    return output.decode("utf-8", "replace")


def test_ask_reads_the_code_from_a_real_terminal() -> None:
    output = _run_in_pty(b"k7p2\n")
    assert "codigo: " in output
    assert "RESULT=k7p2" in output, output


def test_without_a_terminal_it_refuses() -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,  # no controlling terminal: /dev/tty cannot be opened
        check=False,
        timeout=30,
    )
    assert "REFUSED=sin terminal no se confirma" in result.stdout, result.stdout + result.stderr
