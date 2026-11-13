
## 04 Nov 2026 22:13 - 2 camera(s) together, 30 s, no AI

| Camera | source | picture | fps received | longest freeze | freezes > 0.5 s | status | verdict |
|---|---|---|---|---|---|---|---|
| cam_01 | 0 | 1280x720 | 25.4 | 0.07 s | 0 | OFFLINE ok | smooth |
| cam_02 | http://172.24.255.85:8080/video | 720x960 | 14.7 | 0.12 s | 0 | OFFLINE ok | smooth |

## 13 Nov 2026 02:30 - 4 camera(s) together, 10 s, no AI

| Camera | source | picture | fps received | longest freeze | freezes > 0.5 s | status | verdict |
|---|---|---|---|---|---|---|---|
| cam_01 | rtsp://***@192.168.0.90/axis-media/media.amp?videocodec=h264 | - | 0.0 | - | 0 | OFFLINE Axis password not set in secrets.local.yaml (cameras: axis: user / password) | NO PICTURE - Axis password not set in secrets.local.yaml (cameras: axis: user / password) |
| cam_02 | 0 | 1280x720 | 12.1 | 0.09 s | 0 | OFFLINE ok | smooth |
| cam_03 | http://10.248.34.114:8080/video | - | 0.0 | - | 0 | OFFLINE not reachable: 10.248.34.114 - open IP Webcam + 'Start server' (same Wi-Fi) | NO PICTURE - not reachable: 10.248.34.114 - open IP Webcam + 'Start server' (same Wi-Fi) |
| cam_04 | 1 | - | 0.0 | - | 0 | OFFLINE webcam '!HP Wide Vision' not connected - plug it in (Device Manager > Cameras) | NO PICTURE - webcam '!HP Wide Vision' not connected - plug it in (Device Manager > Cameras) |
