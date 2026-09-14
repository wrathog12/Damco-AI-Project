"""
Profiles and conversation memory — the P3 slice-3 verification gate.

Seven things have to hold, and every one of them fails silently if it breaks:

1. **Consent comes before storage.** A logged-in caller who has not agreed to the
   current notice cannot have their income written, and the refusal is
   distinguishable from "log in first" so a client shows the right screen.
2. **Nothing personal is disclosed on a cookie.** Every endpoint under
   `/api/me/*` requires a verified access token. The quota surface still never
   refuses — that separation is the thing being checked, not just the 401s.
3. **The profile round-trips, and it is genuinely encrypted at rest.** The
   strongest available check is done directly against Postgres: the income digits
   the caller saved must not appear anywhere in the stored bytes. A test that only
   reads back through the API would pass just as happily with plaintext columns.
4. **`check_eligibility` works with a scheme id and nothing else.** This is the
   slice's whole payoff. Run in-process through `dispatch`, because that is the
   path both the voice and text loops take, and because a live LLM might simply
   choose to pass the arguments anyway and hide a broken profile lookup.
5. **A conversation is persisted, decryptable, and in order.** Over `/chat` with
   a `conversation_id`, which is the cheap way to exercise what the voice path
   does at hang-up.
6. **`DELETE /api/me` still leaves nothing behind.** Slice 2 proved that for the
   tables that existed then; this slice added five more, and an orphaned
   `user_profiles` row is a DPDP violation no endpoint would ever reveal.
7. **Retention actually deletes.** A documented policy nothing enforces is worse
   than no policy, so the sweep is run and its counts are read back.

Needs the server running with a login it can complete, and it reads the database
directly, so **the key must be in `backend/.env` rather than only on the server's
command line** — this script is a second process and cannot open what it cannot
derive:

    # backend/.env
    PROFILE_ENCRYPTION_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">

    OTP_DEV_ECHO=1 python main.py        # in one shell
    python scripts/verify_profiles.py    # in another

Without the key nothing personal is stored at all — that is the designed
behaviour, not a bug — so this script says so and stops rather than reporting a
wall of failures that all mean "no key". Exit code 0 = clean.
"""
import argparse
import asyncio
import json
import random
import sys
import uuid
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

AUTH = "/api/auth"

# Distinctive enough to be searched for as a literal in a database column. A round
# number like 200000 would risk a false pass by colliding with a length prefix or
# a timestamp byte pattern.
TEST_INCOME = 247319
TEST_AGE = 34


def _fail(msg: str) -> int:
    print(f"  >> FAIL: {msg}")
    return 1


def _phone() -> str:
    """A random Indian mobile, so reruns do not rate-limit each other."""
    return f"+9198{random.randint(0, 99_999_999):08d}"


async def _login(client: httpx.AsyncClient) -> tuple[str, dict[str, str]] | None:
    """Complete a login on `client`. Returns `(phone, auth headers)`."""
    phone = _phone()
    resp = await client.post(f"{AUTH}/request-otp", json={"phone": phone})
    code = resp.json().get("dev_code") if resp.status_code == 200 else None
    if not code:
        print("  >> no dev_code — start the server with OTP_DEV_ECHO=1")
        return None
    session = await client.post(f"{AUTH}/verify-otp",
                                json={"phone": phone, "code": code})
    if session.status_code != 200:
        print(f"  >> login failed: {session.text[:160]}")
        return None
    return phone, {"Authorization": f"Bearer {session.json()['access_token']}"}


# ── 0. is the feature even on? ──────────────────────────
async def check_enabled(base_url: str) -> bool:
    from auth import crypto

    print("=" * 70)
    print("PRIVACY: what the server says it is doing")
    print("=" * 70)
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        health = (await client.get("/health")).json()
    privacy = health.get("privacy") or {}
    print(f"  profiles_enabled    {privacy.get('profiles_enabled')}")
    print(f"  consent_version     {privacy.get('consent_version')}")
    print(f"  retention           {privacy.get('retention')}")
    if not privacy:
        print("  >> /health has no privacy block — this server predates slice 3")
        return False
    if not privacy.get("profiles_enabled"):
        print("  >> PROFILE_ENCRYPTION_KEY is not set on the server, so nothing "
              "personal is stored. That is the designed behaviour with no key; "
              "set one and rerun to verify the feature.")
        return False

    # This process derives its own key from its own environment, and half these
    # checks read the database directly. A key the server has and this script does
    # not would produce "the transcript does not decrypt" — a true statement about
    # the wrong thing. Caught here so it cannot be mistaken for a real failure.
    print(f"  key here            "
          f"{crypto.fingerprint() if crypto.available() else 'NOT SET'}")
    if not crypto.available():
        print("  >> this script has no PROFILE_ENCRYPTION_KEY, so it cannot open "
              "what the server stored. Put the key in backend/.env — the same one "
              "the server is using — rather than only on its command line.")
        return False
    return True


# ── 1. the account surface refuses, the rest does not ───
async def check_auth_gate(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("ACCESS: personal data needs a token; the conversation never does")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        # Establish an anonymous identity first, so these are refusals of a real
        # caller rather than of a request with no identity at all.
        quota = await client.get("/api/quota")
        print(f"  /api/quota anon     status={quota.status_code} "
              f"limit={quota.json().get('limit') if quota.content else '?'}")
        if quota.status_code != 200:
            failures += _fail("/api/quota refused an anonymous caller — the "
                              "conversational surface must never refuse")

        gated = [
            ("GET", "/api/me/profile"),
            ("PUT", "/api/me/profile"),
            ("DELETE", "/api/me/profile"),
            ("DELETE", "/api/me/history"),
            ("GET", "/api/me/eligible"),
            ("POST", "/api/me/consent"),
        ]
        for method, path in gated:
            kwargs = {"json": {"age": TEST_AGE}} if method == "PUT" else {}
            resp = await client.request(method, path, **kwargs)
            error = resp.json().get("error") if resp.content else "?"
            print(f"  {method:6} {path:22} status={resp.status_code} error={error}")
            if resp.status_code != 401:
                failures += _fail(f"{method} {path} answered {resp.status_code} to "
                                  "a caller with only a cookie; a cookie is not "
                                  "proof of a person")

        # A cookie-only request to /api/me is allowed, but must say nothing.
        me = (await client.get("/api/me")).json()
        print(f"  /api/me cookie only authenticated={me['authenticated']} "
              f"phone={me['phone']} has_profile="
              f"{me.get('profile', {}).get('has_profile')}")
        if me["phone"] is not None:
            failures += _fail("/api/me leaked a phone number to a cookie-only "
                              "request")
        if me.get("profile", {}).get("has_profile"):
            failures += _fail("/api/me told a cookie-only request that a profile "
                              "exists")

    return failures


# ── 2. consent gates the write ──────────────────────────
async def check_consent(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("CONSENT: no storage before agreement")
    print("=" * 70)

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        login = await _login(client)
        if login is None:
            return _fail("could not log in")
        _, headers = login

        me = (await client.get("/api/me", headers=headers)).json()
        print(f"  fresh account       consent_current="
              f"{me['consent']['current']} required="
              f"{me['consent']['required_version']}")
        if me["consent"]["current"]:
            failures += _fail("a brand-new account already counts as consented — "
                              "then consent is not being recorded at all")

        # The write must be refused, and refused *distinguishably*.
        blocked = await client.put("/api/me/profile", headers=headers,
                                   json={"income": TEST_INCOME})
        error = blocked.json().get("error") if blocked.content else "?"
        print(f"  save before consent status={blocked.status_code} error={error}")
        if blocked.status_code != 403:
            failures += _fail(f"an income was stored without consent "
                              f"({blocked.status_code}) — that is the DPDP breach "
                              "this check exists for")
        if error != "consent_required":
            failures += _fail(f"the refusal is coded '{error}', not "
                              "'consent_required'; a client cannot tell it apart "
                              "from 'log in first' without matching on prose")

        # Nothing may have been written by the refused call.
        after = (await client.get("/api/me/profile", headers=headers)).json()
        print(f"  profile after       has_profile={after['has_profile']} "
              f"facts={after['facts']}")
        if after["has_profile"] or after["facts"]:
            failures += _fail("the refused write left a profile behind")

        # Consent, then the same write succeeds.
        agreed = await client.post("/api/me/consent", headers=headers)
        print(f"  POST consent        status={agreed.status_code} "
              f"version={agreed.json().get('consent', {}).get('version')}")
        if agreed.status_code != 200:
            failures += _fail(f"recording consent failed ({agreed.status_code})")

        saved = await client.put("/api/me/profile", headers=headers,
                                 json={"income": TEST_INCOME})
        print(f"  save after consent  status={saved.status_code}")
        if saved.status_code != 200:
            failures += _fail(f"the same write failed after consent "
                              f"({saved.status_code}): {saved.text[:160]}")

        # Validation: an unknown field is a 400, not a silent no-op, and an
        # absurd value never reaches the eligibility rules.
        junk = await client.put("/api/me/profile", headers=headers,
                                json={"aadhaar": "1234"})
        print(f"  unknown field       status={junk.status_code} "
              f"error={junk.json().get('error') if junk.content else '?'}")
        if junk.status_code != 400:
            failures += _fail("a field the profile does not hold was accepted; a "
                              "client that thinks it saved an Aadhaar number must "
                              "be told it did not")
        silly = await client.put("/api/me/profile", headers=headers,
                                 json={"age": 900})
        print(f"  age=900             status={silly.status_code}")
        if silly.status_code != 400:
            failures += _fail("an age of 900 was stored; it would produce "
                              "confidently wrong eligibility verdicts")

    return failures


# ── 3. round trip, and encrypted at rest ────────────────
async def check_round_trip(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("STORAGE: the profile round-trips and the bytes on disk are opaque")
    print("=" * 70)

    from sqlalchemy import select

    from models.identity import User
    from models.profile import UserProfile
    from services import resources as resource_factory

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        login = await _login(client)
        if login is None:
            return _fail("could not log in")
        phone, headers = login
        await client.post("/api/me/consent", headers=headers)

        # A patch, not a replacement: two writes, each carrying one fact, and
        # both must survive. This is the shape the voice agent writes in.
        await client.put("/api/me/profile", headers=headers,
                         json={"age": TEST_AGE, "state": "Bihar"})
        second = await client.put("/api/me/profile", headers=headers,
                                  json={"income": TEST_INCOME})
        version = second.json().get("version")
        facts = (await client.get("/api/me/profile", headers=headers)).json()
        print(f"  after two writes    v{version} facts={facts['facts']}")
        for key, want in (("age", TEST_AGE), ("state", "Bihar"),
                          ("income", TEST_INCOME)):
            if facts["facts"].get(key) != want:
                failures += _fail(f"'{key}' came back as "
                                  f"{facts['facts'].get(key)!r}, not {want!r} — a "
                                  "patch replaced the object instead of merging")
        if (version or 0) < 2:
            failures += _fail(f"version is {version} after two writes; the "
                              "eligibility cache is invalidated by that number")

        # Clearing one field must be expressible, and must not clear the rest.
        await client.put("/api/me/profile", headers=headers,
                         json={"state": None})
        cleared = (await client.get("/api/me/profile", headers=headers)).json()
        print(f"  after clearing state facts={cleared['facts']}")
        if "state" in cleared["facts"]:
            failures += _fail("'forget my state' did not forget it")
        if cleared["facts"].get("income") != TEST_INCOME:
            failures += _fail("clearing one field dropped another")

        # ── the real check: read the column, not the API ────
        res = await resource_factory.create()
        try:
            async with res.session_factory() as session:
                user_id = (await session.execute(
                    select(User.id).where(User.phone_e164 == phone))
                ).scalar_one()
                row = (await session.execute(
                    select(UserProfile).where(UserProfile.user_id == user_id))
                ).scalars().first()

            if row is None:
                return failures + _fail("no user_profiles row for a caller who "
                                        "just saved one")

            blob = bytes(row.payload)
            digits = str(TEST_INCOME).encode()
            print(f"  stored payload      {len(blob)} bytes, "
                  f"version_byte=0x{blob[0]:02x}")
            print(f"  income digits in it {digits in blob}")
            if digits in blob:
                failures += _fail(f"the literal digits {TEST_INCOME} are in the "
                                  "stored bytes — the column is not encrypted, "
                                  "whatever the API reports")
            if b"income" in blob or b"Bihar" in blob:
                failures += _fail("field names or values are readable in the "
                                  "stored bytes")
            if blob[:1] != b"\x01":
                failures += _fail(f"the envelope does not start with the version "
                                  f"byte 0x01 (got 0x{blob[0]:02x}); "
                                  "auth/crypto.py cannot read it back")

            # A ciphertext moved to another user must fail to open rather than
            # decrypt into someone else's income. This is what `aad` buys.
            from auth import crypto
            print(f"  opens with own aad  "
                  f"{crypto.decrypt(blob, aad=f'profile:{user_id}') is not None}")
            moved = crypto.decrypt(blob, aad=f"profile:{user_id + 1}")
            print(f"  opens as another    {moved is not None}")
            if moved is not None:
                failures += _fail("a profile row copied to a different user_id "
                                  "still decrypted — the ciphertext is not bound "
                                  "to its owner")
        finally:
            await res.close()

    return failures


# ── 4. check_eligibility with no arguments ──────────────
async def check_profile_driven_eligibility(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("PAYOFF: check_eligibility needs only a scheme id")
    print("=" * 70)

    from sqlalchemy import select

    from models.identity import User
    from models.scheme import Scheme
    from services import conversations as conv_service
    from services import eligibility as eligibility_service
    from services import profiles as profile_service
    from services import resources as resource_factory
    from tools.registry import ToolContext, dispatch

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        login = await _login(client)
        if login is None:
            return _fail("could not log in")
        phone, headers = login
        await client.post("/api/me/consent", headers=headers)
        await client.put("/api/me/profile", headers=headers,
                         json={"age": TEST_AGE, "state": "Bihar",
                               "income": TEST_INCOME, "gender": "Female",
                               "caste": "General"})

    # In-process from here: a live LLM may pass the arguments itself, which would
    # hide a profile lookup that never happens.
    res = await resource_factory.create()
    try:
        async with res.session_factory() as session:
            user = (await session.execute(
                select(User).where(User.phone_e164 == phone))).scalars().one()
            scheme_id = (await session.execute(
                select(Scheme.scheme_id)
                .where(Scheme.delisted_at.is_(None)).limit(1))).scalar_one()

        profile = await profile_service.load(res, user)
        print(f"  profile loaded      v{profile.version if profile else '-'} "
              f"known={list(profile.known) if profile else []}")
        if profile is None:
            return failures + _fail("the profile the API just saved does not load "
                                    "server-side")

        # With a user: the facts come from storage.
        # A bare uuid4: `conversations.id` and `scheme_interactions.conversation_id`
        # are both `String(36)`, which is exactly a uuid and not a byte more.
        ctx = ToolContext(resources=res, user=user,
                          conversation_id=str(uuid.uuid4()))
        result = await dispatch("check_eligibility", {"scheme_id": scheme_id}, ctx)
        print(f"  with a user         eligible={result.get('eligible')} "
              f"used_profile={result.get('used_profile')} "
              f"needs_details={result.get('needs_details')}")
        if "error" in result:
            failures += _fail(f"the call errored: {result['error']}")
        if not result.get("used_profile"):
            failures += _fail("no profile fields were used — the caller's stored "
                              "details are not reaching the rules, which is the "
                              "entire point of this slice")
        if result.get("needs_details"):
            failures += _fail("it asked for details it already had")
        if not result.get("reasons"):
            failures += _fail("a verdict with no reasons; nobody can act on that")

        # An argument stated this turn must beat the stored one.
        override = await dispatch("check_eligibility",
                                  {"scheme_id": scheme_id, "user_state": "Kerala"},
                                  ctx)
        quoted = " ".join(override.get("reasons") or [])
        print(f"  stated state wins   'Kerala' in reasons={'Kerala' in quoted}")
        if "Bihar" in quoted and "Kerala" not in quoted:
            failures += _fail("the stored state overrode one the caller stated "
                              "this turn; a stale fact would be uncorrectable")

        # Without a user: exactly the P2 behaviour, and it must ask rather than
        # invent a verdict out of nothing.
        bare = await dispatch("check_eligibility", {"scheme_id": scheme_id},
                              ToolContext(resources=res))
        print(f"  no user at all      eligible={bare.get('eligible')} "
              f"needs_details={bare.get('needs_details')}")
        if bare.get("eligible") is True:
            failures += _fail("an anonymous caller with no facts was told they "
                              "are eligible — a verdict invented from nothing")
        if not bare.get("needs_details"):
            failures += _fail("it did not ask for the details it lacks")

        # The interaction and the cached verdict must both be there.
        cached = await eligibility_service.cached_count(res, user, profile)
        recent = await conv_service.recent_schemes(res, user)
        print(f"  cached verdicts     {cached}")
        print(f"  recent schemes      {recent}")
        if not recent:
            failures += _fail("the eligibility check was not recorded as an "
                              "interaction, so the next call has no L3 memory")

        # The whole-corpus view, which is what GET /api/me/eligible serves.
        everything = await eligibility_service.evaluate_all(res, user, limit=3)
        print(f"  evaluate_all        {everything['eligible_count']} eligible of "
              f"{everything['total']} (fields={everything['profile_fields']})")
        if everything["total"] == 0:
            failures += _fail("evaluate_all scored nothing — either the corpus is "
                              "empty or the profile did not load")
        if everything["eligible_count"] > everything["total"]:
            failures += _fail("more eligible than exist")

        # A profile save must drop the cache, or a stale verdict outlives its
        # inputs and the reasons quote facts that changed.
        before = await eligibility_service.cached_count(res, user, profile)
        await profile_service.save(res, user, {"age": TEST_AGE + 1})
        fresh = await profile_service.load(res, user)
        after = await eligibility_service.cached_count(res, user, fresh)
        print(f"  cache across a save before={before} after={after} "
              f"(v{profile.version} -> v{fresh.version if fresh else '-'})")
        if after is not None and before is not None and after >= before:
            failures += _fail("the cached verdicts survived a profile change")
    finally:
        await res.close()

    return failures


# ── 5. the conversation is stored ───────────────────────
async def check_conversation(base_url: str) -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("MEMORY: a conversation is persisted, in order, and unreadable at rest")
    print("=" * 70)

    from sqlalchemy import func, select

    from models.identity import User
    from models.profile import Conversation, Message
    from services import conversations as conv_service
    from services import resources as resource_factory

    conversation_id = str(uuid.uuid4())
    said = "Bihar mein kisan ke liye scheme batao"

    async with httpx.AsyncClient(base_url=base_url, timeout=180) as client:
        # An anonymous caller sends an id and must be told, in the response, that
        # nothing is being kept — silence would be indistinguishable from storage.
        anon = await client.post("/chat", json={"message": "hello",
                                               "language": "en",
                                               "conversation_id": conversation_id})
        echoed = anon.json().get("conversation_id")
        print(f"  anonymous /chat     conversation_id={echoed!r}")
        if echoed is not None:
            failures += _fail("an anonymous caller's conversation is being "
                              "persisted; they cannot exercise erasure over it")

        login = await _login(client)
        if login is None:
            return failures + _fail("could not log in")
        phone, headers = login

        first = await client.post("/chat", headers=headers,
                                  json={"message": said, "language": "hi",
                                        "conversation_id": conversation_id})
        body = first.json()
        print(f"  logged-in /chat     status={first.status_code} "
              f"conversation_id={body.get('conversation_id')!r}")
        if first.status_code != 200:
            return failures + _fail(f"/chat failed: {first.text[:200]}")
        if body.get("conversation_id") != conversation_id:
            failures += _fail("/chat did not report the conversation as persisted")

        # A second turn on the same id, so ordering and `seq` are exercised.
        second = await client.post("/chat", headers=headers,
                                   json={"message": "iski eligibility kya hai",
                                         "language": "hi",
                                         "history": body.get("history", []),
                                         "conversation_id": conversation_id})
        if second.status_code != 200:
            failures += _fail(f"the second turn failed: {second.text[:200]}")

        # The prompt text must not have leaked into the history the client holds.
        history = second.json().get("history", [])
        roles = [m.get("role") for m in history]
        print(f"  history roles       {roles}")
        if "system" in roles:
            failures += _fail("a system message is in the history returned to the "
                              "client — our own prompt text is being echoed back "
                              "to us every turn and shown in the transcript")

    res = await resource_factory.create()
    try:
        async with res.session_factory() as session:
            row = (await session.execute(
                select(Conversation).where(Conversation.id == conversation_id))
            ).scalars().first()
            bodies = (await session.execute(
                select(Message.seq, Message.role, Message.body)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq))).all()

        print(f"  conversation row    "
              f"{'present' if row else 'MISSING'} channel="
              f"{row.channel if row else '-'} turns="
              f"{row.turn_count if row else '-'} end={row.end_reason if row else '-'}")
        if row is None:
            return failures + _fail("no conversations row after two persisted "
                                    "turns")
        if row.channel != "text":
            failures += _fail(f"the channel is '{row.channel}', not 'text'")

        seqs = [s for s, _, _ in bodies]
        print(f"  message rows        {len(bodies)} seq={seqs} "
              f"roles={[r for _, r, _ in bodies]}")
        if len(bodies) < 4:
            failures += _fail(f"only {len(bodies)} messages for two turns; a "
                              "transcript missing half a conversation is worse "
                              "than none")
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            failures += _fail(f"seq is not a strictly increasing sequence: {seqs} "
                              "— the transcript's order is not recoverable")

        raw = b"".join(bytes(b) for _, _, b in bodies)
        print(f"  said text in bytes  {said.encode() in raw}")
        if said.encode() in raw:
            failures += _fail("the caller's own words are readable in "
                              "messages.body — transcripts are not encrypted")

        transcript = await conv_service.history(res, conversation_id)
        opened = [m["text"] for m in transcript]
        print(f"  decrypted           {len(transcript)} messages, "
              f"first={opened[0][:48] if opened else '-'!r}")
        if not any(said in text for text in opened):
            failures += _fail("the stored transcript does not decrypt back to "
                              "what was said")
        if any(m["role"] == "system" for m in transcript):
            failures += _fail("a system message was stored as part of the "
                              "transcript")

        # ── erasure of the memory, short of the account ─────
        async with res.session_factory() as session:
            user = (await session.execute(
                select(User).where(User.phone_e164 == phone))).scalars().one()

        removed = await conv_service.erase_all(res, user)
        print(f"  erase_all           {removed}")
        left = await conv_service.history(res, conversation_id)
        print(f"  transcript after    {len(left)} messages")
        if left:
            failures += _fail("the transcript survived erasure")
        async with res.session_factory() as session:
            still = (await session.execute(
                select(func.count()).select_from(User)
                .where(User.id == user.id))).scalar()
        if not still:
            failures += _fail("erasing the history deleted the account too; those "
                              "are three separate rights for a reason")
    finally:
        await res.close()

    return failures


# ── 6. erasure reaches the new tables ───────────────────
async def check_cascade(base_url: str) -> int:
    """`DELETE /api/me` must leave no rows in *any* table, including five new ones.

    Slice 2's erasure test could only prove what existed then. Every table added
    here declares `ondelete="CASCADE"`, which is a promise the database keeps — but
    a promise nothing checks is how a table added later gets missed, and an
    orphaned `user_profiles` row is a DPDP violation that no endpoint would ever
    reveal.
    """
    failures = 0
    print("\n" + "=" * 70)
    print("DPDP: erasure reaches the profile, the transcript and the cache")
    print("=" * 70)

    from sqlalchemy import func, select

    from models.auth import RefreshToken
    from models.identity import User
    from models.profile import (Conversation, EligibilityEvaluation, Message,
                                SchemeInteraction, UserProfile)
    from models.scheme import Scheme
    from services import resources as resource_factory
    from tools.registry import ToolContext, dispatch

    conversation_id = str(uuid.uuid4())
    res = await resource_factory.create()
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=180) as client:
            login = await _login(client)
            if login is None:
                return _fail("could not log in")
            phone, headers = login
            await client.post("/api/me/consent", headers=headers)
            await client.put("/api/me/profile", headers=headers,
                             json={"age": TEST_AGE, "state": "Bihar",
                                   "income": TEST_INCOME})
            await client.post("/chat", headers=headers,
                              json={"message": "Bihar mein kisan scheme batao",
                                    "language": "hi",
                                    "conversation_id": conversation_id})

            async with res.session_factory() as session:
                user = (await session.execute(
                    select(User).where(User.phone_e164 == phone))).scalars().one()
                user_id = user.id
                scheme_id = (await session.execute(
                    select(Scheme.scheme_id)
                    .where(Scheme.delisted_at.is_(None)).limit(1))).scalar_one()

            # An eligibility check is what populates the interaction row *and* the
            # verdict cache, and it is dispatched in-process rather than reached
            # through `GET /api/me/eligible` because that endpoint writes only the
            # cache — interactions are recorded by the tool cores, since an
            # interaction means the caller engaged with a scheme in a conversation
            # and reading a dashboard is not that.
            await dispatch("check_eligibility", {"scheme_id": scheme_id},
                           ToolContext(resources=res, user=user,
                                       conversation_id=conversation_id))
            await client.get("/api/me/eligible", params={"limit": 1},
                             headers=headers)

            async def counts() -> dict[str, int]:
                per_user = (
                    ("users", User, User.id),
                    ("user_profiles", UserProfile, UserProfile.user_id),
                    ("conversations", Conversation, Conversation.user_id),
                    ("scheme_interactions", SchemeInteraction,
                     SchemeInteraction.user_id),
                    ("eligibility_evaluations", EligibilityEvaluation,
                     EligibilityEvaluation.user_id),
                    ("refresh_tokens", RefreshToken, RefreshToken.user_id),
                )
                async with res.session_factory() as session:
                    out = {
                        label: (await session.execute(
                            select(func.count()).select_from(model)
                            .where(column == user_id))).scalar() or 0
                        for label, model, column in per_user
                    }
                    # `messages` hangs off the conversation rather than the user,
                    # so it is two hops from the row being deleted — the one most
                    # likely to be left orphaned.
                    out["messages"] = (await session.execute(
                        select(func.count()).select_from(Message)
                        .where(Message.conversation_id == conversation_id))
                    ).scalar() or 0
                    return out

            before = await counts()
            print(f"  before             {json.dumps(before)}")
            for label in ("user_profiles", "conversations", "messages",
                          "scheme_interactions", "eligibility_evaluations"):
                if not before[label]:
                    failures += _fail(f"nothing in '{label}' to erase, so this "
                                      "check would pass without proving anything")

            gone = await client.delete("/api/me", headers=headers)
            print(f"  DELETE /api/me     status={gone.status_code}")
            if gone.status_code != 200:
                failures += _fail(f"erasure failed ({gone.status_code}): "
                                  f"{gone.text[:160]}")

            after = await counts()
            print(f"  after              {json.dumps(after)}")
            left = {k: v for k, v in after.items() if v}
            if left:
                failures += _fail(f"rows survived erasure: {json.dumps(left)} — "
                                  "every one of those is personal data retained "
                                  "after a deletion request")
    finally:
        await res.close()

    return failures


# ── 7. retention enforces itself ────────────────────────
async def check_retention() -> int:
    failures = 0
    print("\n" + "=" * 70)
    print("RETENTION: the policy is enforced, not merely documented")
    print("=" * 70)

    from services import resources as resource_factory
    from services import retention

    policy = retention.policy()
    print(f"  policy              {json.dumps(policy)}")
    for key in ("conversation_days", "interaction_days",
                "anonymous_identity_days", "sweep_every_hours"):
        if key not in policy:
            failures += _fail(f"the policy does not state '{key}', so /health and "
                              "the privacy notice cannot either")
        elif policy[key] <= 0:
            failures += _fail(f"'{key}' is {policy[key]} — a non-positive window "
                              "would delete everything on the next sweep")

    res = await resource_factory.create()
    try:
        pending = await retention.pending(res)
        purged = await retention.purge(res)
        after = await retention.pending(res)
        print(f"  pending before      {json.dumps(pending)}")
        print(f"  purged              {json.dumps(purged)}")
        print(f"  pending after       {json.dumps(after)}")

        failed = [k for k, v in purged.items() if v < 0]
        if failed:
            failures += _fail(f"these steps errored: {', '.join(failed)}")
        for key, count in after.items():
            if count > 0:
                failures += _fail(f"{count} rows are still past their retention "
                                  f"window in '{key}' after a sweep")
    finally:
        await res.close()

    return failures


async def run(base_url: str) -> int:
    if not await check_enabled(base_url):
        print(f"\n{'=' * 70}\nnot verified — see above")
        return 1

    failures = 0
    failures += await check_auth_gate(base_url)
    failures += await check_consent(base_url)
    failures += await check_round_trip(base_url)
    failures += await check_profile_driven_eligibility(base_url)
    failures += await check_conversation(base_url)
    failures += await check_cascade(base_url)
    failures += await check_retention()
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
