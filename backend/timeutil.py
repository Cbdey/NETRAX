"""One time zone for the whole system (messages, dashboard, AI assistant).

zoneinfo needs the 'tzdata' package on Windows; without it we fall back to a fixed offset,
which is exact for India (IST has no daylight saving)."""
from datetime import datetime, timedelta, timezone

FALLBACK = {"Asia/Kolkata": (5, 30), "Asia/Calcutta": (5, 30), "UTC": (0, 0)}


def get_tz(name="Asia/Kolkata"):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # noqa: BLE001 - missing tzdata on Windows
        h, m = FALLBACK.get(name, (5, 30))
        return timezone(timedelta(hours=h, minutes=m), "IST" if (h, m) == (5, 30) else name)


TZ = get_tz()


def set_tz(name):
    global TZ
    TZ = get_tz(name or "Asia/Kolkata")


def local(ts):
    return datetime.fromtimestamp(ts, tz=TZ)


def fmt(ts, with_date=True):
    if ts is None:
        return "-"
    d = local(ts)
    s = d.strftime("%d %b %Y, %H:%M:%S") if with_date else d.strftime("%H:%M:%S")
    return f"{s} {tz_label()}"


def tz_label():
    n = datetime.now(TZ).tzname() or "IST"
    return "IST" if n in ("+0530", "UTC+05:30") else n
