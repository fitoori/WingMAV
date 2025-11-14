#!/usr/bin/env python3
"""High-reliability launcher for MAVProxy with WingMAV support.

This script is intended to be started automatically at login (for example via
`~/.profile`, a systemd user service, or the router's init system).  It keeps a
MAVProxy connection alive from ``/dev/ttyUSB0`` to ``udp:127.0.0.1:14550`` while
attempting to load the WingMAV joystick module when available.

Key behaviours
--------------
* The supervisor continually restarts MAVProxy whenever it exits.
* After a configurable number of failures the WingMAV module is disabled so the
  telemetry stream keeps flowing even if the joystick module is unhealthy.
* A separate diagnostic threshold adds extra MAVProxy arguments to aid
  troubleshooting once repeated failures occur.
* Long-lived successful runs reset the failure counters so WingMAV can be
  re-enabled automatically after a stable period.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_MASTER = "/dev/ttyUSB0"
DEFAULT_OUT = "udp:127.0.0.1:14550"
DEFAULT_BAUD = 115200


class MAVProxyOrchestrator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.failures = 0
        self.current_proc: Optional[subprocess.Popen] = None
        self.stop_requested = False
        self.wingmav_enabled = True
        self.diagnostic_mode = False
        self.repo_root = Path(__file__).resolve().parent
        self._pty_master: Optional[int] = None

    # ------------------------------------------------------------------
    def build_command(self) -> List[str]:
        master = self.args.master
        outs = self.args.out or [DEFAULT_OUT]
        baud = self.args.baud

        if self.wingmav_enabled:
            runner = self.repo_root / "run_wingmav_proxy.py"
            command: List[str] = [sys.executable, str(runner), f"--master={master}"]
            if baud:
                command.append(f"--baud={baud}")
            for out in outs:
                command.append(f"--out={out}")
            command.extend(self.args.extra)
            command.append("--auto-load")
            command.append("--forward-stdin")
        else:
            command = [self.args.mavproxy_bin, f"--master={master}"]
            if baud:
                command.append(f"--baud={baud}")
            for out in outs:
                command.append(f"--out={out}")
            command.append("--load-module=rc")
            command.extend(self.args.extra)

        if self.diagnostic_mode:
            command.extend(self.args.diagnostic_extra)

        return command

    # ------------------------------------------------------------------
    def run_once(self) -> int:
        command = self.build_command()
        pretty = " ".join(command)
        print(f"Starting MAVProxy command: {pretty}")
        sys.stdout.flush()

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")

        master_fd, slave_fd = os.openpty()

        # Launch MAVProxy connected to the slave side of the pseudo-terminal so
        # interactive users can work with it as if it were running directly in
        # the foreground.
        self.current_proc = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
        )

        os.close(slave_fd)
        self._pty_master = master_fd

        # Forward data between the controlling terminal and the MAVProxy child
        # while it is alive.  This keeps prompts responsive and allows
        # operators to type commands directly into MAVProxy when needed.
        stdin_fd: Optional[int]
        try:
            stdin_fd = sys.stdin.fileno()
        except (AttributeError, OSError):
            stdin_fd = None

        try:
            stdout_fd = sys.stdout.fileno()
        except (AttributeError, OSError):
            stdout_fd = None

        fds = [master_fd]
        if stdin_fd is not None:
            fds.append(stdin_fd)

        start_time = time.time()
        while True:
            if self.stop_requested and self.current_proc.poll() is None:
                self.current_proc.terminate()

            if self.current_proc.poll() is not None:
                break

            try:
                readable, _, _ = select.select(fds, [], [], 0.1)
            except InterruptedError:
                continue

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    data = b""
                if not data:
                    break
                if stdout_fd is not None:
                    os.write(stdout_fd, data)
                else:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

            if stdin_fd is not None and stdin_fd in readable:
                try:
                    user_input = os.read(stdin_fd, 1024)
                except OSError:
                    user_input = b""
                if not user_input:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    self._pty_master = None
                    break
                os.write(master_fd, user_input)

        try:
            return_code = self.current_proc.wait()
        finally:
            self.current_proc = None
            if self._pty_master is not None:
                try:
                    os.close(self._pty_master)
                except OSError:
                    pass
            self._pty_master = None

        runtime = time.time() - start_time
        print(f"MAVProxy exited with return code {return_code} after {runtime:.1f}s")

        if return_code == 0 and runtime >= self.args.success_reset:
            # Treat as successful run; reset diagnostics to give WingMAV another try.
            self.failures = 0
            if not self.wingmav_enabled:
                print("Stable run detected — re-enabling WingMAV module on next restart.")
            self.wingmav_enabled = True
            self.diagnostic_mode = False
        else:
            self.failures += 1
            print(f"Failure count now {self.failures}")
            if self.failures >= self.args.disable_wingmav_after:
                if self.wingmav_enabled:
                    print(
                        "Disabling WingMAV module due to repeated failures;"
                        " telemetry stream will continue without joystick support."
                    )
                self.wingmav_enabled = False
            if self.failures >= self.args.enable_diagnostics_after:
                if not self.diagnostic_mode:
                    print("Enabling diagnostic MAVProxy options for additional insight.")
                self.diagnostic_mode = True

        return return_code

    # ------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_requested:
            self.run_once()
            if self.stop_requested:
                break
            print(f"Restarting in {self.args.restart_delay}s …")
            for _ in range(int(self.args.restart_delay / 0.5)):
                if self.stop_requested:
                    break
                time.sleep(0.5)
            else:
                remaining = self.args.restart_delay % 0.5
                if remaining:
                    time.sleep(remaining)

    # ------------------------------------------------------------------
    def request_stop(self, *_: object) -> None:
        print("Stop requested — terminating child process if needed.")
        self.stop_requested = True
        if self.current_proc and self.current_proc.poll() is None:
            self.current_proc.terminate()
            try:
                self.current_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("Child did not exit promptly; killing …")
                self.current_proc.kill()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reliable MAVProxy launcher with WingMAV joystick support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Example:
              wingmav_orchestrator.py --baud=115200 \
                  --out udp:127.0.0.1:14550 --out udp:192.168.1.255:14550
            """
        ),
    )
    parser.add_argument("--master", default=DEFAULT_MASTER, help="MAVLink master connection")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Serial baud rate")
    parser.add_argument(
        "--out",
        action="append",
        default=[],
        metavar="ENDPOINT",
        help="Additional MAVLink --out endpoints (default includes udp:127.0.0.1:14550)",
    )
    parser.add_argument(
        "--mavproxy-bin",
        default=os.environ.get("MAVPROXY_BIN", "mavproxy.py"),
        help="Path to the mavproxy.py executable",
    )
    parser.add_argument(
        "--extra",
        nargs=argparse.REMAINDER,
        default=[],
        help="Additional arguments forwarded verbatim to MAVProxy",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=5.0,
        help="Delay (seconds) before restarting MAVProxy after it exits",
    )
    parser.add_argument(
        "--disable-wingmav-after",
        type=int,
        default=3,
        help="Disable WingMAV after this many consecutive failures",
    )
    parser.add_argument(
        "--enable-diagnostics-after",
        type=int,
        default=5,
        help="Add diagnostic MAVProxy options after this many failures",
    )
    parser.add_argument(
        "--diagnostic-extra",
        nargs="*",
        default=["--show-errors"],
        help="Extra MAVProxy arguments enabled during diagnostic mode",
    )
    parser.add_argument(
        "--success-reset",
        type=float,
        default=120.0,
        help="Runtime (seconds) treated as a successful session that resets counters",
    )

    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    orchestrator = MAVProxyOrchestrator(args)

    signal.signal(signal.SIGINT, orchestrator.request_stop)
    signal.signal(signal.SIGTERM, orchestrator.request_stop)

    orchestrator.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
