"""Set up and test the FIRE / SMOKE phone call (11 Nov).

    python -m tools.phone_call_setup                 # what is configured, is adb there, which phone is connected
    python -m tools.phone_call_setup --connect 192.168.60.70:5555   # Android phone over Wi-Fi (adb tcpip / wireless debugging)
    python -m tools.phone_call_setup --test          # REALLY rings everybody in call.to once (asks first)

Normal SIM call = an Android phone with a SIM, connected to this laptop:
  1. On the phone: Settings > About phone > tap "Build number" 7 times; Developer options > USB debugging ON
     (and "Stay awake" ON so the screen does not lock while charging).
  2. Connect it by USB, tap "Allow" on the phone (always allow from this computer).
  3. Android platform-tools: unzip https://dl.google.com/android/repository/platform-tools-latest-windows.zip into
     tools/  (so tools/platform-tools/adb.exe exists)  - or set call.adb_path in config/secrets.local.yaml.
  4. config/secrets.local.yaml ->  call: {to: ["+9198xxxxxxxx"]}  (the supervisor's number; several allowed)
If the phone is not connected, NETRAX falls back to a CallMeBot Telegram voice call (config/notify.yaml phone.provider).
"""
import argparse
import subprocess
import sys

from backend import notify


def clean_number(t):
    """+91 number from what a person types (spaces / dashes / leading 0 allowed); @telegram names pass through."""
    t = t.strip()
    if t.startswith("@"):
        return t
    d = "".join(ch for ch in t if ch.isdigit())
    if t.startswith("+") and 8 <= len(d) <= 15:
        return "+" + d
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10:
        return "+91" + d
    if len(d) == 12 and d.startswith("91"):
        return "+" + d
    return None


def save_numbers(text, path=None):
    """Writes call.to in config/secrets.local.yaml (only that one line; everything else untouched)."""
    import os
    import re
    nums = [clean_number(x) for x in text.split(",") if x.strip()]
    if not nums or None in nums:
        sys.exit("Not saved: write the number like +919812345678 (10 digits after +91).")
    path = path or os.environ.get("SENTINELEYE_SECRETS") or os.path.join(notify.ROOT, "config", "secrets.local.yaml")
    s = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    line = "  to: [" + ", ".join('"%s"' % n for n in nums) + "]"
    m = re.search(r"(?m)^call:[^\n]*\n(?:[ \t]+[^\n]*\n?|[ \t]*\n)*", s)
    if m:
        blk = m.group(0)
        new = re.sub(r"(?m)^[ \t]+to:[^\n]*", lambda _: line, blk, count=1) if re.search(r"(?m)^[ \t]+to:", blk) \
            else blk.rstrip("\n") + "\n" + line + "\n"
        s = s[:m.start()] + new + s[m.end():]
    else:
        s = s.rstrip("\n") + "\n\ncall:\n" + line + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print("Saved who is called:", ", ".join(n[:3] + "*" * (len(n) - 7) + n[-4:] for n in nums))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connect", help="adb over Wi-Fi: phone address, e.g. 192.168.60.70:5555")
    ap.add_argument("--test", action="store_true", help="place one real test call to every number in call.to")
    ap.add_argument("--to", help="save who is called, e.g. +919812345678 (several: comma-separated)")
    a = ap.parse_args()
    if a.to:
        save_numbers(a.to)
    pol, sec = notify.load_policy(), notify.load_secrets()
    provs = notify.call_providers(pol)
    targets = notify.call_targets(sec, pol)
    print("Call order        :", " -> ".join(provs))
    print("Who is called     :", ", ".join(targets) or "NOBODY - add call.to in config/secrets.local.yaml")
    adb = notify.adb_exe(sec)
    print("adb               :", adb or "NOT FOUND - unzip platform-tools into tools/ (see the top of this file)")
    if adb:
        if a.connect:
            print(subprocess.run([adb, "connect", a.connect], capture_output=True, text=True, timeout=15).stdout.strip())
        out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, timeout=15).stdout.strip()
        phones = [l for l in out.splitlines()[1:] if l.strip()]
        print("Phones            :", "\n                    ".join(phones) or "none - connect the phone by USB and tap Allow")
        if any(" unauthorized" in l for l in phones):
            print("                    -> tap 'Allow USB debugging' on the phone")
    print("Channel ready     :", "YES" if notify.configured(sec)["call"] else "NO")
    if a.test:
        if not targets:
            sys.exit("Nobody to call.")
        if input(f"Really call {', '.join(targets)} now? (yes/no) ").strip().lower() != "yes":
            sys.exit("Not called.")
        text = "NETRAX test call. This is only a test of the fire alarm phone call. No action needed."
        for t in targets:
            r = notify.send_call(sec, t, text, pol)
            print(f"{t}: {r.status} - {r.provider_status or r.error}")


if __name__ == "__main__":
    main()
