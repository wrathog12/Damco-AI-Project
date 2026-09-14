"""
Identity and access. Everything here is P3.

Two kinds of caller, one resolution path:

* **Anonymous** — a uuid the server minted into a signed httpOnly cookie. No
  personal data, no login, and the default state for everyone. `anon.py`.
* **Authenticated** — a phone number verified by OTP, carrying a short-lived
  access JWT. Unlocks a larger turn allowance, nothing else.

The deliberate property: nothing in the request path *requires* a logged-in user.
An unauthenticated request is a valid request with a smaller allowance, never a
401. That is the whole product decision — see `config.Settings`' quota block.
"""
