#!/usr/bin/env python3
"""Run MAVProxy and dynamically load the WingMAV joystick module.

This helper launches a MAVProxy instance configured to find the
``mavproxy_wingmav`` module that lives in this repository.  It is designed to
be started before the "main" flight-control program.  The process keeps
MAVProxy running in the background and watches its own STDIN for activity
from the main program.  As soon as any data arrives it "side loads" the
WingMAV joystick module so that joystick control becomes available.  When
installed via ``install.sh`` this script is typically exposed on ``PATH`` as
``wingmav-proxy``; when running directly from a checkout use the Python
invocation shown below.

Typical usage::

    python run_wingmav_proxy.py --master=udp:127.0.0.1:14550 \
        --out=udp:127.0.0.1:14551 --out=udp:0.0.0.0:14550

The script forwards any lines received on STDIN directly to the MAVProxy
instance, making it possible to chain additional commands from the main
script if desired.
"""

from __future__ import annotations

import argparse
import functools
import os
import select
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, List, Optional, TextIO


@functools.lru_cache(maxsize=1)
def _mavproxy_supports_mod_path(executable: str) -> bool:
    """Return ``True`` when ``mavproxy.py`` accepts the ``--mod-path`` flag."""

    try:
        result = subprocess.run(
            [executable, "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError:
        # If ``mavproxy.py`` cannot be executed we err on the safe side and
        # assume it does *not* support the option.  The subsequent launch will
        # fail in a clearer way.
        return False

    return "--mod-path" in result.stdout


def build_mavproxy_command(
    executable: str,
    master: str,
    baudrate: Optional[int],
    outs: Iterable[str],
    cmd: Iterable[str],
    mod_path: Path,
) -> List[str]:
    """Construct the command line used to launch MAVProxy."""

    command: List[str] = [executable, f"--master={master}"]

    if _mavproxy_supports_mod_path(executable):
        command.append(f"--mod-path={mod_path}")

    if baudrate:
        command.append(f"--baud={baudrate}")

    for out in outs:
        command.append(f"--out={out}")

    command.extend(cmd)

    # Ensure the RC module is available so WingMAV can piggy-back on it.
    if not any(part.startswith("--load-module") for part in command):
        command.append("--load-module=rc")

    return command


def stream_output(
    pipe: TextIO, prefix: str, on_line: Optional[Callable[[str], None]] = None
) -> None:
    """Continuously forward MAVProxy output to this program's stdout."""

    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            if on_line:
                on_line(line)
            sys.stdout.write(f"[{prefix}] {line}")
            sys.stdout.flush()
    finally:
        pipe.close()


WINGMAV_FAILURE_EXIT = 42


class WingMAVProxyRunner:
    """Supervisor for a MAVProxy process that can load WingMAV on demand."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.process: Optional[subprocess.Popen[str]] = None
        self.output_thread: Optional[threading.Thread] = None
        self.auto_load = args.auto_load
        self.joystick_loaded = False
        self._stop_requested = False
        self._wingmav_args = []
        if getattr(args, "manual_only", False):
            self._wingmav_args.append("manual_only=1")
        self.supervised_by = args.supervised_by
        self._wingmav_failure_detected = False
        self._last_returncode: Optional[int] = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        repo_root = Path(__file__).resolve().parent
        command = build_mavproxy_command(
            executable=self.args.mavproxy,
            master=self.args.master,
            baudrate=self.args.baud,
            outs=self.args.out,
            cmd=self.args.extra,
            mod_path=repo_root,
        )

        print("Launching MAVProxy with:")
        print("  " + " ".join(shlex.quote(part) for part in command))
        sys.stdout.flush()

        env = os.environ.copy()
        if not _mavproxy_supports_mod_path(self.args.mavproxy):
            repo_path = str(repo_root)
            existing_path = env.get("PYTHONPATH")
            if existing_path:
                env["PYTHONPATH"] = os.pathsep.join([repo_path, existing_path])
            else:
                env["PYTHONPATH"] = repo_path

        self._last_returncode = None
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        assert self.process.stdout is not None
        self.output_thread = threading.Thread(
            target=stream_output,
            args=(self.process.stdout, "MAVProxy", self._handle_mavproxy_line),
            daemon=True,
        )
        self.output_thread.start()

        if self.auto_load:
            self._load_wingmav_module()

    # ------------------------------------------------------------------
    def _load_wingmav_module(self) -> None:
        if not self.process or not self.process.stdin:
            return
        if self.joystick_loaded:
            return
        print("Loading WingMAV joystick module …")
        load_cmd = "module load wingmav"
        if self._wingmav_args:
            load_cmd += " " + " ".join(self._wingmav_args)
        self._write_to_mavproxy(load_cmd + "\n")
        self.joystick_loaded = True

    # ------------------------------------------------------------------
    def _write_to_mavproxy(self, data: str) -> None:
        if not self.process or not self.process.stdin:
            return
        self.process.stdin.write(data)
        self.process.stdin.flush()

    # ------------------------------------------------------------------
    def _handle_mavproxy_line(self, line: str) -> None:
        """Watch MAVProxy output for WingMAV specific failures."""

        if self._wingmav_failure_detected:
            return

        lower_line = line.lower()
        if "wingmav" not in lower_line:
            return

        failure_reason: Optional[str] = None
        if "failed to load module" in lower_line:
            failure_reason = "MAVProxy reported it failed to load WingMAV."
        elif "no module named" in lower_line and "wingmav" in lower_line:
            failure_reason = "WingMAV Python module was not found in MAVProxy's path."
        elif "exception" in lower_line or "traceback" in lower_line:
            failure_reason = "WingMAV module raised an exception inside MAVProxy."

        if failure_reason:
            self._report_wingmav_failure(failure_reason)

    # ------------------------------------------------------------------
    def _report_wingmav_failure(self, reason: str) -> None:
        self._wingmav_failure_detected = True
        print("Detected WingMAV failure: " + reason)
        if self.supervised_by:
            print(
                "Supervised by", self.supervised_by, "— signalling orchestrator for failover."
            )
        self.request_stop()

    # ------------------------------------------------------------------
    def run(self) -> int:
        if not self.process and not self._stop_requested:
            self.start()
        if not self.process:
            return 0

        try:
            while not self._stop_requested:
                if self.process.poll() is not None:
                    break

                try:
                    rlist, _, _ = select.select([sys.stdin], [], [], self.args.poll_interval)
                except InterruptedError:
                    continue
                if sys.stdin in rlist:
                    data = sys.stdin.readline()
                    if data == "":
                        # EOF → main script ended. Break out to terminate gracefully.
                        break
                    if not self.joystick_loaded:
                        self._load_wingmav_module()
                    if self.args.forward_stdin:
                        self._write_to_mavproxy(data)
        except KeyboardInterrupt:
            print("Received Ctrl+C. Stopping MAVProxy …")
        finally:
            self.stop()
        if self._wingmav_failure_detected:
            return WINGMAV_FAILURE_EXIT
        if self._last_returncode is not None:
            return self._last_returncode or 0
        return 0

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_requested = True
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("MAVProxy did not exit in time; killing …")
                self.process.kill()
        self._last_returncode = self.process.returncode
        self.process = None
        if self.output_thread and self.output_thread.is_alive():
            self.output_thread.join(timeout=1)
        self.output_thread = None

    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the runner to stop at the next opportunity."""

        self._stop_requested = True


# ----------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mavproxy",
        default=os.environ.get("MAVPROXY_BIN", "mavproxy.py"),
        help="Path to the mavproxy.py executable (default: %(default)s)",
    )
    parser.add_argument(
        "--master",
        default=os.environ.get("MAVPROXY_MASTER", "udp:127.0.0.1:14550"),
        help="MAVLink master connection string (default: %(default)s)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Serial baud rate when using a serial master connection",
    )
    parser.add_argument(
        "--out",
        action="append",
        default=[],
        help="Additional MAVLink --out endpoints (may be specified multiple times)",
    )
    parser.add_argument(
        "--extra",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra arguments passed verbatim to MAVProxy",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="How frequently (seconds) to poll for STDIN data (default: %(default)s)",
    )
    parser.add_argument(
        "--auto-load",
        action="store_true",
        help="Load the WingMAV module immediately instead of waiting for input",
    )
    parser.add_argument(
        "--forward-stdin",
        action="store_true",
        help="Forward any STDIN received to MAVProxy after triggering the module",
    )
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="Load WingMAV in manual-only mode (no automatic flight-mode changes)",
    )
    parser.add_argument(
        "--supervised-by",
        default=None,
        help="Name of the higher-level supervisor managing this runner",
    )

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    runner = WingMAVProxyRunner(args)

    def _handle_signal(_signum: int, _frame: Optional[object]) -> None:
        runner.request_stop()

    # Make sure we shut down cleanly when receiving termination signals.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
