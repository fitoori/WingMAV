#!/usr/bin/env python3
"""
WingMAV - The MAVProxy Joystick Control Module for Logitech Wingman series joysticks. 

This module allows controlling an ArduPilot vehicle with a joystick.
Press and hold the joystick trigger to take control (vehicle enters GUIDED mode and RC override engages).
Release the trigger to relinquish control (vehicle reverts to previous or safe mode and RC override stops).
Additional buttons are mapped for emergency Return-to-Launch (RTL) and Disarm commands.

Joystick mappings (assumed for Logitech Wingman Extreme Digital 3D):
    - Axis 0: Roll (RC Channel 1)
    - Axis 1: Pitch (RC Channel 2)
    - Axis 2: Yaw (Twist; RC Channel 4)
    - Axis 3: Throttle Slider (RC Channel 3)
When the trigger is pressed, the current joystick position for roll, pitch, and yaw is saved as the "zero" reference.

Button mappings:
    - Trigger (Button index 0): Engage control (switch to GUIDED, capture neutral position)
    - Release trigger: Disengage control and revert to previous mode (fallback: LOITER → STABILIZE)
    - Button index 5: RTL (Return-to-Launch)
    - Button index 6: Disarm

Logging:
    - By default, log messages are printed to the MAVProxy console.
    - Set LOG_TO_FILE = True and adjust LOG_FILE_PATH to enable file logging.
    
Autoload:
    - Place this file in your MAVProxy modules folder.
    - To auto-load on startup, add the line: module load joystickctrl in your ~/.mavinit.rc

Author: github.com/fitoori
Date: March 5, 2025
"""

import time

try:
    import pygame  # type: ignore
except ImportError:  # pragma: no cover - pygame optional for external integrations
    pygame = None

from pymavlink import mavutil
from MAVProxy.modules.lib import mp_module

# Configuration variables
LOG_TO_FILE = False  # Set to True to enable logging to file
LOG_FILE_PATH = "/home/pi/joystick_control.log"  # Log file location

# Joystick axis indices for control mapping (0-based indexing, per pygame)
AXIS_ROLL     = 0  # Roll control (RC Channel 1)
AXIS_PITCH    = 1  # Pitch control (RC Channel 2)
AXIS_YAW      = 2  # Yaw (Twist) control (RC Channel 4)
AXIS_THROTTLE = 3  # Throttle slider control (RC Channel 3)

# Joystick button indices (0-based index)
BTN_TRIGGER = 0   # Trigger button: engage control when pressed, disengage when released
BTN_RTL     = 5   # Button to command RTL (Return-to-Launch)
BTN_DISARM  = 6   # Button to disarm the vehicle

class JoystickControlModule(mp_module.MPModule):
    def __init__(
        self,
        mpstate,
        *,
        log_to_file=None,
        log_file_path=None,
        init_pygame=True,
        auto_connect=True,
        pygame_module=None,
    ):
        """
        Initialize the joystick control module.

        Parameters allow external callers to customise integration:
        - ``log_to_file`` / ``log_file_path``: override default logging configuration.
        - ``init_pygame``: delay pygame setup when running in a headless test harness.
        - ``auto_connect``: defer joystick discovery until requested explicitly.
        - ``pygame_module``: inject a pygame-compatible shim for unit testing.
        """
        super(JoystickControlModule, self).__init__(mpstate, "joystickctrl", "Joystick control module")
        # Use instance variable for logging configuration
        if log_to_file is None:
            log_to_file = LOG_TO_FILE
        self.log_enabled = bool(log_to_file)
        self.log_file_path = log_file_path or LOG_FILE_PATH
        self.log_file = None

        # State variables
        self.joystick = None          # Pygame joystick object
        self.joy_id = None            # Joystick device ID
        self.control_active = False   # True if joystick control is active
        self.center_offsets = [0.0, 0.0, 0.0]  # Neutral offsets for roll, pitch, yaw
        self.prev_mode = None         # Flight mode prior to entering GUIDED
        self.last_override_time = 0   # Timestamp of last RC override send
        self._last_joystick_retry = 0
        self._pygame_ready = False
        self._auto_connect = auto_connect
        self._pg = pygame_module if pygame_module is not None else pygame
        self._pending_mode_change = None
        self._pending_mode_success = None
        self._pending_mode_failure = None
        self._pending_mode_plan = []
        self._pending_disarm_ack = None

        # Initialize logging if enabled
        if self.log_enabled:
            try:
                self.log_file = open(self.log_file_path, "a")
                self._log("Joystick control module started (file logging enabled).")
            except Exception as e:
                print(f"JoystickCtrl: ERROR opening log file {self.log_file_path}: {e}")
                self.log_file = None
                self.log_enabled = False

        # Initialize pygame joystick subsystem
        if init_pygame:
            self._initialize_pygame()

        # Attempt to connect to a joystick device
        if self._pygame_ready and self._auto_connect:
            self._connect_joystick()

        # Check for RC override module
        self.rc_module = self.module('rc')
        if self.rc_module is None:
            self._log("WARNING: 'rc' module not found. RC overrides will be sent directly via MAVLink.", error=True)
        else:
            self._clear_rc_override()

    def _initialize_pygame(self):
        """Initialise pygame's joystick subsystem, raising if unavailable."""
        if self._pg is None:
            raise ImportError(
                "pygame is not available. Install pygame or instantiate JoystickControlModule "
                "with init_pygame=False and provide a compatible event source."
            )
        try:
            self._pg.init()
            self._pg.joystick.init()
            self._pygame_ready = True
        except Exception as e:
            self._log(f"Failed to initialize pygame joystick system: {e}", error=True)
            self._pygame_ready = False
            if self.log_enabled and self.log_file:
                try:
                    self.log_file.close()
                finally:
                    self.log_file = None
                    self.log_enabled = False
            raise

    def ensure_pygame_ready(self):
        """Ensure pygame has been initialised. Intended for external callers."""
        if not self._pygame_ready:
            self._initialize_pygame()
        if self._pygame_ready and self._auto_connect and self.joystick is None:
            self._connect_joystick()

    def _connect_joystick(self):
        """Connect to the first available joystick."""
        if not self._pygame_ready or self._pg is None:
            return False
        self._last_joystick_retry = time.time()
        count = self._pg.joystick.get_count()
        if count < 1:
            self._log("No joystick detected. Waiting for a joystick connection.")
            return False
        try:
            js = self._pg.joystick.Joystick(0)
            js.init()
            self.joystick = js
            self.joy_id = js.get_id()
            name = js.get_name()
            axes = js.get_numaxes()
            buttons = js.get_numbuttons()
            self._log(f"Joystick connected: '{name}' (axes={axes}, buttons={buttons})")
            return True
        except Exception as e:
            self._log(f"Error initializing joystick: {e}", error=True)
            self.joystick = None
            self.joy_id = None
            return False

    def idle_task(self):
        """
        Process joystick events and send RC override messages.
        This method is called frequently by MAVProxy.
        """
        # Always service asynchronous state machines first so pending mode/disarm
        # transitions continue even if pygame is unavailable.
        self._service_async_transitions()

        # Handle events (button presses, axis movements, connection changes)
        if not self._pygame_ready or self._pg is None:
            return

        for event in self._pg.event.get():
            if event.type == self._pg.JOYBUTTONDOWN and self.joystick and event.joy == self.joy_id:
                if event.button == BTN_TRIGGER:
                    if not self.control_active:
                        self._activate_control()
                elif event.button == BTN_RTL:
                    self._log("RTL button pressed → Switching to RTL mode")
                    self._set_flight_mode(
                        "RTL",
                        success_msg="RTL button pressed → Vehicle confirmed RTL mode",
                        pending_msg="RTL button pressed → Requested RTL mode; awaiting confirmation.",
                        failure_msg="RTL button pressed → Failed to change to RTL mode.",
                    )
                elif event.button == BTN_DISARM:
                    self._log("Disarm button pressed → Disarming the vehicle")
                    self._disarm_vehicle()
            elif event.type == self._pg.JOYBUTTONUP and self.joystick and event.joy == self.joy_id:
                if event.button == BTN_TRIGGER:
                    if self.control_active:
                        self._deactivate_control()
            elif event.type == self._pg.JOYAXISMOTION and self.joystick and event.joy == self.joy_id:
                if self.control_active:
                    self._send_override()
            elif event.type == self._pg.JOYDEVICEADDED:
                if self.joystick is None:
                    self._log("Joystick device added. Attempting to initialize.")
                    self._connect_joystick()
            elif event.type == self._pg.JOYDEVICEREMOVED:
                if self.joystick and event.joy == self.joy_id:
                    self._handle_disconnection()

        if self.joystick is None and time.time() - self._last_joystick_retry > 2.0:
            self._last_joystick_retry = time.time()
            self._connect_joystick()

        # If no rc module, throttle direct override sending to ~10 Hz
        if self.control_active and self.rc_module is None:
            if time.time() - self.last_override_time > 0.1:
                self._send_override()

        # Run async transitions again in case the work above queued new requests.
        self._service_async_transitions()

    def _service_async_transitions(self):
        self._check_pending_mode_change()
        self._process_disarm_ack()

    def _activate_control(self):
        """Activate joystick control: save neutral offsets, switch to GUIDED mode, and begin RC override."""
        self.prev_mode = self.status.flightmode
        try:
            self.center_offsets[0] = self.joystick.get_axis(AXIS_ROLL)
            self.center_offsets[1] = self.joystick.get_axis(AXIS_PITCH)
            self.center_offsets[2] = self.joystick.get_axis(AXIS_YAW)
        except Exception as e:
            self._log(f"ERROR reading joystick axes for centering: {e}", error=True)
            return
        self._set_flight_mode(
            "GUIDED",
            success_msg="Trigger pressed → Entering GUIDED mode and enabling joystick control",
            pending_msg="Trigger pressed → Requested GUIDED mode; awaiting confirmation before continuing.",
            failure_msg="Trigger pressed → GUIDED mode switch FAILED, continuing in current mode.",
        )
        self.control_active = True
        self._send_override(force=True)

    def _deactivate_control(self):
        """Deactivate joystick control: clear overrides and revert to previous or safe flight mode."""
        self.control_active = False
        self._clear_rc_override()
        target_mode = self.prev_mode if self.prev_mode else "LOITER"
        plan = [
            {
                "mode": target_mode,
                "success_msg": f"Trigger released → Joystick control disabled, switched to {target_mode} mode",
                "pending_msg": f"Trigger released → Requested {target_mode} mode; awaiting confirmation.",
                "failure_msg": f"Failed to revert to {target_mode}. Attempting fallback to LOITER.",
            },
            {
                "mode": "LOITER",
                "success_msg": "Trigger released → Joystick control disabled, switched to LOITER mode",
                "pending_msg": "Trigger released → Requested LOITER mode; awaiting confirmation.",
                "failure_msg": "Fallback to LOITER failed. Attempting fallback to STABILIZE.",
            },
            {
                "mode": "STABILIZE",
                "success_msg": "Trigger released → Joystick control disabled, switched to STABILIZE mode",
                "pending_msg": "Trigger released → Requested STABILIZE mode; awaiting confirmation.",
                "failure_msg": "Trigger released → Joystick control disabled. WARNING: Failed to change flight mode!",
            },
        ]
        self._attempt_mode_sequence(plan)

    def _handle_disconnection(self):
        """Handle joystick disconnection by clearing control and switching to safe mode."""
        self._log("Joystick disconnected!", error=True)
        if self.control_active:
            self.control_active = False
            self._clear_rc_override()
            self._set_flight_mode(
                "LOITER",
                success_msg="Joystick was active. Switching to LOITER for safety.",
                pending_msg="Joystick was active. Requested LOITER mode for safety; awaiting confirmation.",
                failure_msg="Joystick was active but failed to switch to LOITER for safety.",
            )
        self.joystick = None
        self.joy_id = None

    def _send_override(self, force=False):
        """
        Read current joystick values, apply centering offsets, and send RC override messages.
        Maps deflections to PWM values for RC channels (roll, pitch, throttle, yaw).
        """
        if self.joystick is None:
            return
        try:
            roll_in     = self.joystick.get_axis(AXIS_ROLL)
            pitch_in    = self.joystick.get_axis(AXIS_PITCH)
            yaw_in      = self.joystick.get_axis(AXIS_YAW)
            throttle_in = self.joystick.get_axis(AXIS_THROTTLE)
        except Exception as e:
            self._log(f"ERROR reading joystick axes for override: {e}", error=True)
            return

        roll_deflect  = roll_in  - self.center_offsets[0]
        pitch_deflect = pitch_in - self.center_offsets[1]
        yaw_deflect   = yaw_in   - self.center_offsets[2]

        roll_pwm     = int(1500 + (roll_deflect  * 500))
        pitch_pwm    = int(1500 + (pitch_deflect * 500))
        yaw_pwm      = int(1500 + (yaw_deflect   * 500))
        throttle_pwm = int(1500 + (throttle_in * 500))
        roll_pwm     = max(1000, min(2000, roll_pwm))
        pitch_pwm    = max(1000, min(2000, pitch_pwm))
        yaw_pwm      = max(1000, min(2000, yaw_pwm))
        throttle_pwm = max(1000, min(2000, throttle_pwm))

        pwm_values = (roll_pwm, pitch_pwm, throttle_pwm, yaw_pwm)
        if not force and hasattr(self, "_last_pwm_values"):
            if pwm_values == self._last_pwm_values:
                return
        self._last_pwm_values = pwm_values
        if self.rc_module:
            override_source = self.rc_module.override
            if override_source is None:
                override_list = [0] * 8
            else:
                override_list = list(override_source)
            if len(override_list) < 8:
                override_list.extend([0] * (8 - len(override_list)))
            override_list[0] = roll_pwm
            override_list[1] = pitch_pwm
            override_list[2] = throttle_pwm
            override_list[3] = yaw_pwm
            try:
                self.rc_module.override = override_list
            except Exception as e:
                self._log(f"Failed to apply RC override via rc module: {e}", error=True)
                return
            if hasattr(self.rc_module, "override_period"):
                self.rc_module.override_period.force()
        else:
            self.master.mav.rc_channels_override_send(
                self.master.target_system,
                self.master.target_component,
                roll_pwm, pitch_pwm, throttle_pwm, yaw_pwm,
                0, 0, 0, 0
            )
        self.last_override_time = time.time()

    def _clear_rc_override(self):
        """Clear any RC override by setting channels 1-4 to 0 (no override)."""
        if self.rc_module:
            override_source = self.rc_module.override
            if override_source is None:
                override_list = [0] * 8
            else:
                override_list = list(override_source)
            if len(override_list) < 8:
                override_list.extend([0] * (8 - len(override_list)))
            override_list[0] = 0
            override_list[1] = 0
            override_list[2] = 0
            override_list[3] = 0
            try:
                self.rc_module.override = override_list
            except Exception as e:
                self._log(f"Failed to clear RC override via rc module: {e}", error=True)
                return
            if hasattr(self.rc_module, "override_period"):
                self.rc_module.override_period.force()
        else:
            try:
                self.master.mav.rc_channels_override_send(
                    self.master.target_system,
                    self.master.target_component,
                    0, 0, 0, 0, 0, 0, 0, 0
                )
            except Exception as e:
                self._log(f"ERROR clearing RC override: {e}", error=True)

    def _clone_mode_plan(self, plan):
        return [
            {
                "mode": entry.get("mode"),
                "success_msg": entry.get("success_msg"),
                "pending_msg": entry.get("pending_msg"),
                "failure_msg": entry.get("failure_msg"),
            }
            for entry in (plan or [])
            if entry.get("mode")
        ]

    def _attempt_mode_sequence(self, plan):
        plan_copy = self._clone_mode_plan(plan)
        if not plan_copy:
            return False
        first = plan_copy.pop(0)
        return self._set_flight_mode(
            first["mode"],
            success_msg=first.get("success_msg"),
            pending_msg=first.get("pending_msg"),
            failure_msg=first.get("failure_msg"),
            fallback_plan=plan_copy,
        )

    def _start_next_mode_from_plan(self, plan, previous_failure_msg=None):
        if previous_failure_msg:
            self._log(previous_failure_msg, error=True)
        if not plan:
            return False
        next_entry = plan[0]
        remaining = self._clone_mode_plan(plan[1:])
        return self._set_flight_mode(
            next_entry.get("mode"),
            success_msg=next_entry.get("success_msg"),
            pending_msg=next_entry.get("pending_msg"),
            failure_msg=next_entry.get("failure_msg"),
            fallback_plan=remaining,
        )

    def _clear_pending_mode_change(self):
        self._pending_mode_change = None
        self._pending_mode_success = None
        self._pending_mode_failure = None
        self._pending_mode_plan = []

    def _set_flight_mode(self, mode_name, *, success_msg=None, pending_msg=None, failure_msg=None, fallback_plan=None):
        """Request a flight mode change without blocking the event loop."""
        if not mode_name:
            if failure_msg:
                self._log(failure_msg, error=True)
            return False
        mode_name = mode_name.upper()

        existing = self._pending_mode_change
        existing_mode = None
        cancelled_pending_msg = None
        if existing:
            existing_mode = existing.get("mode")
            if existing_mode == mode_name:
                if pending_msg:
                    self._log(pending_msg)
                return None
            cancelled_pending_msg = (
                f"Cancelling pending flight mode change to {existing_mode}; requesting {mode_name} instead."
            )

        try:
            mode_mapping = self.master.mode_mapping()
        except Exception as e:
            self._log(f"Unable to retrieve mode mapping: {e}", error=True)
            if failure_msg:
                self._log(failure_msg, error=True)
            return False
        if mode_mapping is None or mode_name not in mode_mapping:
            self._log(f"Flight mode '{mode_name}' not recognized or not supported", error=True)
            if failure_msg:
                self._log(failure_msg, error=True)
            return False
        mode_id = mode_mapping[mode_name]
        current_mode = (self.status.flightmode or "").upper()
        if current_mode == mode_name:
            if success_msg:
                self._log(success_msg)
            self._clear_pending_mode_change()
            return True
        fallback_plan = self._clone_mode_plan(fallback_plan)
        try:
            self.master.set_mode(mode_id)
        except Exception as e:
            self._log(f"Failed to send mode change to {mode_name}: {e}", error=True)
            if existing:
                self._log(
                    "Continuing to monitor previously pending flight mode change"
                    + (f" to {existing_mode}" if existing_mode else "")
                    + ".",
                )
                return False
            return self._start_next_mode_from_plan(fallback_plan, failure_msg)
        if cancelled_pending_msg:
            self._log(cancelled_pending_msg)
            self._clear_pending_mode_change()
        self._pending_mode_change = {
            "mode": mode_name,
            "deadline": time.time() + 5.0,
        }
        self._pending_mode_success = success_msg
        self._pending_mode_failure = failure_msg
        self._pending_mode_plan = fallback_plan
        if pending_msg:
            self._log(pending_msg)
        return None

    def _check_pending_mode_change(self):
        if not self._pending_mode_change:
            return
        target = self._pending_mode_change.get("mode")
        current_mode = (self.status.flightmode or "").upper()
        if current_mode == target:
            if self._pending_mode_success:
                self._log(self._pending_mode_success)
            else:
                self._log(f"Flight mode change to {target} confirmed.")
            self._clear_pending_mode_change()
            return
        if time.time() < self._pending_mode_change.get("deadline", 0):
            return
        failure_msg = self._pending_mode_failure or f"Timed out waiting for confirmation of mode change to {target}"
        plan = self._pending_mode_plan
        self._clear_pending_mode_change()
        if plan:
            self._start_next_mode_from_plan(plan, failure_msg)
        else:
            self._log(failure_msg, error=True)

    def _disarm_vehicle(self):
        """Send disarm command to the vehicle without blocking the event loop."""
        if self._pending_disarm_ack:
            self._log("Disarm command already in progress; awaiting acknowledgement.")
            return
        if self._send_primary_disarm():
            self._pending_disarm_ack = {
                "stage": "primary",
                "deadline": time.time() + 2.0,
            }
        else:
            if self._send_fallback_disarm():
                self._log("Primary disarm command failed; issued fallback disarm command.", error=True)
                self._pending_disarm_ack = {
                    "stage": "fallback",
                    "deadline": time.time() + 2.0,
                }

    def _send_primary_disarm(self):
        try:
            self.master.arducopter_disarm()
            return True
        except Exception as e:
            self._log(f"Primary disarm command failed: {e}", error=True)
            return False

    def _send_fallback_disarm(self):
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                0, 0, 0, 0, 0, 0, 0
            )
            return True
        except Exception as e:
            self._log(f"ERROR: Disarm command failed: {e}", error=True)
            return False

    def _process_disarm_ack(self):
        pending = self._pending_disarm_ack
        if not pending:
            return
        if time.time() <= pending.get("deadline", 0):
            return
        if pending.get("stage") == "primary":
            self._log("Primary disarm command sent but not acknowledged; attempting fallback.", error=True)
            if self._send_fallback_disarm():
                self._pending_disarm_ack = {
                    "stage": "fallback",
                    "deadline": time.time() + 2.0,
                }
            else:
                self._pending_disarm_ack = None
        else:
            self._log("Fallback disarm command did not receive acknowledgement.", error=True)
            self._pending_disarm_ack = None

    def mavlink_packet(self, msg):
        msg_type = msg.get_type()
        if msg_type == "COMMAND_ACK":
            self._handle_command_ack(msg)
        elif msg_type == "HEARTBEAT":
            self._check_pending_mode_change()

    def _handle_command_ack(self, msg):
        if getattr(msg, "command", None) != mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
            return
        pending = self._pending_disarm_ack
        if not pending:
            return
        result = getattr(msg, "result", None)
        if result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
            self._log("Disarm command acknowledged by vehicle.")
        else:
            self._log(f"Disarm command rejected with MAV_RESULT {result}", error=True)
        self._pending_disarm_ack = None

    def _log(self, message, error=False):
        """
        Log a message to the MAVProxy console and (optionally) to a file.
        """
        prefix = "JoystickCtrl:"
        if error:
            prefix = "JoystickCtrl [WARN]:"
        log_msg = f"{prefix} {message}"
        print(log_msg)
        try:
            self.say(text=log_msg, priority='important' if error else 'normal')
        except Exception:
            pass
        if self.log_enabled and self.log_file:
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            self.log_file.write(f"[{timestamp}] {message}\n")
            self.log_file.flush()

    def unload(self):
        """
        Clean up on module unload: quit the joystick and close log file.
        """
        if self.joystick:
            try:
                self.joystick.quit()
            except Exception:
                pass
        self._log("Joystick control module unloaded.")
        if self.log_enabled and self.log_file:
            try:
                self.log_file.close()
            finally:
                self.log_file = None
                self.log_enabled = False

def init(mpstate, **kwargs):
    """Factory used by MAVProxy and external callers to construct the module."""
    return JoystickControlModule(mpstate, **kwargs)
