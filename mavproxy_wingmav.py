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
import pygame
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
    def __init__(self, mpstate):
        """
        Initialize the joystick control module.
        Sets up pygame for joystick handling and prepares RC override.
        """
        super(JoystickControlModule, self).__init__(mpstate, "joystickctrl", "Joystick control module")
        # Use instance variable for logging configuration
        self.log_enabled = LOG_TO_FILE
        self.log_file = None

        # State variables
        self.joystick = None          # Pygame joystick object
        self.joy_id = None            # Joystick device ID
        self.control_active = False   # True if joystick control is active
        self.center_offsets = [0.0, 0.0, 0.0]  # Neutral offsets for roll, pitch, yaw
        self.prev_mode = None         # Flight mode prior to entering GUIDED
        self.last_override_time = 0   # Timestamp of last RC override send

        # Initialize logging if enabled
        if self.log_enabled:
            try:
                self.log_file = open(LOG_FILE_PATH, "a")
                self._log("Joystick control module started (file logging enabled).")
            except Exception as e:
                print(f"JoystickCtrl: ERROR opening log file {LOG_FILE_PATH}: {e}")
                self.log_file = None
                self.log_enabled = False

        # Initialize pygame joystick subsystem
        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as e:
            self._log(f"Failed to initialize pygame joystick system: {e}", error=True)
            return

        # Attempt to connect to a joystick device
        self._connect_joystick()

        # Check for RC override module
        self.rc_module = self.module('rc')
        if self.rc_module is None:
            self._log("WARNING: 'rc' module not found. RC overrides will be sent directly via MAVLink.", error=True)
        else:
            self._clear_rc_override()

    def _connect_joystick(self):
        """Connect to the first available joystick."""
        count = pygame.joystick.get_count()
        if count < 1:
            self._log("No joystick detected. Waiting for a joystick connection.")
            return False
        try:
            js = pygame.joystick.Joystick(0)
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
        # Handle events (button presses, axis movements, connection changes)
        for event in pygame.event.get():
            if self.joystick is None:
                continue
            if event.type == pygame.JOYBUTTONDOWN and event.joy == self.joy_id:
                if event.button == BTN_TRIGGER:
                    if not self.control_active:
                        self._activate_control()
                elif event.button == BTN_RTL:
                    self._log("RTL button pressed → Switching to RTL mode")
                    self._set_flight_mode("RTL")
                elif event.button == BTN_DISARM:
                    self._log("Disarm button pressed → Disarming the vehicle")
                    self._disarm_vehicle()
            elif event.type == pygame.JOYBUTTONUP and event.joy == self.joy_id:
                if event.button == BTN_TRIGGER:
                    if self.control_active:
                        self._deactivate_control()
            elif event.type == pygame.JOYAXISMOTION and event.joy == self.joy_id:
                if self.control_active:
                    self._send_override()
            elif event.type == pygame.JOYDEVICEADDED:
                if self.joystick is None:
                    self._log("Joystick device added. Attempting to initialize.")
                    self._connect_joystick()
            elif event.type == pygame.JOYDEVICEREMOVED:
                if self.joystick and event.joy == self.joy_id:
                    self._handle_disconnection()

        # If no rc module, throttle direct override sending to ~10 Hz
        if self.control_active and self.rc_module is None:
            if time.time() - self.last_override_time > 0.1:
                self._send_override()

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
        if self._set_flight_mode("GUIDED"):
            self._log("Trigger pressed → Entering GUIDED mode and enabling joystick control")
        else:
            self._log("Trigger pressed → Enabling joystick control (GUIDED mode switch FAILED, continuing in current mode)", error=True)
        self.control_active = True
        self._send_override(force=True)

    def _deactivate_control(self):
        """Deactivate joystick control: clear overrides and revert to previous or safe flight mode."""
        self.control_active = False
        self._clear_rc_override()
        target_mode = self.prev_mode if self.prev_mode else "LOITER"
        success = self._set_flight_mode(target_mode)
        if not success:
            self._log(f"Failed to revert to {target_mode}. Attempting fallback to LOITER.", error=True)
            success = self._set_flight_mode("LOITER")
            target_mode = "LOITER" if success else target_mode
            if not success:
                self._log("Fallback to LOITER failed. Attempting fallback to STABILIZE.", error=True)
                success = self._set_flight_mode("STABILIZE")
                target_mode = "STABILIZE" if success else target_mode
        if success:
            self._log(f"Trigger released → Joystick control disabled, switched to {target_mode} mode")
        else:
            self._log("Trigger released → Joystick control disabled. WARNING: Failed to change flight mode!", error=True)

    def _handle_disconnection(self):
        """Handle joystick disconnection by clearing control and switching to safe mode."""
        self._log("Joystick disconnected!", error=True)
        if self.control_active:
            self.control_active = False
            self._clear_rc_override()
            self._log("Joystick was active. Switching to LOITER for safety.")
            self._set_flight_mode("LOITER")
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
            override_list = list(self.rc_module.override)
            override_list[0] = roll_pwm
            override_list[1] = pitch_pwm
            override_list[2] = throttle_pwm
            override_list[3] = yaw_pwm
            self.rc_module.override = override_list
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
            override_list = list(self.rc_module.override)
            override_list[0] = 0
            override_list[1] = 0
            override_list[2] = 0
            override_list[3] = 0
            self.rc_module.override = override_list
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

    def _set_flight_mode(self, mode_name):
        """
        Change flight mode to the specified mode (e.g. "GUIDED", "LOITER").
        Returns True if successful.
        """
        if not mode_name:
            return False
        mode_name = mode_name.upper()
        mode_mapping = self.master.mode_mapping()
        if mode_mapping is None or mode_name not in mode_mapping:
            self._log(f"Flight mode '{mode_name}' not recognized or not supported", error=True)
            return False
        mode_id = mode_mapping[mode_name]
        try:
            self.master.set_mode(mode_id)
            return True
        except Exception as e:
            self._log(f"Failed to send mode change to {mode_name}: {e}", error=True)
            return False

    def _disarm_vehicle(self):
        """Send disarm command to the vehicle."""
        try:
            self.master.arducopter_disarm()
        except Exception as e:
            try:
                self.master.mav.command_long_send(
                    self.master.target_system,
                    self.master.target_component,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0,
                    0, 0, 0, 0, 0, 0, 0
                )
            except Exception as e2:
                self._log(f"ERROR: Disarm command failed: {e2}", error=True)

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
        if self.log_enabled and self.log_file:
            self.log_file.close()
        self._log("Joystick control module unloaded.")

def init(mpstate):
    return JoystickControlModule(mpstate)
