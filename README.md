# WingMAV - Logitech Wingman MAVProxy Module

WingMAV is a MAVProxy joystick module for flying ArduPilot vehicles with a Logitech Wingman Extreme Digital 3D. Press and hold the trigger to take control (the vehicle switches to GUIDED and RC override engages), then release it to hand control back (the module restores the previous mode when possible, falling back to LOITER → STABILIZE if needed). Additional buttons provide quick Return-to-Launch (RTL) and Disarm actions.

## Joystick layout
- **Axes** (zero captured when the trigger is pressed):
  - Axis 0: Roll (RC Channel 1)
  - Axis 1: Pitch (RC Channel 2)
  - Axis 2: Yaw / twist (RC Channel 4)
  - Axis 3: Throttle slider (RC Channel 3)
- **Buttons**:
  - Trigger (Button 0): Engage control, switch to GUIDED, and capture neutral stick positions
  - Trigger release: Disengage control and restore the previous mode (fallback: LOITER → STABILIZE)
  - Button 5: RTL (Return-to-Launch)
  - Button 6: Disarm

## Logging and modes
- Console logging is enabled by default. Set `LOG_TO_FILE = True` and adjust `LOG_FILE_PATH` inside `mavproxy_wingmav.py` to enable file logging.

### Mode behavior
- **Default behavior (mode switching enabled)**
  - Trigger press: captures stick centers, switches to GUIDED, and starts RC override.
  - Trigger release: stops override and restores the previous mode when known (fallback: LOITER → STABILIZE).
  - Joystick disconnect while active: clears override and commands LOITER for safety.
- **Manual-only mode (no mode changes)**
  - Trigger press/release only toggles RC override; the current vehicle mode is left untouched.
  - Joystick disconnect while active: clears override but does not attempt a mode change.
- Enable manual-only mode with `module load wingmav manual_only=1` inside MAVProxy or pass `--manual-only` to `run_wingmav_proxy.py`.

## Installation
Use the interactive installer to deploy the module, satisfy dependencies, and run common checks:
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

Manual-only mode:
    - Prevents WingMAV from changing the vehicle flight mode when the trigger is pressed
      or released. RC override is still applied, allowing the current mode to accept or
      reject the commands.
    - Enable via ``module load wingmav manual_only=1`` inside MAVProxy or pass
      ``--manual-only`` to the helper launcher (``wingmav-proxy`` when installed, or
      ``python run_wingmav_proxy.py`` from the repository checkout).
    
## Automated installation

An installation helper is provided to deploy the module, install system
dependencies and perform common environment checks.

```bash
./install.sh
```

The wizard summarizes the detected environment, prompts before making changes, and reports each command it runs. Typical actions include:

1. Installing `mavproxy_wingmav.py` into your MAVProxy modules directory (creating one if missing).
2. Installing system prerequisites via `apt` when available.
3. Offering to install missing Python packages (`mavproxy`, `pymavlink`, `pygame`) via `pip` with safe flags (`--user` or `--break-system-packages` when required).
4. Optionally installing the `wingmav-proxy` helper launcher into `/usr/local/bin` (or `~/.local/bin` when sudo is unavailable).
5. Adding the invoking user to the `dialout` group for serial port access.
6. Running verification checks (`mavproxy.py --version` and Python imports).

Useful flags:

```bash
./install.sh --dry-run                 # Preview actions without modifying the system
./install.sh -y --skip-apt             # Accept defaults but skip apt installs
./install.sh --module-dir ~/mav/modules  # Override the MAVProxy modules directory
```

See `./install.sh --help` for the full list of options, including ways to skip specific steps once they are already configured.

## Running WingMAV
Load the module directly in MAVProxy:

```bash
mavproxy.py --load-module=rc,wingmav
# Or add to ~/.mavinit.rc
module load wingmav
module load rc
```

### Helper launcher
`run_wingmav_proxy.py` starts MAVProxy with the module available and waits to side-load it after the main program sends input on STDIN. Example:

```bash
python run_wingmav_proxy.py --master=udp:127.0.0.1:14550 \
    --out=udp:127.0.0.1:14551 --out=udp:0.0.0.0:14550
```

### Diagnostics
Use the diagnostic tool to confirm joystick visibility and simulate MAVLink traffic before flight:

```bash
python diagnostic_wingmav.py --help
```

## Always-on MAVProxy orchestrator
`wingmav_orchestrator.py` supervises a MAVProxy link for unattended setups. Launch it from a user service or login script to keep a serial connection alive while opportunistically enabling the joystick module:

```bash
./wingmav_orchestrator.py \
    --master=/dev/ttyUSB0 --baud=115200 --out udp:127.0.0.1:14550
```

If MAVProxy repeatedly fails, the orchestrator restarts it, temporarily disables WingMAV to keep telemetry flowing, and adds extra diagnostics when problems persist.
