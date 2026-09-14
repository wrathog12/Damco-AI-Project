"""
Phone-OTP login — the P3 slice-2 verification gate.

Four things have to hold, and each of them fails silently if it breaks:

1. **A code is required, and it is the right code.** Wrong codes are refused,
   attempts are capped, and a resend is rate-limited. This is the only part of the
   app that is allowed to say no, so it has to actually say it.
2. **Logging in promotes the anonymous identity rather than replacing it.** The
   caller who has already spent turns keeps that history and gains the larger
   allowance — they must not be handed a fresh bucket, and must not be locked out
   because the anonymous row was already exhausted.
3. **The larger allowance is actually served.** A bearer token has to change what
   `/chat` and `/api/quota` report, or slice 2 unlocks nothing.
4. **A stolen refresh token is detectable.** Rotation works, and re-presenting a
   spent token revokes the whole family instead of quietly minting another session.

Needs the server running with the stub OTP provider and the code echoed back, or
there is no way to complete a login without reading the log:

    OTP_DEV_ECHO=1 ANON_TURN_QUOTA=3 python main.py     # in one shell
    python scripts/verify_auth.py                       # in another

`ANON_TURN_QUOTA` is small so the promotion check can exhaust the anonymous
allowance in a few real LLM calls. Exit code 0 = clean.
"""
import argparse
import asyncio
import random
import sys
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

AUTH = "/api/auth"


def _fail(msg: str) -> int:
    print(f"  >> FAIL: {msg}")
    return 1


def _phone() -> str:
    """A random Indian mobile number, so reruns are not rate-limited by each other.

    The rate limit is keyed on the phone number (see `services/auth.py`), which is
    correct and means a fixed test number would hit the hourly send cap on the
    fifth run of this script.
    """
    return f"+9198{random.randint(0, 99_999_999):08d}"


async def _request_code(client: httpx.AsyncClient, phone: str) -> str | None:
    resp = await client.post(f"{AUTH}/request-otp", json={"phone": phone})
    if resp.status_code != 200:
        print(f"  >> request-otp returned {resp.status_code}: {resp.text[:120]}")
        return None
    return resp.json().get("dev_code")


# ── 1. the code gate ────────────────────────────────────
async def check_otp_gate(base_url: str) -> int:
    failures = 0
    print("=" * 70)
    print("OTP: a code is required, and it is checked")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        phone = _phone()

        # ── a malformed number is a 400, not a send ─────────
        bad = await client.post(f"{AUTH}/request-otp", json={"phone": "12345678"})
        print(f"  bad number          status={bad.status_code} "
              f"error={bad.json().get('error') if bad.content else '?'}")
        if bad.status_code != 400:
            failures += _fail("a number that cannot be E.164 was accepted; it "
                              "would become a second quota bucket for one person")

        # ── a code is issued ────────────────────────────────
        resp = await client.post(f"{AUTH}/request-otp", json={"phone": phone})
        body = resp.json()
        code = body.get("dev_code")
        print(f"  request-otp         status={resp.status_code} "
              f"phone={body.get('phone')} dev_code={'yes' if code else 'no'}")
        if resp.status_code != 200:
            return _fail(f"request-otp failed: {resp.text[:200]}")
        if not code:
            return _fail("no dev_code in the response — start the server with "
                         "OTP_DEV_ECHO=1 (and the stub provider) or this script "
                         "cannot log in")
        if body.get("phone") and body["phone"].count("*") == 0:
            failures += _fail("the response echoed the full phone number back; it "
                              "must be masked")

        # ── the resend cooldown bites ───────────────────────
        again = await client.post(f"{AUTH}/request-otp", json={"phone": phone})
        print(f"  immediate resend    status={again.status_code} "
              f"retry_after={again.headers.get('retry-after')}")
        if again.status_code != 429:
            failures += _fail(f"a resend one second later returned "
                              f"{again.status_code}; every send costs money")
        elif not again.headers.get("retry-after"):
            failures += _fail("a 429 with no Retry-After header — the client has "
                              "nothing to show a timer from")

        # ── a wrong code is refused ─────────────────────────
        wrong = "".join("0" if c != "0" else "1" for c in code)
        resp = await client.post(f"{AUTH}/verify-otp",
                                 json={"phone": phone, "code": wrong})
        print(f"  wrong code          status={resp.status_code} "
              f"error={resp.json().get('error') if resp.content else '?'}")
        if resp.status_code != 401:
            failures += _fail(f"a wrong code returned {resp.status_code}, not 401")
        if "access_token" in (resp.text or ""):
            failures += _fail("a wrong code produced an access token")

        # ── the right code works ────────────────────────────
        resp = await client.post(f"{AUTH}/verify-otp",
                                 json={"phone": phone, "code": code})
        ok = resp.status_code == 200
        session = resp.json() if ok else {}
        print(f"  right code          status={resp.status_code} "
              f"token={'yes' if session.get('access_token') else 'no'} "
              f"quota={session.get('quota', {}).get('limit')}")
        if not ok:
            return failures + _fail(f"the correct code was refused: "
                                    f"{resp.text[:200]}")
        if not session.get("access_token"):
            failures += _fail("login returned no access token")
        if session.get("phone") and "*" not in session["phone"]:
            failures += _fail("the session response leaked the full phone number")

        # ── the code cannot be replayed ─────────────────────
        replay = await client.post(f"{AUTH}/verify-otp",
                                  json={"phone": phone, "code": code})
        print(f"  replayed code       status={replay.status_code}")
        if replay.status_code == 200:
            failures += _fail("the same code logged in twice — it is not consumed")

    return failures


# ── 2. attempts are capped ──────────────────────────────
async def check_attempt_cap(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("OTP: a six-digit code cannot be walked through")
    print("=" * 70)

    from config import settings

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        phone = _phone()
        code = await _request_code(client, phone)
        if not code:
            return _fail("could not obtain a code")

        wrong = "".join("0" if c != "0" else "1" for c in code)
        statuses = []
        for _ in range(settings.otp_max_attempts + 1):
            resp = await client.post(f"{AUTH}/verify-otp",
                                     json={"phone": phone, "code": wrong})
            statuses.append(resp.status_code)
        print(f"  {settings.otp_max_attempts + 1} wrong attempts   "
              f"statuses={statuses}")

        # After the cap the challenge is gone, so even the *right* code fails.
        resp = await client.post(f"{AUTH}/verify-otp",
                                 json={"phone": phone, "code": code})
        print(f"  right code after    status={resp.status_code}")
        if resp.status_code == 200:
            failures += _fail(f"the challenge survived "
                              f"{settings.otp_max_attempts + 1} wrong attempts — "
                              "a six-digit code is brute-forceable")
        if any(s == 200 for s in statuses):
            failures += _fail("a wrong code was accepted somewhere in the run")

    return failures


# ── 3. promotion and the larger allowance ───────────────
async def check_promotion(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("LOGIN: the anonymous identity is promoted, not replaced")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        # Become an anonymous caller and spend the whole allowance.
        state = (await client.get("/api/quota")).json()
        anon_limit = state["limit"]
        print(f"  anonymous limit     {anon_limit}")

        for turn in range(anon_limit):
            await client.post("/chat", json={"message": "Bihar mein scheme batao",
                                             "language": "hi"})
        spent = (await client.get("/api/quota")).json()
        print(f"  after spending      used={spent['used']}/{spent['limit']} "
              f"allowed={spent['allowed']}")
        if spent["allowed"]:
            failures += _fail("the anonymous allowance was not exhausted; the rest "
                              "of this check proves nothing. Use a small "
                              "ANON_TURN_QUOTA.")

        # Log in on the same client, so the anonymous cookie is present.
        phone = _phone()
        code = await _request_code(client, phone)
        if not code:
            return failures + _fail("could not obtain a code")
        session = (await client.post(f"{AUTH}/verify-otp",
                                     json={"phone": phone, "code": code})).json()
        token = session.get("access_token")
        if not token:
            return failures + _fail("login failed, cannot continue")

        after = session.get("quota", {})
        print(f"  right after login   used={after.get('used')}/"
              f"{after.get('limit')} remaining={after.get('remaining')}")
        if after.get("limit") == anon_limit:
            failures += _fail("logging in did not raise the limit — signing in "
                              "unlocks nothing")
        if after.get("used") != spent["used"]:
            failures += _fail(f"used went from {spent['used']} to "
                              f"{after.get('used')}: the anonymous row was "
                              "replaced rather than promoted, so a caller can "
                              "reset their quota by signing in")

        # The bearer token has to be what serves the larger allowance.
        auth_headers = {"Authorization": f"Bearer {token}"}
        with_token = (await client.get("/api/quota",
                                       headers=auth_headers)).json()
        print(f"  with bearer token   used={with_token['used']}/"
              f"{with_token['limit']} allowed={with_token['allowed']}")
        if not with_token["allowed"]:
            failures += _fail("still refused after logging in — the larger "
                              "allowance is not being served")
        if not with_token["authenticated"]:
            failures += _fail("/api/quota reports authenticated=false with a "
                              "valid bearer token")

        # And a turn actually goes through.
        chat = (await client.post("/chat", headers=auth_headers,
                                  json={"message": "Kisan ke liye kuch hai",
                                        "language": "hi"})).json()
        q = chat.get("quota", {})
        print(f"  /chat with token    allowed={q.get('allowed')} "
              f"used={q.get('used')}/{q.get('limit')}")
        if not q.get("allowed"):
            failures += _fail("/chat still refuses a logged-in caller")

        # /api/me must only reveal the number to a request that proved it.
        me_token = (await client.get("/api/me", headers=auth_headers)).json()
        me_cookie = (await client.get("/api/me")).json()
        print(f"  /api/me with token  authenticated={me_token['authenticated']} "
              f"phone={me_token['phone']}")
        print(f"  /api/me cookie only authenticated={me_cookie['authenticated']} "
              f"phone={me_cookie['phone']}")
        if not me_token["authenticated"] or not me_token["phone"]:
            failures += _fail("/api/me told a token-bearing caller nothing")
        if me_token["phone"] and "*" not in me_token["phone"]:
            failures += _fail("/api/me returned an unmasked phone number")
        if me_cookie["phone"] is not None:
            failures += _fail("/api/me handed the phone number to a request "
                              "carrying only a cookie — a cookie is not proof")

        # ── a second login on a fresh client reuses the row ──
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as other:
            code2 = await _request_code(other, phone)
            if code2:
                s2 = (await other.post(f"{AUTH}/verify-otp",
                                       json={"phone": phone,
                                             "code": code2})).json()
                q2 = s2.get("quota", {})
                print(f"  same number, new    used={q2.get('used')}/"
                      f"{q2.get('limit')}")
                if q2.get("used", -1) == 0:
                    failures += _fail("signing in with the same number on a new "
                                      "client started a fresh bucket — the phone "
                                      "number is not the identity")

    return failures


# ── 4. refresh rotation and replay ──────────────────────
async def check_refresh(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("SESSION: rotation works and a replayed token kills the family")
    print("=" * 70)

    from config import settings
    cookie_name = settings.refresh_cookie_name

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        phone = _phone()
        code = await _request_code(client, phone)
        if not code:
            return _fail("could not obtain a code")
        resp = await client.post(f"{AUTH}/verify-otp",
                                 json={"phone": phone, "code": code})
        if resp.status_code != 200:
            return _fail("login failed, cannot continue")

        # Read the cookie off the response, not the client jar: the jar can hold
        # more than one value for a name once the server has rotated it (the same
        # trap `verify_quota.py` hit with `bh_anon`).
        stolen = resp.cookies.get(cookie_name)
        print(f"  refresh cookie      {'set' if stolen else 'MISSING'}")
        if not stolen:
            return failures + _fail(f"no {cookie_name} cookie — the session "
                                    "cannot be renewed or revoked")

        rotated = await client.post(f"{AUTH}/refresh")
        fresh = rotated.cookies.get(cookie_name)
        print(f"  refresh             status={rotated.status_code} "
              f"rotated={'yes' if fresh and fresh != stolen else 'no'}")
        if rotated.status_code != 200:
            failures += _fail(f"refresh returned {rotated.status_code}")
        if not fresh or fresh == stolen:
            failures += _fail("the refresh token was not rotated; a stolen one "
                              "would stay valid for its full lifetime")
        if rotated.status_code == 200 and not rotated.json().get("access_token"):
            failures += _fail("refresh returned no new access token")

        # Replay the spent token, the way a thief with a copy would.
        replay = await client.post(f"{AUTH}/refresh",
                                   cookies={cookie_name: stolen})
        print(f"  replay spent token  status={replay.status_code} "
              f"error={replay.json().get('error') if replay.content else '?'}")
        if replay.status_code != 401:
            failures += _fail(f"a spent refresh token was accepted "
                              f"({replay.status_code}) — theft is undetectable")

        # The replay must have revoked the family, so the *legitimate* current
        # token is dead too. That is the point: both parties are logged out and
        # only the real one can get an SMS.
        after = await client.post(f"{AUTH}/refresh",
                                  cookies={cookie_name: fresh} if fresh else None)
        print(f"  family after replay status={after.status_code}")
        if after.status_code == 200:
            failures += _fail("the family survived a replay — a thief keeps "
                              "rotating alongside the real user")

    # ── logout ends a clean session ─────────────────────
    # A fresh client and a fresh number, so this tests logout rather than the
    # family the replay above already revoked.
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        phone = _phone()
        code = await _request_code(client, phone)
        if not code:
            return failures + _fail("could not obtain a code")
        login = await client.post(f"{AUTH}/verify-otp",
                                  json={"phone": phone, "code": code})
        token = login.cookies.get(cookie_name)

        out = await client.post(f"{AUTH}/logout")
        print(f"  logout              status={out.status_code}")
        if out.status_code != 200:
            failures += _fail("logout failed; asking to be signed out cannot fail")

        dead = await client.post(f"{AUTH}/refresh",
                                 cookies={cookie_name: token} if token else None)
        print(f"  refresh after out   status={dead.status_code}")
        if dead.status_code == 200:
            failures += _fail("the refresh token still worked after logout")

        # Twice is still a success.
        twice = await client.post(f"{AUTH}/logout")
        if twice.status_code != 200:
            failures += _fail("a second logout errored")

    return failures


# ── 5. erasure ──────────────────────────────────────────
async def check_erasure(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("DPDP: DELETE /api/me erases the account")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        # An anonymous caller has nothing to erase and must be told so.
        anon_delete = await client.delete("/api/me")
        print(f"  anonymous delete    status={anon_delete.status_code}")
        if anon_delete.status_code != 401:
            failures += _fail(f"an anonymous caller could DELETE /api/me "
                              f"({anon_delete.status_code}) — that is a quota "
                              "reset dressed as a data right")

        phone = _phone()
        code = await _request_code(client, phone)
        if not code:
            return failures + _fail("could not obtain a code")
        session = (await client.post(f"{AUTH}/verify-otp",
                                     json={"phone": phone, "code": code})).json()
        headers = {"Authorization": f"Bearer {session['access_token']}"}

        gone = await client.delete("/api/me", headers=headers)
        print(f"  authenticated       status={gone.status_code}")
        if gone.status_code != 200:
            failures += _fail(f"DELETE /api/me returned {gone.status_code}")

        # The row is gone, so the same number logs in as a brand-new identity with
        # no history — which is what erasure has to mean.
        code = await _request_code(client, phone)
        if code:
            again = (await client.post(f"{AUTH}/verify-otp",
                                       json={"phone": phone,
                                             "code": code})).json()
            used = again.get("quota", {}).get("used")
            print(f"  same number after   used={used}")
            if used not in (0, 1):
                failures += _fail(f"a re-registered number came back with "
                                  f"used={used} — the old row survived")

    return failures


async def run(base_url: str) -> int:
    failures = 0
    failures += await check_otp_gate(base_url)
    failures += await check_attempt_cap(base_url)
    failures += await check_promotion(base_url)
    failures += await check_refresh(base_url)
    failures += await check_erasure(base_url)
    print(f"\n{'=' * 70}\nfailures: {failures}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    args = p.parse_args()
    return asyncio.run(run(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
