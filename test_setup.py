import cv2, supervision, fastapi, yaml, onnxruntime
from ultralytics import YOLO

print("OpenCV:", cv2.__version__, "| Supervision:", supervision.__version__)

m = yaml.safe_load(open("config/settings.yaml"))["models"]   # always the models the live system uses
ppe = YOLO(m["ppe"])
fire = YOLO(m["fire"])
aux = YOLO(m["ppe_aux"])
shoe = YOLO(m["shoe_cls"])
print("PPE main  :", m["ppe"], ppe.names)
print("PPE aux   :", m["ppe_aux"], aux.names)
print("Fire      :", m["fire"], fire.names)
print("Shoe cls  :", m["shoe_cls"], shoe.names)

import os, time
# Same way the live system opens a webcam (edge/stream.py): DirectShow on Windows; the default (MSMF) often fails.
# If it still fails: stop run_all.bat / the edge first - only one program can use the webcam at a time.
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(0)
ok, frame = False, None
for _ in range(20):                      # the first frames are often empty while the camera wakes up
    ok, frame = cap.read()
    if ok and frame is not None:
        break
    time.sleep(0.1)
cap.release()
print("Webcam working:", ok, "" if ok else "(is run_all.bat / the edge already using it? close it and retry)")

if ok:
    r = ppe(frame, verbose=False)[0]
    found = [ppe.names[int(c)] for c in r.boxes.cls]
    print("PPE detections on your webcam frame:", found)
    cv2.imwrite("data/snapshots/test_frame.jpg", r.plot())
    print("Annotated frame saved to data/snapshots/test_frame.jpg")