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
