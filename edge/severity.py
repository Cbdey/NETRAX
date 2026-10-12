"""Severity engine (F07, plan section 7.3)."""
LEVELS = ["Low", "Medium", "High", "Critical"]


def _bump(level, steps=1, cap="High"):
    i = min(LEVELS.index(level) + steps, LEVELS.index(cap))
    return LEVELS[i]


def ppe_severity(items_missing, zone_risk, repeated=False):
    """Same missing glove: Medium in the warehouse, High in the welding bay."""
    if zone_risk == "high":
        base = "High"
    elif zone_risk == "medium":
        base = "High" if "helmet" in items_missing else "Medium"
    else:
        base = "Medium" if "helmet" in items_missing else "Low"
    if len(items_missing) >= 2:
        base = _bump(base)
    if repeated:
        base = _bump(base)
    return base          # PPE never reaches Critical; Critical is reserved for fire/smoke


def fire_severity(growing, persistent):
    return "Critical" if (growing or persistent) else "High"
