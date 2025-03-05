
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
