"""Who a pipeline's runs act as (ADR 0058, ADR 0119 amendment 3, ADR 0120).

A pipeline's owner is a person's ``sub`` or a service account ``svc:<workspace>:<name>``. Core mints
the run's token for that owner: a person's live role (and only while they keep signing in), or the
account's stored role (no sign-in window; a disabled account is refused). Who may change the owner
is pipeline's own rule, because core has no say in it:

* the first person to save a schedule onto a pipeline becomes its owner;
* an editor or owner may **take over** a person-owned pipeline (become its owner themselves);
* only a workspace owner may make a service account the owner, or take a pipeline from one;
* nobody hands a pipeline to another person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# core's service-account id (ADR 0120): `svc:<workspace>:<name>`.
SERVICE_ACCOUNT_RE = re.compile(r"^svc:([^:\s]+):([A-Za-z0-9][A-Za-z0-9._-]{0,99})$")


def is_service_account(owner: str) -> bool:
    return owner.startswith("svc:")


@dataclass(frozen=True)
class ServiceAccountRef:
    workspace: str
    name: str

    @property
    def id(self) -> str:
        return f"svc:{self.workspace}:{self.name}"


def parse_service_account(owner: str) -> ServiceAccountRef | None:
    m = SERVICE_ACCOUNT_RE.match(owner)
    return ServiceAccountRef(m.group(1), m.group(2)) if m else None


def describe(owner: str, name: str) -> str:
    """The owner in words, for messages: ``service account svc:acme:nfl``, or a person's name."""
    if is_service_account(owner):
        return f"service account {owner}"
    return name or "the person who first scheduled it"
