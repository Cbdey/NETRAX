"""Keep a copy of the NETRAX window output in data/ (13 Nov, user: "camera glitches / cam off while running").

run_all.bat shows the edge and backend output only in their console windows - after a problem nothing was left to
read. start("edge_live.log") copies everything printed (stdout + stderr) into data/<name> with the time on every
line. Only when the output is a real console window (run_all.bat): tests and redirected runs are untouched.
A log over keep_mb is moved to <name>.old at start-up. NETRAX_NO_LIVELOG=1 turns it off."""
import os
import sys
import time

from .config import path


class _Tee:
    def __init__(self, stream, f):
        self.s, self.f, self.bol = stream, f, True

    def write(self, x):
        try:
            self.s.write(x)
        except Exception:                  # noqa: BLE001 - the console must never break NETRAX
            pass
        try:
            out = []
            for part in x.splitlines(keepends=True):
                if self.bol:
                    out.append(time.strftime("%H:%M:%S "))
                out.append(part)
                self.bol = part.endswith("\n")
            self.f.write("".join(out))
            self.f.flush()
        except Exception:                  # noqa: BLE001
            pass
        return len(x)

    def flush(self):
        try:
            self.s.flush()
        except Exception:                  # noqa: BLE001
            pass

    def __getattr__(self, name):
        return getattr(self.s, name)


def start(name, keep_mb=20, force=False):
    if os.environ.get("NETRAX_NO_LIVELOG") or isinstance(sys.stdout, _Tee):
        return None
    try:
        console = sys.stdout.isatty()
    except Exception:                      # noqa: BLE001
        console = False
    if not (console or force):
        return None
    p = path("data", name)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > keep_mb * 1024 * 1024:
            os.replace(p, p + ".old")
        f = open(p, "a", encoding="utf-8", errors="replace")
        f.write(f"\n===== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    except OSError:
        return None
    sys.stdout, sys.stderr = _Tee(sys.stdout, f), _Tee(sys.stderr, f)
    return p
