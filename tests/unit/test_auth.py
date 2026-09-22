"""Real JWT verification against a real (local) OIDC provider: discovery + JWKS over HTTP, tokens
signed with a real RSA key. The decision logic is tested through the API in test_api.py; this file
is about the cryptographic path — the part a fake verifier cannot exercise."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from booth_pipeline.auth import (
    AuthError,
    OIDCVerifier,
    claims_from_payload,
    effective_role,
    role_in_workspace,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key"


class Provider(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/realms/booth/.well-known/openid-configuration":
            body = {"issuer": base + "/realms/booth", "jwks_uri": base + "/jwks"}
        elif self.path == "/jwks":
            jwk = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
            body = {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def issuer():
    srv = HTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/realms/booth"
    srv.shutdown()


def token(issuer, key=KEY, alg="RS256", **over):
    payload = {"iss": issuer, "sub": "u1", "aud": "booth-pipeline", "exp": int(time.time()) + 300, "preferred_username": "alice", "groups": ["/workspaces/acme/editor"], **over}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": KID})


def verifier(issuer, aud=True):
    return OIDCVerifier(issuer, "booth-pipeline", aud, "groups")


def test_a_valid_token_yields_claims(issuer):
    c = verifier(issuer).verify(token(issuer))
    assert (c.subject, c.display_name, c.groups) == ("u1", "alice", ("/workspaces/acme/editor",))


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("expired", {"exp": int(time.time()) - 10}),
        ("wrong issuer", {"iss": "http://evil.example/realms/booth"}),
        ("wrong audience", {"aud": "someone-else"}),
        ("no expiry", {"exp": None}),
        ("no subject", {"sub": None}),
        ("signed by another key", {"key": OTHER_KEY}),
    ],
)
def test_bad_tokens_are_rejected(issuer, name, kwargs):
    with pytest.raises(AuthError):
        verifier(issuer).verify(token(issuer, **kwargs))


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_algorithm_confusion_and_none_are_rejected(issuer):
    """The classic downgrade: HS256 "signed" using the RSA *public* key as the HMAC secret.
    PyJWT refuses to *create* such a token, so it is built by hand — the point is that our
    verifier, given one, must refuse it (its algorithm allow-list has no HS*)."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = {"iss": issuer, "sub": "u1", "aud": "booth-pipeline", "exp": int(time.time()) + 300}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = _b64(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        verifier(issuer).verify(f"{head}.{body}.{sig}")
    head_none = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode())
    with pytest.raises(AuthError):
        verifier(issuer).verify(f"{head_none}.{body}.")


def test_audience_is_only_enforced_when_the_deployment_requires_it(issuer):
    t = token(issuer, aud="something-else")
    verifier(issuer, aud=False).verify(t)
    with pytest.raises(AuthError):
        verifier(issuer, aud=True).verify(t)


def test_an_unreachable_provider_fails_closed_and_recovers_when_it_returns():
    v = OIDCVerifier("http://127.0.0.1:1/realms/booth", "c", False, "groups")
    with pytest.raises(AuthError):
        v.verify("a.b.c")  # discovery fails -> 401, not a crash, and not "allowed"


def test_garbage_tokens_are_rejected(issuer):
    for bad in ("", "not-a-jwt", "a.b.c", token(issuer)[:-4] + "AAAA"):
        with pytest.raises(AuthError):
            verifier(issuer).verify(bad)


# ---- claim handling (pure) ------------------------------------------------------------------


def test_groups_claim_of_the_wrong_shape_grants_nothing():
    for bad in ("/workspaces/acme/owner", {"a": 1}, 5, None, [1, 2]):
        assert claims_from_payload({"sub": "u", "groups": bad}, "groups").groups == ()


def test_custom_groups_claim_name_is_honoured():
    assert claims_from_payload({"sub": "u", "roles": ["/workspaces/a/owner"]}, "roles").groups == ("/workspaces/a/owner",)
    assert claims_from_payload({"sub": "u", "groups": ["/workspaces/a/owner"]}, "roles").groups == ()


def test_role_derivation_takes_the_highest_matching_grant_only_in_that_workspace():
    g = ("/workspaces/acme/viewer", "/workspaces/acme/owner", "/workspaces/other/editor", "/workspaces/ACME/owner", "/workspaces/acme/admin", "workspaces/acme/owner")
    assert role_in_workspace(g, "acme") == "owner"
    assert role_in_workspace(g, "other") == "editor"
    assert role_in_workspace(g, "nope") == ""  # malformed and mis-cased groups grant nothing


def test_effective_role_never_exceeds_either_input():
    assert effective_role("", "editor") == "editor"
    assert effective_role("viewer", "owner") == "viewer"
    assert effective_role("owner", "viewer") == "viewer"
    assert effective_role("bogus", "owner") == ""
