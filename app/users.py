"""Hand the data from before accounts existed to its owner (one time, from the server).

    python -m app.users claim-existing

On the server:  docker compose run --rm food-service python -m app.users claim-existing

Create your account in the app first (/signup). The command asks for that account's username
and password — proving it's yours — and moves the old plans, ratings, single-household
preferences + stores and LLM cost history to it. It works once per database.

There are deliberately no other admin commands: accounts are self-service, usernames aren't
stored, and there's no password reset, so nobody running the server can take over an account.
"""

import argparse
import getpass
import sys

import psycopg

from app.auth import authenticate, parse_username_key
from app.config import load_config
from app.db import connect


def claim_existing(conn: psycopg.Connection, user_id: int) -> dict[str, int]:
    """Give every unowned row to `user_id`; returns how many rows of each kind moved.
    Allowed once per database: the old single-household preferences can only belong to one
    person."""
    with conn.transaction():
        first = conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('legacy_data_claimed_by', %s) "
            "ON CONFLICT (key) DO NOTHING", (str(user_id),)).rowcount
        if not first:
            raise ValueError("existing data was already claimed")
        plans = conn.execute("UPDATE plans SET user_id = %s WHERE user_id IS NULL", (user_id,)).rowcount
        ratings = conn.execute("UPDATE ratings SET user_id = %s WHERE user_id IS NULL",
                               (user_id,)).rowcount
        prefs = conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, max_kcal, min_protein_g, restrictions, servings, bans, language) "
            "SELECT %s, max_kcal, min_protein_g, restrictions, servings, bans, language "
            "FROM preferences WHERE id = 1 "
            "ON CONFLICT (user_id) DO UPDATE SET max_kcal = excluded.max_kcal, "
            "min_protein_g = excluded.min_protein_g, restrictions = excluded.restrictions, "
            "servings = excluded.servings, bans = excluded.bans, language = excluded.language",
            (user_id,)).rowcount
        stores = conn.execute(
            "INSERT INTO user_stores (user_id, chain_slug) SELECT %s, chain_slug FROM stores "
            "ON CONFLICT DO NOTHING", (user_id,)).rowcount
        calls = conn.execute("UPDATE llm_calls SET tenant_id = %s WHERE tenant_id = 'me'",
                             (str(user_id),)).rowcount
    return {"plans": plans, "ratings": ratings, "preferences": prefs, "stores": stores,
            "llm_calls": calls}


def _credentials(from_stdin: bool) -> tuple[str, str]:
    if from_stdin:  # scripted use: two lines, username then password
        return sys.stdin.readline().rstrip("\n"), sys.stdin.readline().rstrip("\n")
    return input("Your username: "), getpass.getpass("Your password: ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.users", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim-existing", help="move pre-accounts data to your account (once)")
    c.add_argument("--stdin", action="store_true", help="read username and password from stdin")
    args = ap.parse_args(argv)

    config = load_config()
    key = parse_username_key(config.username_key)
    username, password = _credentials(args.stdin)
    with connect(config.database_url) as conn:
        user = authenticate(conn, key, username, password)
        if user is None:
            print("error: wrong username or password (create the account at /signup first)",
                  file=sys.stderr)
            return 1
        try:
            moved = claim_existing(conn, user.id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print("Claimed: " + ", ".join(f"{n} {k}" for k, n in moved.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
