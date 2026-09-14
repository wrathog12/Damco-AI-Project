"""
The one-time code: generating it, hashing it, and getting it to a phone.

## Sending is behind an interface because it costs money

Every real provider — MSG91, Twilio Verify, Firebase Phone Auth — bills per
message, which makes the send the one part of this flow that cannot be exercised
freely in development or in a test script. So it is the one part that is a
swappable object: `StubSender` writes the code to the log, and the whole login
flow above it is identical either way.

That is not only about cost. A test that cannot log in cannot check anything about
what login unlocks, so without a stub the entire quota-tier behaviour of slice 2
would be unverifiable.

## Generating

`secrets.randbelow`, not `random`. The Mersenne Twister is seeded from the clock
and its state is recoverable from a handful of outputs, so `random.randint` would
make consecutive codes predictable from each other — which is a login bypass, not
a statistical curiosity.

Codes are zero-padded to a fixed length, so `042315` is a valid code and the
keyspace really is 10^n. Dropping the padding would silently shrink it and make
low codes rarer, which is the kind of bias that never shows up in testing.

## Hashing

Keyed HMAC under `jwt_secret`, not a bare digest. Six digits is a million
possibilities: an unkeyed SHA-256 of one is reversed by a for-loop in
milliseconds, so a database dump would expose every outstanding code. With a key
it exposes nothing without also leaking the secret.
"""
import hmac
import secrets
from hashlib import sha256
from typing import Protocol

import httpx
from loguru import logger

from config import settings


def generate_code() -> str:
    """A fresh code, `otp_code_length` digits, zero-padded."""
    n = settings.otp_code_length
    return f"{secrets.randbelow(10 ** n):0{n}d}"


def hash_code(phone_e164: str, code: str) -> str:
    """The stored form of a code.

    The phone number is bound into the message, not just the code. Without it the
    same code hashes identically for everyone, so a code harvested for one number
    could be replayed against another that happened to be issued the same one —
    which, at a million possibilities and a live user base, is not a remote event.
    """
    msg = f"{phone_e164}:{code}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, sha256).hexdigest()


def matches(phone_e164: str, code: str, code_hash: str) -> bool:
    """Constant-time comparison of a submitted code against a stored hash."""
    return hmac.compare_digest(hash_code(phone_e164, code), code_hash)


# ── Delivery ────────────────────────────────────────────
class OtpSender(Protocol):
    """Anything that can get a code onto a phone.

    Deliberately not a class hierarchy: a provider is one async call, and the
    thing worth keeping stable is the signature, not an inheritance chain. A new
    provider is a new module-level object with a `send`, plus one line in
    `get_sender`.

    A sender that fails **raises**. The endpoint above turns that into a 502, and
    the challenge row is only written once the send succeeded — otherwise a caller
    would be rate-limited for a message that never arrived.
    """

    async def send(self, phone_e164: str, code: str) -> None: ...


class StubSender:
    """Logs the code instead of sending it. The development and test provider.

    The code *is* written to the log, which is the one place in this codebase
    where a live credential is logged on purpose. It is safe only because it is
    the stub — the whole point of this object is that the code went nowhere, so the
    log is the only channel it has. `WARNING`, not `INFO`, so that seeing it in a
    deployment's logs is loud rather than something to scroll past.
    """

    name = "stub"

    async def send(self, phone_e164: str, code: str) -> None:
        # The number is masked even here. Nothing needs the full number to debug a
        # login, and log files outlive the reason they were turned on.
        from auth.phone import mask
        logger.warning(f"[otp] STUB provider — no SMS sent. "
                       f"{mask(phone_e164)} code={code}")


class Msg91Sender:
    """MSG91's OTP endpoint.

    Untested against a live account — there is no credit on one yet — so it is
    written to the documented API and left switched off rather than guessed at and
    switched on. `otp_provider` defaults to `stub`, so reaching this code requires
    someone to configure it deliberately.

    MSG91 owns the message body: the template is registered with them (Indian
    DLT rules require it) and the code is passed as a variable, which is why there
    is no message text anywhere in this file.
    """

    name = "msg91"
    _URL = "https://control.msg91.com/api/v5/otp"

    async def send(self, phone_e164: str, code: str) -> None:
        if not settings.msg91_auth_key or not settings.msg91_template_id:
            raise RuntimeError(
                "otp_provider=msg91 but msg91_auth_key/template_id are unset")

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                self._URL,
                headers={"authkey": settings.msg91_auth_key},
                json={
                    # MSG91 wants the number without the `+`.
                    "mobile": phone_e164.lstrip("+"),
                    "template_id": settings.msg91_template_id,
                    "otp": code,
                },
            )
        # Raise on a bad status *and* on MSG91's success-shaped error body: it
        # answers 200 with `{"type": "error"}` for a rejected template, and
        # treating that as sent would rate-limit the caller for nothing.
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        if str(body.get("type", "success")).lower() != "success":
            raise RuntimeError(f"msg91 refused the send: {body.get('message')}")
        # No code, no number: this is the branch where the code is a live secret.
        logger.info("[otp] sent via msg91")


_SENDERS: dict[str, OtpSender] = {"stub": StubSender(), "msg91": Msg91Sender()}


def get_sender() -> OtpSender:
    """The configured provider. One object per process — they are stateless."""
    return _SENDERS[settings.otp_provider]


def dev_echo_enabled() -> bool:
    """Whether the code may be returned in the HTTP response.

    Two conditions, not one: the flag *and* the stub provider. A deployment that
    leaves `otp_dev_echo=1` in its environment by accident but has a real provider
    configured therefore does not hand out codes over HTTP — the mistake most
    likely to actually happen is the one this guards.
    """
    return settings.otp_dev_echo and settings.otp_provider == "stub"
