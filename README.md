# WingMAV - Logitech Wingman MAVProxy Module

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
    
## Automated installation

An installation helper is provided to deploy the module, install system
dependencies and perform common environment checks.

```bash
./install.sh
```

The installer acts as a guided wizard: it summarises the detected
environment, prompts before making changes, and reports the commands it
executes.  Answer the prompts to control each action, or run in
non-interactive mode with `-y/--yes` (and optionally `--non-interactive`).

By default the wizard will:

1. Detect or create a MAVProxy modules directory and install
   `mavproxy_wingmav.py` there.
2. Install system prerequisites (`python3`, `python3-pip`, `joystick`,
   etc.) via `apt` when available.
3. Offer to install missing Python packages (`mavproxy`, `pymavlink`,
   `pygame`) via `pip`, automatically using `--user` or
   `--break-system-packages` when required.
4. Copy the optional `wingmav-proxy` helper launcher into
   `/usr/local/bin` (or `~/.local/bin` when sudo is not used).
5. Add the invoking user to the `dialout` group to ensure serial
   permissions.
6. Run verification checks (Python imports and `mavproxy.py --version`).

Useful flags:

```
./install.sh --dry-run                 # Preview the actions without modifying the system
./install.sh -y --skip-apt             # Accept defaults but skip apt installs
./install.sh --module-dir ~/mav/modules
```

See `./install.sh --help` for the full list of options, including ways to
skip specific steps if you have already configured part of the system.

After the installer finishes, you can start MAVProxy with the WingMAV
module using `mavproxy.py --load-module=rc,wingmav`, or add the
following to your `~/.mavinit.rc` file to load it automatically:

```
module load wingmav
module load rc
```
