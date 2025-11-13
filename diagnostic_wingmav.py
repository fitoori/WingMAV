#!/usr/bin/env python3
"""WingMAV joystick and MAVProxy environment diagnostic tool.

This script is intended to be run on a workstation before using the
``mavproxy_wingmav`` module for actual flight control.  It verifies that the
runtime environment can see a joystick, inspects the MAVProxy installation,
listens for joystick inputs, and simulates the MAVLink traffic that the module
would normally emit.

Run ``python diagnostic_wingmav.py --help`` for usage details.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

try:
    from pymavlink import mavutil  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    mavutil = None  # type: ignore


# Default joystick mapping mirrors ``mavproxy_wingmav``.
AXIS_ROLL = 0
AXIS_PITCH = 1
AXIS_YAW = 2
AXIS_THROTTLE = 3

BTN_TRIGGER = 0
BTN_RTL = 5
BTN_DISARM = 6

AXIS_LABELS = {
    AXIS_ROLL: "Roll (RC1)",
    AXIS_PITCH: "Pitch (RC2)",
    AXIS_THROTTLE: "Throttle (RC3)",
    AXIS_YAW: "Yaw (RC4)",
}

BUTTON_LABELS = {
    BTN_TRIGGER: "Trigger / engage GUIDED",
    BTN_RTL: "RTL",
    BTN_DISARM: "Disarm",
}


def print_header() -> None:
    """Emit a high-level environment summary."""

    print("=== WingMAV Diagnostics ===")
    print(f"Python: {platform.python_version()} ({sys.executable})")
    uname = platform.uname()
    print(f"Platform: {uname.system} {uname.release} ({uname.machine})")
    print()


def describe_module(name: str) -> Tuple[str, Optional[str]]:
    """Return the availability and version string of a module."""

    try:
        module = __import__(name)
    except Exception as exc:  # pragma: no cover - purely informational
        return ("missing", str(exc))

    version = getattr(module, "__version__", None)
    if version is None and hasattr(module, "version"):
        with contextlib.suppress(Exception):
            version = module.version
    return ("available", version)


def report_environment() -> None:
    """Print information about pygame, pymavlink, and MAVProxy availability."""

    pygame_status, pygame_version = describe_module("pygame")
    pymav_status, pymav_version = describe_module("pymavlink")
    mavproxy_status, _ = describe_module("MAVProxy")

    print("=== Python Module Availability ===")
    print(f"pygame: {pygame_status}" + (f" (version {pygame_version})" if pygame_version else ""))
    print(f"pymavlink: {pymav_status}" + (f" (version {pymav_version})" if pymav_version else ""))
    print(f"MAVProxy: {mavproxy_status}")
    print()

    mavproxy_bin = os.environ.get("MAVPROXY_BIN") or shutil.which("mavproxy.py")
    print("=== MAVProxy Executable ===")
    if mavproxy_bin:
        print(f"mavproxy.py resolved to: {mavproxy_bin}")
    else:
        print("WARNING: mavproxy.py was not found on PATH. Set MAVPROXY_BIN or update PATH.")
    print()

    print("=== Relevant Environment Variables ===")
    for key in ("MAVPROXY_HOME", "MAVPROXY_BASE", "MAVINIT_RC", "SDL_VIDEODRIVER"):
        value = os.environ.get(key)
        print(f"{key}={value!r}" if value is not None else f"{key} is not set")
    print()


@dataclass
class JoystickInfo:
    index: int
    name: str
    guid: Optional[str]
    axes: int
    buttons: int
    hats: int
    trackballs: int


def ensure_pygame(args: argparse.Namespace):
    """Import and initialise pygame in a headless-friendly way."""

    try:
        import pygame  # type: ignore
    except ImportError as exc:
        print("ERROR: pygame is required to inspect joystick hardware.")
        print(f"       Install it via 'pip install pygame'. ({exc})")
        return None

    if not os.environ.get("DISPLAY") and not os.environ.get("SDL_VIDEODRIVER"):
        # Fall back to the SDL "dummy" driver when running without an X server.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    try:
        pygame.init()
        pygame.joystick.init()
    except Exception as exc:  # pragma: no cover - system dependent
        print(f"ERROR: pygame failed to initialise joystick support: {exc}")
        return None

    if args.verbose:
        print(f"pygame initialised using video driver: {pygame.display.get_driver()}")

    return pygame


def enumerate_joysticks(pygame_module) -> Dict[int, JoystickInfo]:
    """Return metadata about all joysticks currently visible to pygame."""

    info: Dict[int, JoystickInfo] = {}
    count = pygame_module.joystick.get_count()
    for idx in range(count):
        js = pygame_module.joystick.Joystick(idx)
        js.init()
        guid = None
        if hasattr(js, "get_guid"):
            with contextlib.suppress(Exception):
                guid = js.get_guid()
        info[idx] = JoystickInfo(
            index=idx,
            name=js.get_name(),
            guid=guid,
            axes=js.get_numaxes(),
            buttons=js.get_numbuttons(),
            hats=js.get_numhats(),
            trackballs=getattr(js, "get_numballs", lambda: 0)(),
        )
        js.quit()
    return info


def print_joystick_inventory(joysticks: Dict[int, JoystickInfo]) -> None:
    """Pretty-print detected joystick hardware."""

    print("=== Detected Joysticks ===")
    if not joysticks:
        print("No joystick devices detected by pygame.")
    for info in joysticks.values():
        guid_fragment = f" GUID={info.guid}" if info.guid else ""
        print(
            f"[{info.index}] {info.name}{guid_fragment} | "
            f"axes={info.axes} buttons={info.buttons} hats={info.hats} trackballs={info.trackballs}"
        )
    print()


class MavlinkReporter:
    """Helper that either emits simulated MAVLink activity or talks to a real endpoint."""

    def __init__(
        self,
        endpoint: Optional[str],
        *,
        target_system: int,
        target_component: int,
        source_system: int,
        source_component: int,
        wait_heartbeat: bool,
        verbose: bool,
    ) -> None:
        self.endpoint = endpoint
        self.target_system = target_system
        self.target_component = target_component
        self.verbose = verbose
        self._last_override: Optional[Tuple[int, int, int, int]] = None

        self.connection = None
        if endpoint and mavutil is not None:
            try:
                self.connection = mavutil.mavlink_connection(
                    endpoint,
                    source_system=source_system,
                    source_component=source_component,
                    autoreconnect=True,
                )
                if wait_heartbeat:
                    print("Waiting for MAVLink heartbeat …")
                    msg = self.connection.wait_heartbeat(timeout=10)
                    if msg:
                        print(
                            "Heartbeat received from system", msg.get("srcSystem"),
                            "component", msg.get("srcComponent"),
                        )
            except Exception as exc:  # pragma: no cover - I/O dependent
                print(f"WARNING: Failed to establish MAVLink connection ({exc}). Falling back to simulation.")
                self.connection = None
        elif endpoint and mavutil is None:
            print("WARNING: pymavlink is not installed; MAVLink commands will be simulated only.")

    def _emit(self, message: str) -> None:
        prefix = "[MAVLINK]" if self.connection else "[SIM]"
        print(f"{prefix} {message}")

    def set_mode(self, mode: str) -> None:
        self._emit(f"Set mode → {mode}")
        if self.connection:
            try:
                self.connection.set_mode(mode)
            except Exception as exc:  # pragma: no cover - autopilot dependent
                self._emit(f"ERROR sending set_mode: {exc}")

    def disarm(self) -> None:
        self._emit("Command: Disarm")
        if self.connection:
            try:
                self.connection.mav.command_long_send(
                    self.target_system,
                    self.target_component,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0,
                    0, 0, 0, 0, 0, 0, 0,
                )
            except Exception as exc:  # pragma: no cover - autopilot dependent
                self._emit(f"ERROR sending disarm command: {exc}")

    def send_rc_override(self, roll: int, pitch: int, throttle: int, yaw: int) -> None:
        payload = (roll, pitch, throttle, yaw)
        if payload == self._last_override:
            return
        self._last_override = payload
        self._emit(f"RC override: roll={roll} pitch={pitch} throttle={throttle} yaw={yaw}")
        if self.connection:
            try:
                self.connection.mav.rc_channels_override_send(
                    self.target_system,
                    self.target_component,
                    roll,
                    pitch,
                    throttle,
                    yaw,
                    0,
                    0,
                    0,
                    0,
                )
            except Exception as exc:  # pragma: no cover - autopilot dependent
                self._emit(f"ERROR sending RC override: {exc}")

    def clear_override(self) -> None:
        self._emit("Clear RC override")
        self._last_override = None
        if self.connection:
            try:
                self.connection.mav.rc_channels_override_send(
                    self.target_system,
                    self.target_component,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                )
            except Exception as exc:  # pragma: no cover - autopilot dependent
                self._emit(f"ERROR clearing RC override: {exc}")

    def close(self) -> None:
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.close()


class JoystickSession:
    """Interactive joystick diagnostics with simulated WingMAV behaviour."""

    def __init__(
        self,
        pygame_module,
        *,
        device_index: int,
        poll_hz: float,
        duration: Optional[float],
        mavlink: MavlinkReporter,
        verbose: bool,
    ) -> None:
        self.pg = pygame_module
        self.device_index = device_index
        self.poll_interval = 1.0 / poll_hz if poll_hz > 0 else 0.1
        self.duration = duration
        self.mavlink = mavlink
        self.verbose = verbose

        self.joystick = None
        self.axis_values: Dict[int, float] = {}
        self.center_offsets = {AXIS_ROLL: 0.0, AXIS_PITCH: 0.0, AXIS_YAW: 0.0}
        self.control_active = False
        self.prev_mode = "UNKNOWN"
        self._start_time = None

    def setup(self) -> bool:
        count = self.pg.joystick.get_count()
        if count == 0:
            print("ERROR: No joystick devices available.")
            return False
        if self.device_index >= count or self.device_index < 0:
            print(f"ERROR: Requested joystick index {self.device_index} is out of range (0-{count - 1}).")
            return False

        self.joystick = self.pg.joystick.Joystick(self.device_index)
        self.joystick.init()
        print(
            f"Using joystick [{self.device_index}] {self.joystick.get_name()} "
            f"(axes={self.joystick.get_numaxes()} buttons={self.joystick.get_numbuttons()})"
        )
        for axis in range(self.joystick.get_numaxes()):
            with contextlib.suppress(Exception):
                self.axis_values[axis] = self.joystick.get_axis(axis)
        self._start_time = time.monotonic()
        return True

    def _elapsed(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def run(self) -> None:
        assert self.joystick is not None
        print("Listening for joystick activity. Press Ctrl+C to exit.")
        try:
            while True:
                for event in self.pg.event.get():
                    self._handle_event(event)

                if self.control_active:
                    self._send_override()

                if self.duration is not None and self._elapsed() >= self.duration:
                    print("Duration reached. Exiting diagnostic loop.")
                    break

                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("Interrupted by user. Exiting diagnostic loop.")
        finally:
            self.mavlink.clear_override()
            self.pg.event.clear()
            self.joystick.quit()

    # ------------------------------------------------------------------
    def _handle_event(self, event) -> None:
        etype = event.type
        if etype == self.pg.JOYAXISMOTION:
            self.axis_values[event.axis] = event.value
            label = AXIS_LABELS.get(event.axis, f"Axis {event.axis}")
            print(f"Axis {event.axis} ({label}) → {event.value:+.3f}")
            if self.control_active:
                self._send_override(force=True)

        elif etype == self.pg.JOYBUTTONDOWN:
            label = BUTTON_LABELS.get(event.button, f"Button {event.button}")
            print(f"Button {event.button} ({label}) pressed")
            if event.button == BTN_TRIGGER:
                self._engage_control()
            elif event.button == BTN_RTL:
                self.mavlink.set_mode("RTL")
            elif event.button == BTN_DISARM:
                self.mavlink.disarm()

        elif etype == self.pg.JOYBUTTONUP:
            label = BUTTON_LABELS.get(event.button, f"Button {event.button}")
            print(f"Button {event.button} ({label}) released")
            if event.button == BTN_TRIGGER and self.control_active:
                self._disengage_control()

        elif etype == self.pg.JOYDEVICEADDED:
            print(f"Joystick device added (index={event.device_index}).")

        elif etype == self.pg.JOYDEVICEREMOVED:
            instance_id = getattr(event, "instance_id", getattr(event, "joy", "unknown"))
            print(f"Joystick device removed (instance_id={instance_id}).")
            if self.control_active and self.joystick:
                current_id = None
                if hasattr(self.joystick, "get_instance_id"):
                    with contextlib.suppress(Exception):
                        current_id = self.joystick.get_instance_id()
                if current_id is not None and instance_id == current_id:
                    print("Active joystick was disconnected. Disengaging control.")
                    self._disengage_control()

    # ------------------------------------------------------------------
    def _engage_control(self) -> None:
        assert self.joystick is not None
        for axis in (AXIS_ROLL, AXIS_PITCH, AXIS_YAW):
            with contextlib.suppress(Exception):
                self.center_offsets[axis] = self.joystick.get_axis(axis)
        self.prev_mode = "GUIDED"
        print(
            "Trigger engaged → capturing center offsets "
            f"(roll={self.center_offsets[AXIS_ROLL]:+.3f}, "
            f"pitch={self.center_offsets[AXIS_PITCH]:+.3f}, "
            f"yaw={self.center_offsets[AXIS_YAW]:+.3f})"
        )
        self.control_active = True
        self.mavlink.set_mode("GUIDED")
        self._send_override(force=True)

    # ------------------------------------------------------------------
    def _disengage_control(self) -> None:
        self.control_active = False
        self.mavlink.clear_override()
        print("Trigger released → control disengaged. Suggested fallback mode: LOITER")
        self.mavlink.set_mode("LOITER")

    # ------------------------------------------------------------------
    def _send_override(self, force: bool = False) -> None:
        roll = self.axis_values.get(AXIS_ROLL, 0.0)
        pitch = self.axis_values.get(AXIS_PITCH, 0.0)
        yaw = self.axis_values.get(AXIS_YAW, 0.0)
        throttle = self.axis_values.get(AXIS_THROTTLE, -1.0)

        roll_deflect = roll - self.center_offsets[AXIS_ROLL]
        pitch_deflect = pitch - self.center_offsets[AXIS_PITCH]
        yaw_deflect = yaw - self.center_offsets[AXIS_YAW]

        roll_pwm = int(max(1000, min(2000, 1500 + (roll_deflect * 500))))
        pitch_pwm = int(max(1000, min(2000, 1500 + (pitch_deflect * 500))))
        yaw_pwm = int(max(1000, min(2000, 1500 + (yaw_deflect * 500))))
        throttle_pwm = int(max(1000, min(2000, 1500 + (throttle * 500))))

        if force or self.verbose:
            print(
                "Computed PWM → "
                f"roll={roll_pwm} pitch={pitch_pwm} throttle={throttle_pwm} yaw={yaw_pwm}"
            )
        self.mavlink.send_rc_override(roll_pwm, pitch_pwm, throttle_pwm, yaw_pwm)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device-index",
        type=int,
        default=0,
        help="Joystick index to test (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Optional duration (seconds) to run before exiting",
    )
    parser.add_argument(
        "--poll-hz",
        type=float,
        default=20.0,
        help="How often to poll for events when idle (default: %(default)s)",
    )
    parser.add_argument(
        "--mavlink-endpoint",
        default=None,
        help="Optional pymavlink connection string (e.g. udpout:127.0.0.1:14550)",
    )
    parser.add_argument(
        "--target-system",
        type=int,
        default=1,
        help="Target system id for MAVLink commands (default: %(default)s)",
    )
    parser.add_argument(
        "--target-component",
        type=int,
        default=1,
        help="Target component id for MAVLink commands (default: %(default)s)",
    )
    parser.add_argument(
        "--source-system",
        type=int,
        default=254,
        help="Source system id for MAVLink messages (default: %(default)s)",
    )
    parser.add_argument(
        "--source-component",
        type=int,
        default=190,
        help="Source component id for MAVLink messages (default: %(default)s)",
    )
    parser.add_argument(
        "--wait-heartbeat",
        action="store_true",
        help="Block until a heartbeat is received when connecting to MAVLink",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list joysticks and exit (no interactive loop)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional debugging details",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)

    print_header()
    report_environment()

    pygame_module = ensure_pygame(args)
    if pygame_module is None:
        return 1

    try:
        joysticks = enumerate_joysticks(pygame_module)
        print_joystick_inventory(joysticks)

        if args.list_only:
            return 0

        mavlink = MavlinkReporter(
            args.mavlink_endpoint,
            target_system=args.target_system,
            target_component=args.target_component,
            source_system=args.source_system,
            source_component=args.source_component,
            wait_heartbeat=args.wait_heartbeat,
            verbose=args.verbose,
        )

        session = JoystickSession(
            pygame_module,
            device_index=args.device_index,
            poll_hz=args.poll_hz,
            duration=args.duration,
            mavlink=mavlink,
            verbose=args.verbose,
        )

        if not session.setup():
            return 1

        session.run()
        mavlink.close()
    finally:
        pygame_module.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
