"""NETRAX on the shared Wi-Fi (11 Nov): which address do teammates open, which address do the ESP32 boards send to?

    python -m tools.lan_setup            # show the links + check Wi-Fi, firewall, backend
    python -m tools.lan_setup --write    # ALSO put this laptop's address into both ESP32 secrets.h files
    python -m tools.lan_setup --brief    # one line (used by run_all.bat)

Everybody (laptops, phones, ESP32 boards) must be on the same Wi-Fi (team decision: "NOTHING"). The laptop's
address changes when it joins another Wi-Fi - run this after connecting, then re-flash the boards if it changed.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8000
SECRETS = {   # file -> the define that holds the laptop address
    os.path.join("firmware", "rfid_direct", "secrets.h"): "SECRET_SERVER",
    os.path.join("firmware", "netrax_station_team", "secrets.h"): "SECRET_NETRAX_HOST",
}
PAGES = [("Control room", "/"), ("Safety Intelligence", "/intelligence"), ("Hardware / sensors", "/hardware"),
         ("Entrance", "/entrance"), ("Admin", "/admin")]


def _ps(cmd):
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:                                   # noqa: BLE001
        return ""


def wifi():
    """(ssid, ipv4, network category) of the Wi-Fi adapter."""
    ssid = ""
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        ssid = m.group(1).strip() if m else ""
    except Exception:                                   # noqa: BLE001
        pass
    ip = ""
    raw = _ps("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match 'Wi-?Fi|WLAN' -and "
              "$_.IPAddress -notmatch '^169\\.254' } | Select-Object -First 1 -ExpandProperty IPAddress")
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", raw or ""):
        ip = raw
    if not ip:                                          # no packet is sent: only picks the outgoing interface
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = ""
    cat = _ps("(Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -match 'Wi-?Fi|WLAN' } | "
              "Select-Object -First 1).NetworkCategory")
    return ssid, ip, cat


def firewall_profiles():
    """Profiles in which an inbound 'allow' rule covers this Python (or port 8000)."""
    # only the candidate rules (all rules with their filters took > 20 s)
    raw = _ps("$r = @(Get-NetFirewallApplicationFilter | Where-Object { $_.Program -match 'python' } | Get-NetFirewallRule) + "
              "@(Get-NetFirewallPortFilter -Protocol TCP | Where-Object { $_.LocalPort -eq '8000' } | Get-NetFirewallRule); "
              "$r | Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' } | "
              "ForEach-Object { $p = $_ | Get-NetFirewallPortFilter; "
              "[pscustomobject]@{prof=[string]$_.Profile; proto=[string]$p.Protocol} } | ConvertTo-Json -Compress")
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        return set()
    rows = rows if isinstance(rows, list) else [rows]
    prof = set()
    for r in rows:
        if r.get("proto") not in ("TCP", "Any", None, ""):
            continue
        p = r.get("prof") or ""
        prof |= {"Public", "Private", "Domain"} if p in ("Any", "") else set(x.strip() for x in p.split(","))
    return prof


def backend_answers(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:{PORT}/login", timeout=3) as r:
            return r.status == 200
    except Exception:                                   # noqa: BLE001
        return False


def write_secrets(base):
    done = []
    for rel, define in SECRETS.items():
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            done.append(f"{rel}: not found (copy secrets.example.h -> secrets.h first)")
            continue
        s = open(p, encoding="utf-8").read()
        line = f'#define {define} "{base}"'
        if re.search(rf"^#define\s+{define}\b.*$", s, re.M):
            s2 = re.sub(rf"^#define\s+{define}\b.*$", line, s, count=1, flags=re.M)
        else:
            s2 = s.rstrip("\n") + "\n" + line + "\n"
        if s2 != s:
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(s2)
            done.append(f"{rel}: {define} = {base}  (CHANGED - flash this board again)")
        else:
            done.append(f"{rel}: already {base}")
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write this laptop's address into the ESP32 secrets.h files")
    ap.add_argument("--brief", action="store_true", help="one line with the link to share")
    ap.add_argument("--ssid", default="NOTHING", help="the Wi-Fi everybody should use (default NOTHING)")
    a = ap.parse_args()
    ssid, ip, cat = wifi()
    base = f"http://{ip}:{PORT}" if ip else None
    if a.brief:
        if base:
            print(f"Share with the team (same Wi-Fi '{ssid or '?'}'): {base}   - this laptop: http://127.0.0.1:{PORT}")
        else:
            print("No Wi-Fi address found - connect to the Wi-Fi first (python -m tools.lan_setup)")
        return
    print(f"Wi-Fi             : {ssid or 'NOT CONNECTED'}" + ("" if not ssid or ssid == a.ssid else
                                                             f"   <-- the team uses '{a.ssid}': connect this laptop to it"))
    print(f"Laptop address    : {ip or '-'}   (network type: {cat or '?'})")
    if not base:
        return
    print("\nOpen on any laptop / phone on the same Wi-Fi (login needed):")
    for name, path in PAGES:
        print(f"  {name:20s} {base}{path}")
    print(f"  (on THIS laptop use http://127.0.0.1:{PORT} - voice commands / browser GPS only work there)")
    print("\nESP32 boards send to:")
    print(f"  safety station    {base}/api/hw/telemetry   (+ GPS {base}/api/location/hardware)")
    print(f"  RFID reader       {base}/api/rfid")
    prof = firewall_profiles()
    if cat and cat not in prof:
        print(f"\nFIREWALL: no inbound rule for Python / port {PORT} on '{cat}' networks - other devices are BLOCKED.")
        print("  Fix (one time, as administrator):  netsh advfirewall firewall add rule name=\"NETRAX 8000\" "
              f"dir=in action=allow protocol=TCP localport={PORT} profile=any")
    else:
        print(f"\nFirewall          : Python allowed on {', '.join(sorted(prof)) or '?'} networks - ok")
    print(f"Backend           : {'RUNNING - reachable at ' + base if backend_answers(ip) else 'not running (start run_all.bat)'}")
    if a.write:
        print("\nESP32 secrets.h:")
        for line in write_secrets(base):
            print("  " + line)
    else:
        print("\nTo point the ESP32 boards here:  python -m tools.lan_setup --write   (then flash both boards)")


if __name__ == "__main__":
    main()
