"""Data minimisation (plan section 14): delete alert snapshots older than N days.
The alert records stay (reports / analytics need them); only the images are removed.
Registration photos of workers are NOT touched - they are deleted when the worker is removed."""
import glob
import os
import time


def purge(folder, days, now=None):
    """Delete *.jpg in folder older than `days`. Returns the deleted event ids (file stems)."""
    if not days or days <= 0 or not os.path.isdir(folder):
        return []
    cutoff = (now or time.time()) - days * 86400
    gone = []
    for f in glob.glob(os.path.join(folder, "*.jpg")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                gone.append(os.path.splitext(os.path.basename(f))[0])
        except OSError:
            pass
    return gone
