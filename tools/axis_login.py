"""Store the Axis camera login in config/secrets.local.yaml (git-ignored) without showing it (13 Nov).

    python -m tools.axis_login            asks for user + password (password is not echoed), saves, then tests the camera

Only the `cameras: axis:` block is changed; everything else in the file stays as it is."""
import getpass
import re
import subprocess
import sys

import yaml

from edge.config import path


def save(user, password, file=None):
    p = file or path("config", "secrets.local.yaml")
    try:
        txt = open(p, encoding="utf-8").read()
    except FileNotFoundError:
        txt = ""
    q = lambda v: yaml.safe_dump(v, default_style='"').strip().split("\n")[0]       # noqa: E731 - quoted YAML scalar
    block = ("cameras:                   # Axis P1447-LE login (rtsp://{axis}@... in cameras.yaml)\n"
             f"  axis:\n    user: {q(user)}\n    password: {q(password)}\n")
    if re.search(r"^cameras:", txt, re.M):
        txt = re.sub(r"^cameras:.*\n(?:[ #].*\n|\n)*", block, txt, count=1, flags=re.M)
    else:
        txt = txt + ("" if txt.endswith("\n") or not txt else "\n") + block
    data = yaml.safe_load(txt) or {}
    assert data["cameras"]["axis"] == {"user": user, "password": password}, "could not write the block"
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)


def main():
    user = input("Axis user name (made on the camera's web page): ").strip()
    pw = getpass.getpass("Axis password (not shown): ")
    if not user or not pw:
        print("nothing saved (user and password are both needed)")
        return 1
    save(user, pw)
    print("saved in config/secrets.local.yaml (git-ignored). Testing the camera ...\n")
    return subprocess.call([sys.executable, "-m", "tools.rtsp_test", "cam_01"])


if __name__ == "__main__":
    raise SystemExit(main())
