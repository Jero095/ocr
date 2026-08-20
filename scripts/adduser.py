"""Create, list, disable or re-password accounts.

There is no signup form on purpose - this is a small known team, so a public
registration route would be attack surface with nobody to use it. Accounts are
created here instead.

    python scripts/adduser.py list
    python scripts/adduser.py add jeronimo@fignow.com --name "Jeronimo" --admin
    python scripts/adduser.py passwd m.castro@fignow.com
    python scripts/adduser.py disable m.castro@fignow.com

The password is prompted for, never taken as an argument: a command-line password
ends up in the shell history file.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store  # noqa: E402

MIN_LENGTH = 10


def _prompt_password() -> str:
    while True:
        first = getpass.getpass("Password: ")
        if len(first) < MIN_LENGTH:
            print(f"  Too short - use at least {MIN_LENGTH} characters.")
            continue
        if first != getpass.getpass("Repeat: "):
            print("  They do not match.")
            continue
        return first


def cmd_list() -> None:
    users = store.list_users()
    if not users:
        print("No accounts yet. Create one with: python scripts/adduser.py add <email>")
        return
    print(f"{'id':>3}  {'email':32} {'name':16} {'role':6} {'status':8} created")
    print("-" * 92)
    for u in users:
        print(
            f"{u['id']:>3}  {u['email']:32} {u['display_name'][:16]:16} "
            f"{'admin' if u['is_admin'] else 'member':6} "
            f"{'disabled' if u['disabled'] else 'active':8} {u['created_at'][:19]}"
        )


def cmd_add(email: str, name: str, admin: bool) -> None:
    try:
        uid = store.create_user(email, _prompt_password(), name, admin)
    except sqlite3.IntegrityError:
        print(f"{email} already has an account. Use 'passwd' to reset it.")
        raise SystemExit(1)
    print(f"Created {email} (id {uid}){' as admin' if admin else ''}.")


def cmd_passwd(email: str) -> None:
    if not store.set_password(email, _prompt_password()):
        print(f"No account for {email}.")
        raise SystemExit(1)
    print(f"Password updated for {email}. Existing sessions stay valid -")
    print("run 'disable' then 'enable' if you need to force a re-login.")


def _set_disabled(email: str, disabled: bool) -> None:
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE users SET disabled = ? WHERE email = ?",
            (1 if disabled else 0, email.strip().lower()),
        )
        if disabled:
            # Sessions are checked against users.disabled on every request, but
            # deleting them makes the revocation explicit rather than implicit.
            conn.execute(
                "DELETE FROM sessions WHERE user_id ="
                " (SELECT id FROM users WHERE email = ?)",
                (email.strip().lower(),),
            )
    if cur.rowcount == 0:
        print(f"No account for {email}.")
        raise SystemExit(1)
    print(f"{email} {'disabled - sessions revoked' if disabled else 'enabled'}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show all accounts")

    add = sub.add_parser("add", help="create an account")
    add.add_argument("email")
    add.add_argument("--name", default="", help="display name shown in history")
    add.add_argument("--admin", action="store_true")

    for name in ("passwd", "disable", "enable"):
        p = sub.add_parser(name)
        p.add_argument("email")

    args = parser.parse_args()
    store.init()

    if args.command == "list":
        cmd_list()
    elif args.command == "add":
        cmd_add(args.email, args.name, args.admin)
    elif args.command == "passwd":
        cmd_passwd(args.email)
    elif args.command == "disable":
        _set_disabled(args.email, True)
    elif args.command == "enable":
        _set_disabled(args.email, False)


if __name__ == "__main__":
    main()
