"""Create / reset a founder login. Password is read from a prompt (never a CLI argument, which lands in shell history).
    python -m scripts.create_founder you@example.com
    python -m scripts.create_founder you@example.com --reset-password
    python -m scripts.create_founder you@example.com --unlock
"""
from __future__ import annotations

import argparse
import getpass
import sys

from apps.api.auth_service import AuthService
from apps.api.security import WeakPasswordError


def main(argv=None, *, password_reader=getpass.getpass) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("email")
    p.add_argument("--reset-password", action="store_true")
    p.add_argument("--unlock", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.unlock:
            AuthService.unlock(args.email)
            print("Unlocked.")
            return 0
        pw = password_reader("Password (min 12 chars): ")
        if pw != password_reader("Repeat: "):
            print("Passwords differ.", file=sys.stderr)
            return 1
        (AuthService.set_password if args.reset_password else AuthService.create_user)(args.email, pw)
        print("Password reset; all sessions revoked." if args.reset_password else "Founder account created.")
        return 0
    except (ValueError, WeakPasswordError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
