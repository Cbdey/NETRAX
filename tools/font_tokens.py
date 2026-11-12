"""12 Nov polish: replace hard-coded px font sizes in dashboard/ with the design-system tokens (nothing below 13 px).
  < 12 px -> --fs-xs 13 | 12-14 -> --fs-s 14 | 14.5-17 -> --fs-m 16 | 17.5-21 -> --fs-l 19 | 21.5-27 -> --fs-xl 24 | 28-39 -> --fs-num 34
Each token has a px fallback (var(--fs-s,14px)) so a page without nx.css still works. Run again = no change.
usage: python -m tools.font_tokens [--dry]"""
import glob
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")


def token(v):
    for lim, name, px in ((12, "xs", 13), (14.5, "s", 14), (17.5, "m", 16), (21.5, "l", 19), (28, "xl", 24), (40, "num", 34)):
        if v < lim:
            return f"var(--fs-{name},{px}px)"
    return None


def fix(text):
    n = 0

    def rep_size(m):
        nonlocal n
        t = token(float(m.group(2)))
        if not t:
            return m.group(0)
        n += 1
        return m.group(1) + t

    def rep_short(m):
        nonlocal n
        t = token(float(m.group(3)))
        if not t:
            return m.group(0)
        n += 1
        return f"{m.group(1)}{m.group(2)} {t}"
    text = re.sub(r"(font-size:\s*)(\d+(?:\.\d+)?)px", rep_size, text)
    text = re.sub(r"(font:\s*)((?:italic\s+)?\d{3})\s+(\d+(?:\.\d+)?)px", rep_short, text)
    return text, n


def main():
    dry = "--dry" in sys.argv
    total = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "*.html")) + glob.glob(os.path.join(ROOT, "*.js")) + glob.glob(os.path.join(ROOT, "*.css"))):
        src = open(p, encoding="utf-8").read()
        out, n = fix(src)
        if n:
            total += n
            print(f"{os.path.basename(p)}: {n}")
            if not dry:
                open(p, "w", encoding="utf-8", newline="").write(out)
    print("total", total, "(dry run)" if dry else "")


if __name__ == "__main__":
    main()
