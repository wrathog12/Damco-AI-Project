"""
Phone numbers in, one canonical form out.

`users.phone_e164` is unique, and that constraint is the only thing keeping one
person from holding two quota buckets. It can only do that job if every write
normalises first: `9876543210`, `09876543210`, `+91 98765 43210` and
`+91-9876543210` are one number, and storing two of those spellings hands the
same caller a second allowance.

## Why this is hand-rolled and where it stops being enough

`phonenumbers` (Google's libphonenumber port) is the correct answer the moment a
second country matters — it carries the per-country mobile prefix tables that
make "is this a real mobile number" answerable. It is not a dependency yet
because the audience is Indian and the rules that matter here are two: ten
digits, and a first digit of 6-9 (India's mobile range; 2-5 are landline, and an
SMS to a landline is a silently wasted send).

Other country codes are accepted in explicit `+CC` form with a length check only.
That is deliberately permissive rather than wrong: it does not pretend to
validate what it cannot, and the OTP itself is the real validation — an
unreachable number simply never completes a login.
"""
import re

_NON_DIGIT = re.compile(r"[^\d+]")

# India's mobile series. A number starting 2-5 is a landline, and the whole point
# of collecting a number here is that an SMS can reach it.
_INDIA_MOBILE_START = frozenset("6789")


def to_e164(raw: str | None, *, default_cc: str = "91") -> str | None:
    """`"+91 98765-43210"` → `"+919876543210"`. None if it cannot be one number.

    None rather than an exception: the caller is an endpoint validating user
    input, and "that is not a phone number" is a 400 it composes itself, not an
    error to catch.
    """
    if not raw:
        return None

    # Strip everything decorative, then any `+` that is not leading — a `+` in
    # the middle means the input was two things concatenated, not a number.
    cleaned = _NON_DIGIT.sub("", str(raw).strip())
    plus, digits = cleaned.startswith("+"), cleaned.lstrip("+")
    if not digits.isdigit():
        return None

    if not plus:
        # A bare local number. `0` is India's trunk prefix and is not part of the
        # number; `91` in front of ten digits is the country code written without
        # a `+`, which is how most Indian forms are filled in.
        digits = digits.lstrip("0")
        if len(digits) == 10:
            digits = default_cc + digits
        elif not digits.startswith(default_cc):
            # Nine digits, or twelve, with no country code and no `+`: there is no
            # reading of this that is safe to guess at.
            return None

    if digits.startswith("91"):
        national = digits[2:]
        if len(national) != 10 or national[0] not in _INDIA_MOBILE_START:
            return None
        return "+" + digits

    # Some other country, given explicitly. ITU E.164 allows 15 digits including
    # the country code; below 8 there is no plausible mobile number.
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def mask(phone_e164: str | None) -> str | None:
    """`"+919876543210"` → `"+*********3210"`.

    What goes back to a client that has just logged in, so it can show *which*
    number it is signed in as without the full number being readable from a
    screenshot, a support ticket or a browser cache. The last four digits are the
    part a person recognises their own number by.

    Full numbers leave the server nowhere. `_quota_payload` in `main.py` makes the
    same promise for the quota response.
    """
    if not phone_e164:
        return None
    keep = phone_e164[-4:]
    hidden = len(phone_e164) - 4 - (1 if phone_e164.startswith("+") else 0)
    return ("+" if phone_e164.startswith("+") else "") + "*" * max(0, hidden) + keep
