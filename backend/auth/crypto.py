"""
Encryption at rest for the personal data slice 3 stores.

The DPDP Act 2023 lists age, income, caste and disability as sensitive personal
data and asks for encryption at rest. Disk-level encryption satisfies the letter
of that and very little of its spirit: it protects against a stolen drive and
against nothing else — not a leaked backup, not a `SELECT *` from an over-broad
service account, not the developer debugging a production query. So the columns
that hold those facts are encrypted by the application, and Postgres stores
opaque bytes it cannot read.

## What this costs, and why it costs nothing here

An encrypted column cannot be filtered, indexed, ordered or aggregated. That
would be disqualifying for most columns and is free for these: a profile is only
ever read whole, for one user, by primary key, and the eligibility rules run in
Python on the decrypted values. There is no query in this system of the form
"every user in Bihar", and if one is ever wanted it must be answered by asking
the users rather than by reading round the encryption.

## The envelope

    b"\\x01" || nonce(12) || AES-256-GCM(plaintext, aad)

AES-GCM rather than a bare cipher because it authenticates: a ciphertext that has
been altered fails to decrypt instead of decrypting to plausible rubbish, which
for an income figure is the difference between an error and a wrong eligibility
verdict. The version byte exists so a future format change is detectable rather
than silently misread.

`aad` binds each ciphertext to where it lives — `"profile:41"`, `"message:993"`.
Without it, a row copied from one user to another would decrypt perfectly and the
database would be quietly lying about whose income it is. With it, a moved
ciphertext fails authentication.

## The key, and the deliberate absence of a default

`PROFILE_ENCRYPTION_KEY` has no default, and with no key set nothing personal is
stored at all: profiles refuse to save and transcripts are not recorded. That is
a stricter stance than `jwt_secret`'s dev default, on purpose — a well-known
signing secret costs an attacker's free turns, while a well-known *encryption*
key means a database full of caste and income data that is encrypted in form and
public in fact. Refusing to collect is the safe failure, and it keeps the cost of
the feature visible rather than accidental.

It is deliberately **not** derived from `jwt_secret`. That secret is already
rotated to log everyone out, and coupling it to this one would mean an ordinary
logout-everyone rotation silently destroyed every stored profile.

Rotating this key is therefore a real migration: decrypt with the old, re-encrypt
with the new. Until such a script exists, a rotation loses the data — which is
why `decrypt` returns None rather than raising. A caller who cannot be decrypted
is treated as a caller with no profile, so a mis-set key degrades to "the agent
asks again" instead of a 500 on every request.
"""
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from loguru import logger

from config import settings

_VERSION = b"\x01"
_NONCE_BYTES = 12          # GCM's standard nonce size; anything else is slower
_INFO = b"bhasha-profile-v1"

_key: bytes | None = None
_warned = False


def _derive() -> bytes | None:
    """The 32-byte AES key, or None when no key is configured.

    HKDF rather than using the configured string directly, so any length of
    passphrase yields a correct-length key without the operator having to know
    that AES-256 wants exactly 32 bytes. Cached because HKDF on every message
    would be a hash per row for no benefit.
    """
    global _key, _warned
    if _key is not None:
        return _key

    secret = settings.profile_encryption_key.strip()
    if not secret:
        if not _warned:
            _warned = True
            logger.warning(
                "[privacy] PROFILE_ENCRYPTION_KEY is not set — profiles and "
                "transcripts will not be stored. This is the safe default, not "
                "a failure; see auth/crypto.py.")
        return None

    _key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INFO).derive(secret.encode())
    return _key


def available() -> bool:
    """Whether personal data can be stored at all.

    Every write path checks this rather than assuming it: the alternative is an
    endpoint that appears to save a profile and silently discards it, which is
    worse than refusing.
    """
    return _derive() is not None


def fingerprint() -> str | None:
    """Six hex characters identifying the configured key, for logs and checks.

    Enough to tell "the key changed" from "the data is corrupt" when a profile
    stops decrypting, and far too little to help anyone reconstruct it.
    """
    key = _derive()
    return hashlib.sha256(key).hexdigest()[:6] if key else None


def encrypt(plaintext: bytes, *, aad: str) -> bytes:
    """Seal `plaintext`, bound to `aad`. Raises if no key is configured.

    Raising is right here and returning None is right in `decrypt`: a write that
    cannot be protected must not happen, while a read that cannot be unsealed is
    just an absent value.
    """
    key = _derive()
    if key is None:
        raise RuntimeError("no PROFILE_ENCRYPTION_KEY configured")
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad.encode())
    return _VERSION + nonce + sealed


def decrypt(blob: bytes | None, *, aad: str) -> bytes | None:
    """Open a sealed value, or None if it cannot be opened.

    None covers every failure the same way — wrong key, altered bytes, a row
    written under a different `aad`, an unknown version — because the caller's
    correct response to all of them is identical: behave as though the value was
    never stored. The distinction that matters for an operator is in the log line.
    """
    if not blob:
        return None
    key = _derive()
    if key is None:
        return None
    if blob[:1] != _VERSION:
        logger.error(f"[privacy] ciphertext version {blob[:1]!r} is not "
                     f"understood by this build")
        return None
    try:
        return AESGCM(key).decrypt(blob[1:1 + _NONCE_BYTES],
                                   blob[1 + _NONCE_BYTES:], aad.encode())
    except InvalidTag:
        # The single most likely cause is a changed PROFILE_ENCRYPTION_KEY, which
        # is why the fingerprint is in the message: it turns "why is everyone's
        # profile empty" into one line of diagnosis.
        logger.error(f"[privacy] could not decrypt {aad} with key "
                     f"{fingerprint()} — wrong key, or the row was tampered with")
        return None
