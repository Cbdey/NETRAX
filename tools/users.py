"""Dashboard users (backend/auth.py).
    python -m tools.users list
    python -m tools.users set <name> <password> [viewer|supervisor|admin]
    python -m tools.users delete <name>
Passwords are stored as PBKDF2 hashes in config/users.local.yaml (never pushed)."""
import sys

from backend import auth


def main(argv):
    if not argv or argv[0] == "list":
        users = auth.load_users()
        if not users:
            print("no users yet - they are created on the first backend start (data/initial_passwords.txt)")
        for n, u in sorted(users.items()):
            print(f"{n:<16} {u['role']}")
        return 0
    if argv[0] == "set" and len(argv) >= 3:
        auth.set_user(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        print(f"saved {argv[1]} ({auth.load_users()[argv[1]]['role']})")
        return 0
    if argv[0] == "delete" and len(argv) == 2:
        users = auth.load_users()
        users.pop(argv[1], None)
        auth.save_users(users)
        print(f"deleted {argv[1]} (their sessions stop working)")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
