import base64
import json

import pytest

from bsetl.ingest.keyprovision import (
    MAX_KEYS,
    InvalidCredentials,
    KeyQuotaExhausted,
    PortalError,
    ProvisionedKey,
    _decode_caller_ip,
    ephemeral_key,
    resolve_credentials,
)

SECRET = "eyJhbGciOi.THIS_IS_KEY_MATERIAL.signature"


def make_token(limits):
    payload = base64.urlsafe_b64encode(json.dumps({"limits": limits}).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


class FakePortal:
    """Stands in for the developer portal, recording what was asked of it."""

    def __init__(self, existing=None, ip="203.0.113.7"):
        self.existing = list(existing or [])
        self.ip = ip
        self.revoked = []
        self.created = []
        self.logged_in = False

    def login(self, email, password):
        self.logged_in = True
        return self.ip

    def list_keys(self):
        return list(self.existing)

    def revoke_key(self, key_id):
        self.revoked.append(key_id)

    def create_key(self, name, description, ip):
        rec = {"id": f"id-{len(self.created)}", "key": SECRET, "name": name, "ip": ip}
        self.created.append(rec)
        return rec


# ------------------------------------------------------------- IP decoding
def test_caller_ip_is_found_by_key_not_position():
    """The limits array order is not guaranteed; indexing into it would break
    silently the day the portal reorders it."""
    token = make_token([{"cidrs": ["198.51.100.4"], "type": "client"},
                        {"tier": "developer/silver", "type": "throttling"}])
    assert _decode_caller_ip(token) == "198.51.100.4"


def test_caller_ip_with_the_usual_ordering():
    token = make_token([{"tier": "developer/silver", "type": "throttling"},
                        {"cidrs": ["198.51.100.4"], "type": "client"}])
    assert _decode_caller_ip(token) == "198.51.100.4"


def test_caller_ip_strips_a_cidr_suffix():
    assert _decode_caller_ip(make_token([{"cidrs": ["10.0.0.5/32"]}])) == "10.0.0.5"


def test_missing_cidr_is_an_error_not_a_guess():
    with pytest.raises(PortalError):
        _decode_caller_ip(make_token([{"tier": "x", "type": "throttling"}]))


def test_garbage_token_is_rejected():
    with pytest.raises(PortalError):
        _decode_caller_ip("not-a-jwt")


# ------------------------------------------------------------------ secrecy
def test_key_material_never_appears_in_repr():
    """A leaking repr ends up in tracebacks and CI transcripts, which is the
    whole failure this module exists to prevent."""
    k = ProvisionedKey(key=SECRET, key_id="abc", ip="1.2.3.4", name="bsetl-auto-x")
    assert SECRET not in repr(k)
    assert SECRET not in str(k)
    assert SECRET not in f"{k}"
    assert "<redacted>" in repr(k)


# ------------------------------------------------------------- provisioning
def test_key_is_created_then_revoked(monkeypatch):
    monkeypatch.setenv("BS_DEV_EMAIL", "e@example.com")
    monkeypatch.setenv("BS_DEV_PASSWORD", "pw")
    portal = FakePortal()

    with ephemeral_key(portal=portal) as key:
        assert key.key == SECRET
        assert key.ip == "203.0.113.7"
        assert portal.revoked == []

    assert portal.revoked == [key.key_id]


def test_key_is_revoked_even_when_the_run_fails(monkeypatch):
    """The failure path is the one that matters: an un-revoked key is a live
    credential holding one of ten slots."""
    monkeypatch.setenv("BS_DEV_EMAIL", "e@example.com")
    monkeypatch.setenv("BS_DEV_PASSWORD", "pw")
    portal = FakePortal()

    with pytest.raises(RuntimeError, match="crawl exploded"):
        with ephemeral_key(portal=portal) as key:
            raise RuntimeError("crawl exploded")

    assert portal.revoked == [key.key_id]


def test_orphans_from_crashed_runs_are_reclaimed(monkeypatch):
    monkeypatch.setenv("BS_DEV_EMAIL", "e@example.com")
    monkeypatch.setenv("BS_DEV_PASSWORD", "pw")
    portal = FakePortal(existing=[
        {"id": "old-1", "name": "bsetl-auto-20260101T000000"},
        {"id": "old-2", "name": "bsetl-auto-20260102T000000"},
        {"id": "mine", "name": "my-laptop-key"},   # not ours; must survive
    ])

    with ephemeral_key(portal=portal):
        pass

    assert "old-1" in portal.revoked and "old-2" in portal.revoked
    assert "mine" not in portal.revoked


def test_quota_exhaustion_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("BS_DEV_EMAIL", "e@example.com")
    monkeypatch.setenv("BS_DEV_PASSWORD", "pw")
    # Ten keys, none of them ours to reclaim.
    portal = FakePortal(existing=[
        {"id": f"k{i}", "name": "someone-elses"} for i in range(MAX_KEYS)
    ])
    with pytest.raises(KeyQuotaExhausted, match="10 keys"):
        with ephemeral_key(portal=portal):
            pass


def test_missing_credentials_say_which_variables(monkeypatch):
    monkeypatch.delenv("BS_DEV_EMAIL", raising=False)
    monkeypatch.delenv("BS_DEV_PASSWORD", raising=False)
    with pytest.raises(InvalidCredentials, match="BS_DEV_EMAIL"):
        resolve_credentials(None, None)
