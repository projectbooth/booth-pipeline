from __future__ import annotations

import pytest

from booth_pipeline.config import Config, ConfigError

BASE = {"BOOTH_PIPELINE_DATABASE_DSN": "postgresql://x", "BOOTH_OIDC_ISSUER_URL": "https://idp/realms/b/", "BOOTH_OIDC_CLIENT_ID": "booth-pipeline"}


def load(monkeypatch, **env):
    for k in list(__import__("os").environ):
        if k.startswith(("BOOTH_PIPELINE_", "BOOTH_OIDC_", "BOOTH_CORE_")):
            monkeypatch.delenv(k)
    for k, v in {**BASE, **env}.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    return Config.from_env()


def test_defaults_and_trailing_slash_trimming(monkeypatch):
    c = load(monkeypatch, BOOTH_CORE_URL="http://booth-core:8080/")
    assert c.oidc_issuer_url == "https://idp/realms/b" and c.core_url == "http://booth-core:8080"
    assert (c.max_workers, c.oidc_groups_claim, c.oidc_require_audience) == (4, "groups", True)


def test_a_database_is_required_unless_dev_memory(monkeypatch):
    with pytest.raises(ConfigError, match="DATABASE_DSN"):
        load(monkeypatch, BOOTH_PIPELINE_DATABASE_DSN=None)
    assert load(monkeypatch, BOOTH_PIPELINE_DATABASE_DSN=None, BOOTH_PIPELINE_DEV_MEMORY="true").dev_memory


def test_identity_provider_is_required_because_every_module_verifies_tokens_itself(monkeypatch):
    with pytest.raises(ConfigError, match="ISSUER"):
        load(monkeypatch, BOOTH_OIDC_ISSUER_URL="")
    with pytest.raises(ConfigError, match="CLIENT_ID"):
        load(monkeypatch, BOOTH_OIDC_CLIENT_ID="")
    load(monkeypatch, BOOTH_OIDC_CLIENT_ID="", BOOTH_OIDC_REQUIRE_AUDIENCE="false")


def test_bad_numbers_and_unsafe_timing_are_rejected(monkeypatch):
    with pytest.raises(ConfigError, match="integer"):
        load(monkeypatch, BOOTH_PIPELINE_MAX_WORKERS="lots")
    with pytest.raises(ConfigError, match=">= 1"):
        load(monkeypatch, BOOTH_PIPELINE_MAX_WORKERS="0")
    with pytest.raises(ConfigError, match="twice the heartbeat"):
        load(monkeypatch, BOOTH_PIPELINE_HEARTBEAT_SECONDS="30", BOOTH_PIPELINE_STALE_AFTER_SECONDS="40")


def test_runner_env_passthrough_is_a_trimmed_list(monkeypatch):
    assert load(monkeypatch, BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH=" HTTPS_PROXY, NO_PROXY ,").runner_env_passthrough == ("HTTPS_PROXY", "NO_PROXY")


def test_jwks_url_is_read_and_defaults_to_empty(monkeypatch):
    """ADR 0108: optional; empty means discovery, exactly as before."""
    assert load(monkeypatch).oidc_jwks_url == ""
    certs = "http://keycloak.booth.svc:8080/realms/b/protocol/openid-connect/certs"
    assert load(monkeypatch, BOOTH_OIDC_JWKS_URL=certs).oidc_jwks_url == certs


def test_jwks_url_without_an_issuer_is_a_startup_error():
    with pytest.raises(ConfigError, match="BOOTH_OIDC_JWKS_URL is set but BOOTH_OIDC_ISSUER_URL is empty"):
        Config(dev_memory=True, oidc_client_id="c", oidc_jwks_url="http://k/certs").validate()
