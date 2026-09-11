"""
myScheme API client.

Two measured facts about this API drive the whole design:

1. **It answers HTTP 200 for everything.** A missing scheme is
   `200 {"status":"Success","data":null}`; an invalid page size is
   `200 {"status":"Failure","statusCode":412,...}`. A client that trusts
   `response.status_code` would cheerfully ingest thousands of empty records and
   report total success. So the *envelope* is the source of truth, and
   `_unwrap()` is the only place allowed to decide whether a call worked.

2. **An unknown `lang` silently falls back to English**, and the sole signal is
   that the payload key comes back as `en` instead of what you asked for. So
   `detail()` never assumes; callers check with `language_of()`.

Rate limiting is self-imposed and adaptive, because the API's limit is **not
discoverable by burst testing**. Measured, in order:

    ~172 req/s in short bursts   no throttling at all, at any concurrency
    40 req/s sustained           clean for ~6 minutes, then HTTP 429
    20 req/s sustained           throttled
    12 req/s for 60s (716 reqs)  clean

That is a large burst allowance over a slow refill — a token bucket. A probe
short enough to fit inside the bucket reports "no limit" no matter how fast it
goes, which is how the first full run got itself 429ed. So the ceiling is not a
constant we can look up: `_Pacer` finds it at runtime.
"""
import asyncio
import random
import time
from typing import Any

import httpx

API_ROOT = "https://api.myscheme.gov.in"
SEARCH_PATH = "/search/v6/schemes"
DETAIL_PATH = "/schemes/v6/public/schemes"

# Measured: 100 is accepted, 150 returns statusCode 412. Do not raise this.
MAX_PAGE_SIZE = 100

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class SchemeApiError(RuntimeError):
    """The API refused a call in a way that is worth surfacing."""


class PermanentApiError(SchemeApiError):
    """Our request was wrong. Retrying it verbatim cannot help."""


class AuthRejected(SchemeApiError):
    """The x-api-key was rejected — it has probably rotated."""


class _Pacer:
    """Adaptive rate limiter (AIMD), shared by every worker.

    A semaphore alone bounds *parallelism*, not *rate*: 8 workers each taking
    50ms is 160 req/s. This hands out evenly-spaced departure slots, so the
    ceiling is a number rather than a side effect of latency.

    It adapts because a fixed number cannot find this API's real limit. Measured:
    short bursts at 170 req/s pass completely clean, but ~6 minutes of sustained
    40 req/s earns HTTP 429 — a generous burst allowance over a slow refill,
    which no burst test can reveal. So we halve on throttle and creep back up on
    sustained success.

    The cooldown is shared deliberately: if only the worker that saw the 429
    backed off, the other seven would keep the limit tripped while it waited.
    """

    def __init__(self, rps: float, floor_rps: float = 1.0,
                 reward_after: int = 15) -> None:
        self._target = 1.0 / rps if rps > 0 else 0.0
        self._max_interval = 1.0 / floor_rps if floor_rps > 0 else 1.0
        self._interval = self._target
        # Slowest interval (= fastest rate) we have *never* been throttled at.
        # `reward` may not climb past this, which is what makes the search
        # converge instead of oscillating. Starts at the target: no evidence yet.
        self._safe_interval = self._target
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._cooldown_until = 0.0
        self._ok_streak = 0
        self._reward_after = reward_after
        self.throttle_events = 0

    @property
    def rps(self) -> float:
        return 1.0 / self._interval if self._interval else float("inf")

    async def wait(self) -> None:
        if not self._interval:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    sleep_for = self._cooldown_until - now
                    delay = 0.0
                else:
                    sleep_for = 0.0
                    self._next_slot = max(now, self._next_slot)
                    delay = self._next_slot - now
                    self._next_slot += self._interval
            if sleep_for:
                # Re-check after waking: another worker may have extended it.
                await asyncio.sleep(min(sleep_for, 5.0))
                continue
            if delay > 0:
                await asyncio.sleep(delay)
            return

    async def penalize(self, cooldown_s: float) -> None:
        """Throttled: halve the rate, remember the danger point, pause everyone."""
        if not self._interval:
            return
        async with self._lock:
            # Whatever rate we were just running at is not sustainable, so bar
            # `reward` from ever returning to it. Without this the pacer walks
            # straight back up to the rate that failed and trips again — measured:
            # 213 throttle events in one run, oscillating rather than settling.
            self._safe_interval = max(self._safe_interval, self._interval * 1.25)
            self._interval = min(self._interval * 2.0, self._max_interval)
            self._ok_streak = 0
            self.throttle_events += 1
            self._cooldown_until = max(self._cooldown_until,
                                       time.monotonic() + cooldown_s)
            # Don't let queued slots fire the instant the cooldown lifts.
            self._next_slot = self._cooldown_until

    async def reward(self) -> None:
        """Sustained success: edge back toward the configured target rate.

        Recovery has to be meaningfully faster than decay, or a single early
        throttle pins a multi-hour run at the floor: at 1.5 req/s a 60-success /
        15%-per-step climb needs ~10 minutes to reach 12 req/s, and the run
        spends that time crawling. 15 successes / 25% per step gets there in
        about a minute.

        The ceiling is `_safe_interval`, not the configured target. Climbing all
        the way back to a rate that has already been throttled is how you get
        213 throttle events instead of a rate that settles.
        """
        ceiling = max(self._target, self._safe_interval)
        if not self._interval or self._interval <= ceiling:
            return
        async with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= self._reward_after:
                self._ok_streak = 0
                self._interval = max(ceiling, self._interval * 0.75)


class MySchemeClient:
    """Paced, retrying, envelope-aware client. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        *,
        rps: float = 5.0,
        concurrency: int = 4,
        attempts: int = 8,
        timeout: float = 60.0,
        floor_rps: float = 1.5,
        on_auth_rejected=None,
    ) -> None:
        self._api_key = api_key
        self._pacer = _Pacer(rps, floor_rps=floor_rps)
        self._sem = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._attempts = attempts
        self._timeout = timeout
        self._on_auth_rejected = on_auth_rejected
        self._client: httpx.AsyncClient | None = None
        self.stats: dict[str, int] = {
            "requests": 0, "retries": 0, "null_data": 0, "throttled": 0,
        }

    @property
    def current_rps(self) -> float:
        return self._pacer.rps

    # ── lifecycle ───────────────────────────────────────────────────────
    async def __aenter__(self) -> "MySchemeClient":
        self._client = httpx.AsyncClient(
            base_url=API_ROOT,
            timeout=self._timeout,
            headers={"x-api-key": self._api_key, "User-Agent": UA,
                     "Accept": "application/json", "Origin": "https://www.myscheme.gov.in",
                     "Referer": "https://www.myscheme.gov.in/"},
            # Sized to the semaphore, not aspirationally: idle pooled connections
            # each hold a TLS buffer, and this run was OOM-killed once already.
            limits=httpx.Limits(max_connections=self._concurrency * 2,
                                max_keepalive_connections=self._concurrency),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _refresh_key(self) -> bool:
        """Ask the caller for a new key and re-arm the session with it."""
        if not self._on_auth_rejected or not self._client:
            return False
        new_key = await self._on_auth_rejected()
        if not new_key or new_key == self._api_key:
            return False
        self._api_key = new_key
        self._client.headers["x-api-key"] = new_key
        return True

    # ── the one place that decides success ──────────────────────────────
    @staticmethod
    def _unwrap(body: Any) -> Any:
        """Return `data`, or raise. `None` means 'no such record' — not an error."""
        if not isinstance(body, dict):
            raise SchemeApiError(f"non-object response body: {str(body)[:120]}")

        inner = body.get("statusCode")
        try:
            inner = int(inner) if inner is not None else 200
        except (TypeError, ValueError):
            inner = 200

        if body.get("status") != "Success" or inner != 200:
            desc = body.get("errorDescription") or body.get("error") or ""
            msg = f"envelope statusCode={inner} status={body.get('status')!r} {str(desc)[:160]}"
            # 4xx in the envelope means our request was malformed; retrying is pointless.
            if 400 <= inner < 500:
                raise PermanentApiError(msg)
            raise SchemeApiError(msg)

        return body.get("data")

    async def _get(self, path: str, params: dict) -> Any:
        assert self._client is not None, "use MySchemeClient as an async context manager"
        last_error: Exception | None = None

        for attempt in range(self._attempts):
            if attempt:
                # Exponential backoff with jitter, so parallel workers that hit
                # the same wall don't retry in lockstep.
                await asyncio.sleep(min(2 ** attempt * 0.5, 20.0) * (0.5 + random.random()))
                self.stats["retries"] += 1

            await self._pacer.wait()
            try:
                async with self._sem:
                    resp = await self._client.get(path, params=params)
                self.stats["requests"] += 1
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                continue

            # Transport-level throttling / blocking, distinct from the envelope.
            if resp.status_code in (401, 403):
                if await self._refresh_key():
                    last_error = AuthRejected(f"HTTP {resp.status_code}; key refreshed")
                    continue
                raise AuthRejected(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (429, 500, 502, 503, 504, 509):
                self.stats["throttled"] += 1
                retry_after = resp.headers.get("retry-after")
                # Their throttle window outlasts a short backoff, so slow the
                # shared pacer rather than just retrying this one request faster.
                cooldown = (int(retry_after) if retry_after and retry_after.isdigit()
                            else min(15.0 * (attempt + 1), 90.0))
                await self._pacer.penalize(min(cooldown, 120.0))
                last_error = SchemeApiError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise PermanentApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                body = resp.json()
            except ValueError:
                last_error = SchemeApiError(f"unparseable body: {resp.text[:160]}")
                continue

            try:
                data = self._unwrap(body)
            except PermanentApiError:
                raise
            except SchemeApiError as exc:
                last_error = exc
                continue

            if data is None:
                self.stats["null_data"] += 1
            await self._pacer.reward()
            return data

        raise SchemeApiError(
            f"gave up after {self._attempts} attempts on {path} {params}: {last_error}"
        ) from last_error

    # ── endpoints ───────────────────────────────────────────────────────
    async def search(
        self,
        *,
        offset: int = 0,
        size: int = MAX_PAGE_SIZE,
        lang: str = "en",
        facets: list[dict] | None = None,
        keyword: str = "",
        sort: str = "",
    ) -> dict:
        """One page of the scheme index. `facets` is [{'identifier':..,'value':..}]."""
        if size > MAX_PAGE_SIZE:
            raise ValueError(f"size must be <= {MAX_PAGE_SIZE} (API returns 412 above it)")
        import json as _json
        data = await self._get(SEARCH_PATH, {
            "lang": lang,
            "q": _json.dumps(facets or [], separators=(",", ":")),
            "keyword": keyword,
            "sort": sort,
            "from": str(offset),
            "size": str(size),
        })
        return data or {}

    async def total_schemes(self, *, lang: str = "en",
                            facets: list[dict] | None = None) -> int:
        page = await self.search(offset=0, size=1, lang=lang, facets=facets)
        return int(page.get("summary", {}).get("total", 0))

    async def detail(self, slug: str, lang: str = "en") -> dict | None:
        """Full scheme record. `None` means the slug does not exist."""
        return await self._get(DETAIL_PATH, {"slug": slug, "lang": lang})

    async def documents(self, scheme_id: str, lang: str = "en") -> dict | None:
        """`documents_required` lives on its own sub-resource, keyed by _id."""
        return await self._get(f"{DETAIL_PATH}/{scheme_id}/documents", {"lang": lang})


# ── helpers callers need because of the silent-fallback behaviour ────────
def language_of(data: dict, requested: str) -> str | None:
    """Which language block did we actually get?

    Non-English payloads also carry a *duplicate top-level* `eligibilityCriteria`
    holding the ENGLISH text. Only the block under `data[lang]` is translated, so
    the language keys are found by exclusion rather than by guessing.
    """
    if not isinstance(data, dict):
        return None
    if requested in data:
        return requested
    reserved = {"_id", "slug", "eligibilityCriteria"}
    candidates = [k for k, v in data.items()
                  if k not in reserved and isinstance(v, dict)]
    return candidates[0] if len(candidates) == 1 else None
