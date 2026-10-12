"""Loads settings, zones and cameras from config/*.yaml.

Test / soak-run overrides (never needed in normal use):
  SENTINELEYE_CAMERAS=<file>     use another camera registry (e.g. data/loadtest/cameras.yaml)
  SENTINELEYE_DATA_DIR=<dir>     write outbox, snapshots, fire log and gallery cache there instead of data/
  SENTINELEYE_BACKEND=<url>      talk to another backend (e.g. a scratch one on port 8765)
Cameras with `enabled: false` are skipped by the edge and the dashboard."""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path(*parts):
    return os.path.join(ROOT, *parts)


def load_yaml(name):
    with open(path("config", name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_cameras():
    alt = os.environ.get("SENTINELEYE_CAMERAS")
    if alt:
        with open(alt if os.path.isabs(alt) else path(alt), "r", encoding="utf-8") as f:
            cams = (yaml.safe_load(f) or {})["cameras"]
    else:
        cams = load_yaml("cameras.yaml")["cameras"]
    return {k: v for k, v in cams.items() if v.get("enabled", True)}


def _redirect(p, data_dir):
    """data/xyz -> <data_dir>/xyz"""
    if not p:
        return p
    parts = p.replace("\\", "/").split("/")
    return os.path.join(data_dir, *parts[1:]) if parts[0] == "data" else p


def load_all():
    settings = load_yaml("settings.yaml")
    zones = load_yaml("zones.yaml")["zones"]
    cameras = load_cameras()
    settings.setdefault("identity", {}).setdefault("cache", "data/gallery_cache.json")
    data_dir = os.environ.get("SENTINELEYE_DATA_DIR")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        for key in ("snapshots_dir", "outbox_db"):
            settings[key] = _redirect(settings[key], data_dir)
        settings["fire"]["debug_log"] = _redirect(settings["fire"].get("debug_log"), data_dir)
        settings["identity"]["cache"] = _redirect(settings["identity"]["cache"], data_dir)
    if os.environ.get("SENTINELEYE_BACKEND"):
        settings["backend"]["url"] = os.environ["SENTINELEYE_BACKEND"]
    for cam_id, cam in cameras.items():
        if cam.get("default_zone") not in zones:
            raise ValueError(f"{cam_id}: default_zone '{cam.get('default_zone')}' not in zones.yaml")
        for z in cam.get("zones") or []:
            if z["zone"] not in zones:
                raise ValueError(f"{cam_id}: zone '{z['zone']}' not in zones.yaml")
    return settings, zones, cameras
