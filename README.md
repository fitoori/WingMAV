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
    
### 1.	Place the file in your MAVProxy modules folder (e.g. ~/.mavproxy/modules)
mkdir -p ~/.mavproxy/modules
cp mavproxy_wingmav.py ~/.mavproxy/modules/
chmod +x ~/.mavproxy/modules/mavproxy_wingmav.py


### 2.	Test by starting MAVProxy:
  mavproxy.py --master=udp:127.0.0.1:14550 --load-module=rc,wingmav

### 3.	Auto-load on startup:
#### Add the following line to your ~/.mavinit.rc file:

module load wingmav
module load rc
