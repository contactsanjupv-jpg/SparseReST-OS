"""Manage the encrypted secrets store.
    python -m scripts.secrets_cli gen-key          # prints a new master key
    python -m scripts.secrets_cli set smtp_password   # value read from a prompt
    python -m scripts.secrets_cli list | delete NAME | rotate
Then reference secrets in .env as e.g. SMTP_PASSWORD=secret://smtp_password
"""
from __future__ import annotations

import argparse
import getpass
import sys

from packages.core.config import Settings
from packages.core.secrets import SecretsError, SecretStore, generate_master_key


def main(argv=None, *, value_reader=getpass.getpass, settings=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-key")
    sub.add_parser("list")
    sub.add_parser("rotate")
    s = sub.add_parser("set"); s.add_argument("name")
    d = sub.add_parser("delete"); d.add_argument("name")
    args = p.parse_args(argv)

    if args.cmd == "gen-key":
        print(generate_master_key())
        return 0
    try:
        # Raw Settings(), not get_settings(): references must not be resolved just to manage the store itself.
        store = SecretStore.from_settings(settings or Settings())
        if args.cmd == "set":
            print(f"Stored '{args.name}' (version {store.set(args.name, value_reader('Value: '))}).")
        elif args.cmd == "list":
            for row in store.list_names():
                print(f"{row['name']}  v{row['version']}  {row['updated_at']}")
        elif args.cmd == "delete":
            print("Deleted." if store.delete(args.name) else "No such secret.")
        elif args.cmd == "rotate":
            print(f"Re-encrypted {store.rotate_all()} secret(s) under the primary key.")
        return 0
    except SecretsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
