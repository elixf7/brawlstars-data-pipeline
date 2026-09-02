#!/usr/bin/env python3
"""Inspect and exercise developer-portal key provisioning.

Never prints key material. `check` proves the credentials work end to end by
minting a real key and revoking it again, reporting only its identity.
"""
from __future__ import annotations

import argparse

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.ingest.keyprovision import (
    DeveloperPortal,
    PortalError,
    ephemeral_key,
    resolve_credentials,
)


def cmd_list(_args: argparse.Namespace) -> int:
    email, password = resolve_credentials(None, None)
    portal = DeveloperPortal()
    ip = portal.login(email, password)
    keys = portal.list_keys()
    print(f"This host appears to the portal as {ip}")
    print(f"{len(keys)} of 10 key slots in use")
    for k in keys:
        here = "  <- matches this host" if ip in (k.get("cidrRanges") or []) else ""
        print(f"  {k.get('id')}  {k.get('name'):<32} {k.get('cidrRanges')}{here}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    with ephemeral_key() as key:
        print("Provisioned and verified:")
        print(f"  host ip : {key.ip}")
        print(f"  key id  : {key.key_id}")
        print(f"  key name: {key.name}")
        print("  (key material is never printed)")
    print("Revoked. Credentials work; scheduled runs can mint their own keys.")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Reclaim keys left behind by runs that died before cleanup."""
    email, password = resolve_credentials(None, None)
    portal = DeveloperPortal()
    portal.login(email, password)
    stale = [
        k for k in portal.list_keys()
        if str(k.get("name", "")).startswith(args.prefix)
    ]
    if not stale:
        print(f"No keys named {args.prefix}* to reclaim.")
        return 0
    for k in stale:
        portal.revoke_key(k["id"])
        print(f"  revoked {k['id']}  {k.get('name')}")
    print(f"Reclaimed {len(stale)} key slot(s).")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Developer-portal key management. Reads BS_DEV_EMAIL and "
                    "BS_DEV_PASSWORD from the environment."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show existing keys and this host's IP").set_defaults(
        func=cmd_list
    )
    sub.add_parser(
        "check", help="Mint a key, confirm it, and revoke it"
    ).set_defaults(func=cmd_check)
    sweep = sub.add_parser("sweep", help="Revoke keys left by crashed runs")
    sweep.add_argument("--prefix", default="bsetl-auto")
    sweep.set_defaults(func=cmd_sweep)

    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)
    try:
        raise SystemExit(args.func(args))
    except PortalError as e:
        # A traceback here is noise; the message already says what to do.
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    main()
