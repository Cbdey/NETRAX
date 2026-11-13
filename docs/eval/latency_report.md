# NETRAX measured latency (tools/latency_report.py)

All values measured on the running system; median and 95th percentile.

## 1 camera live (laptop webcam 640x480; phone and USB entrance camera offline; 1 dashboard open; nobody in view, so no incident numbers) - 02 Nov 2026 16:29 (120 s)

Runtime: PyTorch (ultralytics) + shared batched engine; GPU engine: {'batch_ms_p50': 57.3, 'batch_ms_p95': 69.1, 'wait_ms_p50': 10.1, 'wait_ms_p95': 17.1, 'batch_size_avg': 1.0}

| Camera | status | processed fps (median) | capture -> processed p50 | p95 | model p50 | wait for GPU p50 | stale frames skipped |
|---|---|---|---|---|---|---|---|
| cam_01 | ONLINE | 11.9 | 101 ms | 138 ms | 56 ms | 10 ms | 0 |
| cam_02 | OFFLINE | 0 | - | - | - | - | 0 |
| cam_04 | OFFLINE | 0 | - | - | - | - | 0 |

| Measurement | n | median | 95th percentile | max |
|---|---|---|---|---|
| live picture age on the dashboard | 1236 | 198 ms | 282 ms | 333 ms |
| live frame edge -> backend | 711 | 112 ms | 148 ms | 186 ms |

## 3 cameras after the 4 Nov speed work (laptop webcam 640x480 + two recorded PPE videos 1280x720 and 1080x1920 standing in for phones, 30 fps each; AI in its own process; 1 dashboard open with live video over WebSocket) - 04 Nov 2026 23:26 (120 s)

Runtime: PyTorch (ultralytics) + shared batched engine; GPU engine: {'batch_ms_p50': 152.3, 'batch_ms_p95': 205.6, 'wait_ms_p50': 153.9, 'wait_ms_p95': 351.9, 'batch_size_avg': 1.53, 'gpu_busy_share': 0.97, 'process': True}

| Camera | status | processed fps (median) | capture -> processed p50 | p95 | model p50 | wait for GPU p50 | stale frames skipped |
|---|---|---|---|---|---|---|---|
| cam_01 | ONLINE | 2.1 | 368 ms | 706 ms | 155 ms | 178 ms | 0 |
| cam_02 | ONLINE | 1.8 | 399 ms | 1.21 s | 158 ms | 198 ms | 0 |
| cam_03 | ONLINE | 2.0 | 362 ms | 696 ms | 153 ms | 178 ms | 0 |

| Measurement | n | median | 95th percentile | max |
|---|---|---|---|---|
| frame captured -> event stored (backend) | 2 | 726 ms | 1.01 s | 1.04 s |
| frame captured -> event on the dashboard | 2 | 742 ms | 1.03 s | 1.06 s |
| live picture age on the dashboard | 2000 | 44 ms | 121 ms | 490 ms |
| live frame edge -> backend | 2000 | 37 ms | 66 ms | 118 ms |
| hazard first seen -> alert (includes the deliberate confirmation window) | 2 | 3.82 s | 6.88 s | 7.22 s |

_Note (4 Nov): during this run a software-rendered test browser (headless Edge, no GPU) decoded all 36 live pictures per second on the same CPU, which slowed the AI process (model p50 155 ms vs 75 ms without it). Without that browser the same 3 cameras ran at 3.4-3.9 AI checks per second each (edge [perf] lines, data in the run log). Real phones at 960x540 / 15 fps (tools.phone_tune) cost far less CPU than these 30 fps recorded videos._

## 5 Nov LIVE: laptop webcam = welding bay (640x480 30 fps) + USB Logi C270 = entrance; phone offline (IP Webcam app stopped); AI in its own process; 1 dashboard open - 05 Nov 2026 19:56 (100 s)

Runtime: PyTorch (ultralytics) + shared batched engine; GPU engine: {'batch_ms_p50': 60.5, 'batch_ms_p95': 81.5, 'wait_ms_p50': 15.0, 'wait_ms_p95': 54.2, 'batch_size_avg': 1.04, 'gpu_busy_share': 0.66, 'process': True}

| Camera | status | processed fps (median) | capture -> processed p50 | p95 | model p50 | wait for GPU p50 | stale frames skipped |
|---|---|---|---|---|---|---|---|
| cam_01 | ONLINE | 6.9 | 99 ms | 136 ms | 63 ms | 13 ms | 0 |
| cam_02 | OFFLINE | 0 | - | - | - | - | 0 |
| cam_04 | ONLINE | 4.0 | 135 ms | 223 ms | 23 ms | 21 ms | 0 |

| Measurement | n | median | 95th percentile | max |
|---|---|---|---|---|
| frame captured -> event stored (backend) | 2 | 263 ms | 280 ms | 281 ms |
| frame captured -> event on the dashboard | 2 | 282 ms | 306 ms | 309 ms |
| live picture age on the dashboard | 1731 | 39 ms | 92 ms | 186 ms |
| live frame edge -> backend | 1925 | 36 ms | 90 ms | 181 ms |
| entrance: person seen -> APPROVED/DENIED/MANUAL | 2 | 5.91 s | 6.45 s | 6.51 s |

## 5 Nov 2026 evening - live video after live_fps 15 + decode-before-swap (real cameras)

Measured by a client on /ws/video for 20 s, all three cameras at once (edge AI running, 3 cameras):

| Camera | Pictures / s | Gap p50 | Gap p95 | Longest gap | Picture age p50 | p95 |
|---|---|---|---|---|---|---|
| cam_01 laptop (welding bay) | 14.9 | 67 ms | 79 ms | 98 ms | 32 ms | 54 ms |
| cam_02 phone (warehouse) | 13.5 | 69 ms | 134 ms | 157 ms | 42 ms | 78 ms |
| cam_04 USB C270 (entrance) | 7.4 | 132 ms | 206 ms | 272 ms | 31 ms | 87 ms |

Picture age = edge capture -> received by the client on the same computer (the phone's own encode delay before the edge is not
included). No freeze longer than 0.3 s. USB rate is the camera's own limit in this light. AI: 6-10 / 4 / 4 checks per second.
Re-violation (PPE fixed, then taken off again): alert 1.4 s after it comes off (state-machine replay, edge.selftest).

## 6 Nov 2026 - four cameras (laptop, two phones, USB), after "send frames as they arrive", box motion prediction and the USB MJPG fix

Server stream (/ws/video client, 30 s, all four cameras at once):

| Camera | Pictures / s | Gap p95 | Longest gap | Picture age p50 / p95 |
|---|---|---|---|---|
| cam_01 laptop | 19.0 | 66 ms | 119 ms | 31 / 53 ms |
| cam_02 phone 192.168.60.53 | 14.6 | 111 ms | 166 ms | 18 / 48 ms |
| cam_03 phone 192.168.60.58 | 14.7 | 108 ms | 156 ms | 19 / 48 ms |
| cam_04 USB C270 (now MJPG 29.8 fps) | 19.0 | 63 ms | 97 ms | 31 / 53 ms |

Control room in a headless browser (before the USB fix): 19 / 14.8 / 14.5 / 7.5 pictures/s on screen, no gap over 250 ms,
picture age 21-36 ms. AI 4-7 checks/s per camera. Picture age = capture at the edge -> shown; the phones' own encode delay
before the edge is not included. The headless test browser renders without a GPU and occasionally pauses itself (0.7 s).

## 13 Nov Axis via relay + laptop webcam (2 live cameras) - 13 Nov 2026 03:18 (60 s)

Runtime: PyTorch (ultralytics) + shared batched engine; GPU engine: {'batch_ms_p50': 26.2, 'batch_ms_p95': 83.2, 'wait_ms_p50': 10.5, 'wait_ms_p95': 55.4, 'batch_size_avg': 1.21, 'gpu_busy_share': 0.51, 'process': True}

| Camera | status | processed fps (median) | capture -> processed p50 | p95 | model p50 | wait for GPU p50 | stale frames skipped |
|---|---|---|---|---|---|---|---|
| cam_01 | ONLINE | 9.1 | 84 ms | 170 ms | 26 ms | 12 ms | 0 |
| cam_02 | ONLINE | 4.0 | 152 ms | 474 ms | 74 ms | 12 ms | 0 |
| cam_03 | OFFLINE | 0 | - | - | - | - | 0 |
| cam_04 | OFFLINE | 0 | - | - | - | - | 0 |

| Measurement | n | median | 95th percentile | max |
|---|---|---|---|---|
| live frame edge -> backend | 1515 | 53 ms | 136 ms | 286 ms |
