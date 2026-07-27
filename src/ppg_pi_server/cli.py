"""CLI for Pi-side admin tasks: token management, DB init, server.

Examples::

    ppg-pi-server token add phone-01
    ppg-pi-server token list
    ppg-pi-server token revoke <token>
    ppg-pi-server db init
    ppg-pi-server serve
"""

from __future__ import annotations

import argparse
import sys

import duckdb

from ._corelib import converter
from .auth import add_token, revoke_token
from .config import get_settings
from .schema import init_audit_schema


def _cmd_token_add(args) -> int:
    settings = get_settings()
    token = add_token(settings, args.phone_id, token=args.token, scope=args.scope)
    print(f"Phone:  {args.phone_id}")
    print(f"Scope:  {args.scope}")
    print(f"Token:  {token}")
    print()
    print(f"Stored in {settings.tokens_file}.")
    print("Configure the phone with this token (paste it into the app's settings).")
    return 0


def _cmd_token_list(args) -> int:
    settings = get_settings()
    tokens = settings.load_tokens()
    if not tokens:
        print("No tokens configured.")
        return 0
    print(f"{'TOKEN (prefix)':<20} {'PHONE_ID':<24} {'CREATED'}")
    for t, meta in tokens.items():
        print(f"{t[:16]:<20} {meta.get('phone_id', '?'):<24} {meta.get('created_at', '?')}")
    return 0


def _cmd_token_revoke(args) -> int:
    settings = get_settings()
    if revoke_token(settings, args.token):
        print(f"Revoked token {args.token[:16]}...")
        return 0
    print("Token not found.", file=sys.stderr)
    return 1


def _cmd_db_init(args) -> int:
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(settings.db_path))
    try:
        converter.init_tables(con)   # canonical data tables (sessions, ppg, ...)
        init_audit_schema(con)       # server-owned uploads audit table
    finally:
        con.close()
    print(f"Initialised {settings.db_path}.")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    settings = get_settings()
    print(f"Listening on {settings.bind_host}:{settings.bind_port}")
    print(f"DB: {settings.db_path}")
    print(f"Uploads: {settings.upload_dir}")
    print(f"Tokens: {settings.tokens_file}")
    uvicorn.run(
        "ppg_pi_server.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
        reload=args.reload,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ppg-pi-server", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    token = sub.add_parser("token", help="Manage bearer tokens")
    token_sub = token.add_subparsers(dest="action", required=True)

    add = token_sub.add_parser("add", help="Add a bearer token for a phone")
    add.add_argument("phone_id", help="Identifier for the phone (e.g. phone-01)")
    add.add_argument("--token", help="Use a specific token (default: random)")
    add.add_argument(
        "--scope",
        choices=["write", "read"],
        default="write",
        help=(
            "write: may upload (phones). read: may view the web UI and status "
            "API only (browsers). Default write."
        ),
    )
    add.set_defaults(func=_cmd_token_add)

    lst = token_sub.add_parser("list", help="List tokens")
    lst.set_defaults(func=_cmd_token_list)

    rev = token_sub.add_parser("revoke", help="Revoke a token")
    rev.add_argument("token", help="Full token to revoke")
    rev.set_defaults(func=_cmd_token_revoke)

    db = sub.add_parser("db", help="Database admin")
    db_sub = db.add_subparsers(dest="action", required=True)
    db_init = db_sub.add_parser("init", help="Initialise schema")
    db_init.set_defaults(func=_cmd_db_init)

    serve = sub.add_parser("serve", help="Run the ingest API server")
    serve.add_argument("--reload", action="store_true", help="Auto-reload on file changes")
    serve.set_defaults(func=_cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
