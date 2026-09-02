"""Short-lived API keys, minted for the machine that is running.

Supercell API keys are CIDR-locked: the allowed IP is baked into the token, so
a key made at home returns 403 from a CI runner, whose address is neither known
in advance nor stable. Storing a long-lived key as a secret cannot work here,
and would be the wrong shape anyway.

Instead the run signs in to the developer portal, mints a key scoped to its own
address, and revokes it on the way out. What is stored is a credential, not a
key; the key exists for the length of one run and never touches disk.

The caller's IP comes from the login response itself: it returns a
`temporaryAPIToken` whose JWT payload carries the CIDR the portal saw. No
third-party IP-lookup service is involved.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from bsetl.logconfig import get_logger

PORTAL = "https://developer.brawlstars.com/api"
SCOPE = "brawlstars"
# The portal caps an account at 10 keys.
MAX_KEYS = 10
# Keys carrying this name prefix are considered ours and disposable.
MANAGED_PREFIX = "bsetl-auto"
TIMEOUT = 30

logger = get_logger(__name__)


class PortalError(RuntimeError):
    """The developer portal refused or failed a request."""


class InvalidCredentials(PortalError):
    pass


class KeyQuotaExhausted(PortalError):
    pass


@dataclass(frozen=True)
class ProvisionedKey:
    """A key that exists for the duration of one run.

    Never renders the token. A repr that leaked it would end up in a traceback,
    a log line, or a CI transcript, which is the failure this module exists to
    prevent.
    """

    key: str
    key_id: str
    ip: str
    name: str

    def __repr__(self) -> str:
        return f"ProvisionedKey(id={self.key_id!r}, ip={self.ip!r}, name={self.name!r}, key=<redacted>)"

    __str__ = __repr__


def _decode_caller_ip(temporary_token: str) -> str:
    """Pull the caller's address out of the login response's JWT.

    The payload carries a `limits` array; the client entry holds the CIDR the
    portal observed. It is located by looking for that key rather than by
    index, because the order of the limits entries is not guaranteed.
    """
    try:
        payload_b64 = temporary_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except Exception as e:
        raise PortalError(f"Could not decode the portal's temporary token: {e}") from e

    for limit in payload.get("limits", []):
        cidrs = limit.get("cidrs") if isinstance(limit, dict) else None
        if cidrs:
            return str(cidrs[0]).split("/")[0]
    raise PortalError("Login response contained no CIDR for this host")


class DeveloperPortal:
    """Thin client over the portal's key-management endpoints."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def _post(self, path: str, payload: dict | None = None) -> dict:
        resp = self.session.post(f"{PORTAL}{path}", json=payload or {}, timeout=TIMEOUT)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 403 and path == "/login":
            raise InvalidCredentials(
                "Developer portal rejected the credentials. Check BS_DEV_EMAIL "
                "and BS_DEV_PASSWORD."
            )
        if resp.status_code != 200:
            raise PortalError(
                f"{path} failed with HTTP {resp.status_code}: "
                f"{body.get('description') or body.get('error') or 'no detail'}"
            )
        return body

    def login(self, email: str, password: str) -> str:
        """Authenticate and return the IP the portal saw."""
        body = self._post("/login", {"email": email, "password": password})
        token = body.get("temporaryAPIToken")
        if not token:
            raise PortalError("Login succeeded but returned no temporaryAPIToken")
        return _decode_caller_ip(token)

    def list_keys(self) -> list[dict]:
        return self._post("/apikey/list").get("keys") or []

    def revoke_key(self, key_id: str) -> None:
        self._post("/apikey/revoke", {"id": key_id})

    def create_key(self, name: str, description: str, ip: str) -> dict:
        body = self._post(
            "/apikey/create",
            {
                "name": name,
                "description": description,
                "cidrRanges": [ip],
                "scopes": [SCOPE],
            },
        )
        key = body.get("key")
        if not key or not key.get("key"):
            raise PortalError("Key creation returned no key material")
        return key


def resolve_credentials(email: str | None, password: str | None) -> tuple[str, str]:
    email = email or os.environ.get("BS_DEV_EMAIL")
    password = password or os.environ.get("BS_DEV_PASSWORD")
    if not email or not password:
        raise InvalidCredentials(
            "Developer portal credentials missing. Set BS_DEV_EMAIL and "
            "BS_DEV_PASSWORD (see .env.example)."
        )
    return email, password


@contextmanager
def ephemeral_key(
    email: str | None = None,
    password: str | None = None,
    *,
    name_prefix: str = MANAGED_PREFIX,
    portal: DeveloperPortal | None = None,
) -> Iterator[ProvisionedKey]:
    """Mint a key for this host, and revoke it however the block exits.

    Keys already carrying `name_prefix` are swept first. A run that dies without
    reaching its cleanup leaves an orphan behind, and the account allows only
    ten; without the sweep, a handful of crashes would wedge the pipeline until
    someone cleared keys by hand.

    That sweep assumes one pipeline per prefix. Concurrent runs sharing a prefix
    would revoke each other's keys — give them distinct prefixes.
    """
    email, password = resolve_credentials(email, password)
    portal = portal or DeveloperPortal()

    ip = portal.login(email, password)
    logger.info("Portal sees this host as %s", ip)

    existing = portal.list_keys()
    orphans = [k for k in existing if str(k.get("name", "")).startswith(name_prefix)]
    for orphan in orphans:
        logger.info("Reclaiming orphaned key %s from an earlier run", orphan.get("id"))
        try:
            portal.revoke_key(orphan["id"])
        except PortalError as e:
            logger.warning("Could not revoke orphaned key %s: %s", orphan.get("id"), e)

    if len(existing) - len(orphans) >= MAX_KEYS:
        raise KeyQuotaExhausted(
            f"The account holds {len(existing)} keys and none are ours to reclaim. "
            f"The portal allows {MAX_KEYS}; remove some at {PORTAL.rsplit('/', 1)[0]}."
        )

    name = f"{name_prefix}-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%S')}"
    created = portal.create_key(
        name=name,
        description=f"Ephemeral key for an automated ETL run, {ip}. Revoked on exit.",
        ip=ip,
    )
    provisioned = ProvisionedKey(
        key=created["key"], key_id=created["id"], ip=ip, name=name
    )

    try:
        yield provisioned
    finally:
        try:
            portal.revoke_key(provisioned.key_id)
        except PortalError as e:
            # Worth shouting about: an un-revoked key is a live credential and
            # occupies one of ten slots.
            logger.error(
                "Failed to revoke key %s. It remains live and occupies one of %d slots; "
                "run `bsetl-key sweep` to reclaim it. (%s)",
                provisioned.key_id, MAX_KEYS, e,
            )
