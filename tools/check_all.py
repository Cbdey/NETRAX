"""One command for every check Claude needs (run in the venv, dashboard/edge STOPPED so the GPU is free):

    python -m tools.check_all

Runs: logic selftest, hardware selftest, helmet/vest/mask v2 vs v3B (test + val scenes),
fire models on the hard test, and the speed benchmark of the live model set.
Everything is written to docs/eval/check_all.log (and the usual docs/eval/*.md, docs/benchmark.json).
Nothing in the configuration is changed.
"""
import os
import subprocess
import sys
import time

from edge.config import path

STEPS = [
    ("logic selftest", ["-m", "edge.selftest"]),
    ("hardware selftest", ["-m", "tools.hw_selftest"]),
    ("helmet/vest/mask: v2 vs v3B (hard test scenes)", ["-m", "tools.compare_ppe_main", "--split", "test"]),
    ("helmet/vest/mask: v2 vs v3B (val scenes)", ["-m", "tools.compare_ppe_main", "--split", "val"]),
    ("fire models on the hard test", ["-m", "tools.eval_hardtest", "--task", "fire", "--models",
                                      "models/fire_smoke_v2A.pt", "models/fire_smoke_v2B.pt"]),
    ("speed of the live model set", ["-m", "tools.benchmark", "--runs", "60"]),
]


def main():
    log = path("docs", "eval", "check_all.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"check_all started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    for i, (name, args) in enumerate(STEPS, 1):
        print(f"\n===== [{i}/{len(STEPS)}] {name} =====", flush=True)
        t = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")       # Windows pipes are cp1252 otherwise
        p = subprocess.run([sys.executable] + args, cwd=path(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        out = (p.stdout or "") + ("\n--- stderr ---\n" + p.stderr[-4000:] if p.returncode else "")
        tail = "\n".join(out.strip().splitlines()[-25:])
        print(tail)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n===== [{i}/{len(STEPS)}] {name} | exit {p.returncode} | {time.time() - t:.0f} s =====\n{out}\n")
    print(f"\nDONE -> {log}  (tell Claude it is finished)")


if __name__ == "__main__":
    main()
