"""Task secrets (ADR 0128 and its Amendment 1): a workspace owner's outside keys, handed to the tasks that
declare them.

A value is sealed with AES-256-GCM, the workspace and name bound in as associated data (a row copied to
another workspace or name won't open), and stored in the module's own database. The key never is: it lives
in a Kubernetes Secret the operator creates, mounted into the API/scheduler pod only, one file per key id
(``kubectl create secret generic booth-pipeline-secrets-key --from-literal=k1="$(openssl rand -base64 32)"``).
Every value is sealed with the current key; older keys left in the Secret still open what they sealed,
and at startup those rows are re-sealed with the current one.

At an attempt's start the engine opens the task's declared secrets and sends them in the run request as
environment variables for the task's own process (never ``invocation.json``, never the pip init
container). Everything the run stores, its log lines, task and run errors and kept outputs, has each exact
value replaced by ``[secret NAME]`` (``Redactor``). Not caught, and the UI says so: a value split across
two log lines, a transformed value (base64, URL-encoded), and anything a task sends over the network.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

# A secret's name: what a task declares and the owner sees. Letters, digits, "_" and "-".
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# Shorter values are refused: redacting a short or common value would mangle unrelated text.
MIN_LENGTH = 8
MAX_BYTES = 8 * 1024
# A key id is a Kubernetes Secret data key.
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SecretsError(Exception):
    """The key directory is unusable (a bad key file, or no way to choose the current key)."""


class TaskSecretError(Exception):
    """A declared secret can't be given to its task. Fails that task, never retried: the answer won't
    change until an owner acts."""


class SecretUnreadable(TaskSecretError):
    """A stored value can't be opened: the key that sealed it isn't mounted, or the row was altered."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes
    key_id: str


def _aad(workspace: str, name: str) -> bytes:
    return f"booth-pipeline task secret\x00{workspace}\x00{name}".encode()


class SecretBox:
    """Seals and opens task secret values. ``keys`` maps key id to 32 bytes; ``current`` seals."""

    def __init__(self, keys: dict[str, bytes], current: str) -> None:
        if current not in keys:
            raise SecretsError(f"the current key id {current!r} is not among the mounted keys")
        for kid, key in keys.items():
            if len(key) != 32:
                raise SecretsError(f"task secrets key {kid!r} is {len(key)} bytes; it must be 32 (openssl rand -base64 32)")
        self._keys = {kid: AESGCM(key) for kid, key in keys.items()}
        self.current = current

    @classmethod
    def from_dir(cls, path: str, key_id: str = "") -> SecretBox | None:
        """The mounted key Secret, or None when there is none (task secrets are then off). With several
        keys, ``key_id`` names the one that seals; with one, it may be left empty."""
        d = Path(path) if path else None
        if d is None or not d.is_dir():
            return None
        keys: dict[str, bytes] = {}
        for f in sorted(d.iterdir()):
            # A mounted Secret also holds ..data and ..<timestamp> entries (symlinks to the real files).
            if f.name.startswith(".") or not f.is_file() or not KEY_ID_RE.match(f.name):
                continue
            raw = f.read_text(encoding="utf-8").strip()
            try:
                keys[f.name] = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise SecretsError(f"task secrets key {f.name!r} is not base64 (create it with openssl rand -base64 32)") from None
        if not keys:
            return None
        if not key_id:
            if len(keys) > 1:
                raise SecretsError(f"several task secrets keys are mounted ({', '.join(keys)}); set taskSecrets.keyId to the one that seals")
            key_id = next(iter(keys))
        return cls(keys, key_id)

    def seal(self, workspace: str, name: str, value: str) -> Sealed:
        nonce = os.urandom(12)
        ct = self._keys[self.current].encrypt(nonce, value.encode("utf-8"), _aad(workspace, name))
        return Sealed(ct, nonce, self.current)

    def open(self, workspace: str, name: str, sealed: Sealed) -> str:
        aead = self._keys.get(sealed.key_id)
        if aead is None:
            raise SecretUnreadable(f"secret {name} was sealed with key {sealed.key_id!r}, which is no longer mounted; re-enter it")
        try:
            return aead.decrypt(sealed.nonce, sealed.ciphertext, _aad(workspace, name)).decode("utf-8")
        except Exception:  # noqa: BLE001 - InvalidTag: altered, or sealed for another workspace or name
            raise SecretUnreadable(f"secret {name} could not be decrypted; re-enter it") from None

    def can_open(self, key_id: str) -> bool:
        return key_id in self._keys


def check_value(value: str) -> str | None:
    """Why a value can't be stored, or None."""
    if len(value) < MIN_LENGTH:
        return f"a secret must be at least {MIN_LENGTH} characters: a shorter value can't be redacted from logs safely"
    if len(value.encode("utf-8")) > MAX_BYTES:
        return f"a secret can be at most {MAX_BYTES} bytes"
    if value != value.strip():
        return "a secret can't start or end with whitespace (it is usually a copy-paste slip)"
    if "\n" in value or "\r" in value or "\x00" in value:
        return "a secret must be one line (it is set as an environment variable)"
    return None


class SecretSource:
    """One run's view of its workspace's secrets: what the engine opens at an attempt's start. ``box`` is
    None when no key is mounted (task secrets are then off on this install)."""

    def __init__(self, store: Any, box: SecretBox | None, workspace: str) -> None:
        self._store, self._box, self._workspace = store, box, workspace

    def values(self, names: list[str]) -> dict[str, str]:
        """name -> value for each name. Raises ``TaskSecretError`` naming the first that can't be given."""
        if self._box is None:
            raise TaskSecretError("task secrets are not set up on this install (an operator sets the chart's taskSecrets.keySecretName)")
        out: dict[str, str] = {}
        for name in dict.fromkeys(names):
            row = self._store.get_task_secret(self._workspace, name)
            if row is None:
                raise TaskSecretError(f"secret {name} is not set in this workspace")
            out[name] = self._box.open(self._workspace, name, Sealed(row.ciphertext, row.nonce, row.key_id))
        return out


def reseal(store: Any, box: SecretBox) -> None:
    """After a key rotation (a new current key id), re-seal every value sealed with another key that is
    still mounted. A value whose key is gone can't be opened: it is left, and must be re-entered."""
    lost = 0
    for row in store.task_secrets_sealed_with_other_keys(box.current):
        if not box.can_open(row.key_id):
            lost += 1
            continue
        value = box.open(row.workspace, row.name, Sealed(row.ciphertext, row.nonce, row.key_id))
        sealed = box.seal(row.workspace, row.name, value)
        row.ciphertext, row.nonce, row.key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        store.put_task_secret(row)
    if lost:
        log.warning("%d task secret(s) were sealed with a key that is no longer mounted; they can't be opened and must be re-entered", lost)


class Redactor:
    """The values one run has handed to its tasks, and the one place they are taken out of what the run
    stores. Longest first, so a value that contains another is replaced whole."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_value: dict[str, str] = {}
        self._order: list[str] = []

    def add(self, values: dict[str, str]) -> None:
        """``values``: secret name -> value."""
        with self._lock:
            for name, value in values.items():
                if value and value not in self._by_value:
                    self._by_value[value] = name
            self._order = sorted(self._by_value, key=len, reverse=True)

    def __bool__(self) -> bool:
        return bool(self._order)

    def text(self, s: str) -> str:
        order = self._order
        for value in order:
            if value in s:
                s = s.replace(value, f"[secret {self._by_value[value]}]")
        return s

    def maybe(self, s: str | None) -> str | None:
        return None if s is None else self.text(s)

    def value(self, v: Any) -> Any:
        """A JSON value with every string (and every object key) redacted."""
        if not self._order:
            return v
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {self.text(k) if isinstance(k, str) else k: self.value(x) for k, x in v.items()}
        return v
