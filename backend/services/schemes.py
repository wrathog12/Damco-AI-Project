"""
The scheme data layer — everything the five tools used to get from a JSON file.

Shape of a read, and why it is two hops rather than one:

    Qdrant ranks (hybrid dense+sparse, hard filters applied per-prefetch)
      -> ordered list of scheme ids
      -> Postgres supplies the actual field values

Qdrant's payload holds only what filtering needs. Resisting the temptation to
put the full record there is deliberate: Postgres is the source of truth, and a
second copy of `description` in the index is a second thing to keep in sync. The
extra hop is a local indexed lookup of at most `limit` rows — single-digit
milliseconds against a ~600 ms embedding call, so it is not where the time goes.

Two invariants carried over from v1 and worth restating because they are easy to
lose in a rewrite:

* **NULL means unspecified and must never exclude.** Enforced on the retrieval
  side by `qdrant_index._null_or`, and again here in `evaluate_eligibility`,
  which skips any check whose scheme-side value is NULL.
* **Filters are hard constraints; the vector only ranks within them.** So a
  Bihar query cannot surface a Maharashtra scheme even if it is a perfect
  semantic match.

Every public function takes the `Resources` it should read through as its first
argument. Nothing here reaches for a global: the handlers in `tools/` get theirs
from Pipecat's `app_resources`, and `/chat` from the FastAPI app state.
"""
import re
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select

from embeddings.document import build_query_text
from models import Scheme
from search import qdrant_index as qi
from search.sparse import encode_query
from services.resources import Resources

# v1 clipped the description to 150 characters before handing it to the LLM, to
# keep the tool result inside a few hundred tokens. Same budget: the corpus is
# 10x bigger now, so the pressure to stay terse is greater, not smaller.
_DESC_CHARS = 150
_BENEFIT_CHARS = 80
# Ask Qdrant for more than we return, because the ranked ids get re-joined to
# Postgres and a de-listed or freshly deleted row would otherwise shorten the
# result list below `limit`.
_OVERFETCH = 4


_MARKDOWN = re.compile(r"[*_#`>\[\]]+")


def _plain(text: str | None) -> str:
    """Strip markdown before the text reaches the LLM.

    The corpus is harvested from markdown fields, so a benefit reads
    `"> **Financial Assistance** ..."`. Left in, the model reads the asterisks
    aloud or echoes them into a spoken answer. The card keeps the raw text — the
    frontend is what should render it — so this is applied only on the LLM path.
    """
    return " ".join(_MARKDOWN.sub("", text or "").split())


def _clip(text: str | None, budget: int) -> str:
    text = _plain(text)
    return (text[:budget] + "…") if len(text) > budget else text


def _display_state(state: str | None) -> str | None:
    """`"All"` is how myScheme marks a central scheme, and it is not a place.

    Handed to the LLM verbatim it produces "this scheme is available in All".
    """
    return "All India" if state == "All" else state


def _f(value: Any) -> Any:
    """Numeric(14,2) arrives as Decimal, which is not JSON-serialisable."""
    return float(value) if isinstance(value, Decimal) else value


# ── label normalisation ─────────────────────────────────────────────────
# Central schemes carry `state="All"` and no `states`, so they cannot be reached
# by a state filter at all. The prompt teaches the LLM to say "Central
# Government"; that has to become a `level` constraint instead.
_CENTRAL_ALIASES = {"central", "central government", "centre", "center",
                    "government of india", "goi", "union government",
                    "kendriya", "national", "all india", "india", "all"}
_STOP_TOKENS = {"and", "of", "the", "scheme", "schemes", "department",
                "ministry", "govt", "government"}

# The semantic jumps `_canonicalise` cannot make from token overlap alone:
# "farming" shares no word with "Agriculture,Rural & Environment" and "kisan"
# shares none with "Farmer".
#
# v1 taught these to the model as a 40-line table inside the system prompt. That
# cost ~600 tokens on *every* call — and on the then-current Groq free tier of
# 8,000 tokens/minute, a single three-round turn spent the whole minute's budget
# and the next call was throttled 24-44s with no error anywhere in the logs.
# Billing is pay-as-you-go on Gemini now so the cliff is gone, but the tokens are
# not free and the argument still holds: resolving them here is deterministic,
# and cannot drift out of step with the corpus the way a prompt table does (see
# `_canonicalise`).
#
# Keyed per field, which the prompt table was not: "student" means the Education
# category when it arrives as `category` and the string "Student" when it arrives
# as `occupation`. One flat table gets that wrong in one direction or the other.
_ALIASES: dict[str, dict[str, str]] = {
    "category": {
        **{k: "Agriculture,Rural & Environment" for k in
           ("farming", "farmer", "kisan", "krishi", "kheti", "crop", "fasal",
            "agriculture", "rural", "irrigation", "sinchai")},
        **{k: "Social welfare & Empowerment" for k in
           ("pension", "vridha", "widow", "vidhwa", "disability", "divyang",
            "old age", "welfare", "social welfare", "senior citizen")},
        **{k: "Banking,Financial Services and Insurance" for k in
           ("loan", "bank", "banking", "karza", "karz", "insurance", "bima",
            "credit", "finance", "financial", "subsidy")},
        **{k: "Women and Child" for k in
           ("women", "woman", "mahila", "ladki", "beti", "kanya", "bachcha",
            "child", "girl", "maternity", "pregnancy")},
        **{k: "Skills & Employment" for k in
           ("skill", "skills", "training", "rozgar", "naukri", "job",
            "employment", "prashikshan", "apprentice", "internship")},
        **{k: "Housing & Shelter" for k in
           ("house", "housing", "ghar", "awas", "makan", "shelter", "home")},
        **{k: "Business & Entrepreneurship" for k in
           ("business", "udyog", "vyapar", "startup", "entrepreneur",
            "enterprise", "msme", "shop", "dukan")},
        **{k: "Education & Learning" for k in
           ("education", "padhai", "shiksha", "scholarship", "chhatravritti",
            "study", "school", "college", "learning", "fees", "coaching")},
        **{k: "Health & Wellness" for k in
           ("health", "swasthya", "hospital", "ilaj", "medical", "treatment",
            "medicine", "surgery", "wellness")},
        **{k: "Science, IT & Communications" for k in
           ("computer", "internet", "technology", "tech", "science", "digital",
            "laptop", "communications")},
        **{k: "Public Safety,Law & Justice" for k in
           ("police", "court", "kanoon", "legal", "legal aid", "law",
            "justice", "safety")},
        **{k: "Utility & Sanitation" for k in
           ("water", "pani", "toilet", "sauchalay", "bijli", "electricity",
            "sanitation", "utility", "lpg", "gas")},
        **{k: "Transport & Infrastructure" for k in
           ("road", "bus", "transport", "parivahan", "infrastructure",
            "vehicle", "cycle", "bicycle")},
        **{k: "Travel & Tourism" for k in ("travel", "tourism", "paryatan")},
        **{k: "Sports & Culture" for k in
           ("sport", "sports", "khel", "art", "culture", "sanskriti",
            "athlete", "music")},
    },
    # Abbreviations, and the Devanagari names Deepgram returns when a caller says
    # a state aloud in Hindi. `_tokens` strips non-ASCII, so without these a
    # Devanagari state name canonicalises to itself and the hard filter — working
    # exactly as cross-cutting rule 3 requires — matches nothing at all.
    "state": {
        "up": "Uttar Pradesh", "u.p.": "Uttar Pradesh",
        "mp": "Madhya Pradesh", "m.p.": "Madhya Pradesh",
        "wb": "West Bengal", "bengal": "West Bengal",
        "mh": "Maharashtra", "tn": "Tamil Nadu", "ap": "Andhra Pradesh",
        "hp": "Himachal Pradesh", "jk": "Jammu and Kashmir",
        "j&k": "Jammu and Kashmir", "uttaranchal": "Uttarakhand",
        "orissa": "Odisha", "ncr": "Delhi", "pondicherry": "Puducherry",
        "बिहार": "Bihar", "महाराष्ट्र": "Maharashtra",
        "उत्तर प्रदेश": "Uttar Pradesh", "मध्य प्रदेश": "Madhya Pradesh",
        "पश्चिम बंगाल": "West Bengal", "राजस्थान": "Rajasthan",
        "गुजरात": "Gujarat", "पंजाब": "Punjab", "हरियाणा": "Haryana",
        "झारखंड": "Jharkhand", "दिल्ली": "Delhi", "কর্ণাটক": "Karnataka",
        "বাংলা": "West Bengal", "পশ্চিমবঙ্গ": "West Bengal",
    },
    "occupation": {
        **{k: "Farmer" for k in
           ("kisan", "farming", "kheti", "krishi", "cultivator", "किसान")},
        **{k: "Student" for k in
           ("vidyarthi", "chhatra", "studying", "scholar", "छात्र", "विद्यार्थी")},
        **{k: "Worker" for k in
           ("mazdoor", "shramik", "labour", "labourer", "labor", "मजदूर",
            "श्रमिक")},
        **{k: "Unemployed" for k in ("berozgar", "jobless", "बेरोजगार")},
        **{k: "Widow" for k in ("vidhwa", "विधवा")},
        **{k: "Teacher" for k in ("shikshak", "शिक्षक")},
    },
    # `gender` and `caste` are exact `MatchAny` keyword filters, unlike
    # `occupation`'s full-text index — so case is load-bearing here and a
    # lowercase "female" matches nothing at all. The plain English words are in
    # the table for that reason alone.
    "gender": {
        **{k: "Female" for k in
           ("female", "mahila", "ladki", "aurat", "stri", "woman", "women",
            "girl", "beti", "f", "महिला", "लड़की", "स्त्री")},
        **{k: "Male" for k in
           ("male", "purush", "ladka", "aadmi", "admi", "man", "men", "boy",
            "beta", "m", "पुरुष", "लड़का")},
        **{k: "All" for k in ("all", "any", "both", "either", "everyone")},
    },
    "caste": {
        **{k: "SC" for k in ("sc", "scheduled caste", "dalit", "anusuchit jati")},
        **{k: "ST" for k in ("st", "scheduled tribe", "adivasi", "tribal")},
        **{k: "OBC" for k in ("obc", "other backward class", "pichhda varg",
                              "backward")},
        **{k: "General" for k in ("general", "gen", "unreserved", "samanya")},
        **{k: "EWS" for k in ("ews", "economically weaker section")},
    },
}


def _dealias(field: str, value: str | None) -> str | None:
    """`("occupation", "kisan")` → `"Farmer"`. Unknown values pass through."""
    if not value:
        return value
    return _ALIASES.get(field, {}).get(value.strip().lower(), value)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower())
            if t and t not in _STOP_TOKENS}


def _canonicalise(value: str, options: Sequence[str],
                  *, threshold: float = 0.6) -> str:
    """Map an LLM-supplied label onto one the corpus actually uses.

    The Qdrant filters are keyword matches, so v1's labels now match *nothing*:
    myScheme calls them "Social welfare & Empowerment" (lowercase w),
    "Agriculture,Rural & Environment" and "Women and Child", not "Social Welfare
    & Empowerment", "Agriculture & Rural Development" and "Women & Child
    Development". Resolving near-misses here means the prompt table drifting out
    of step with the corpus degrades ranking instead of returning zero rows.

    An unresolvable value is returned **unchanged**, so the filter matches
    nothing and the LLM retries with fewer filters (prompt rule 4). Silently
    *dropping* the filter would be worse: cross-cutting rule 3 says a Bihar query
    must never answer with Maharashtra schemes.

    The 0.6 floor is what separates "Women & Child Development" -> "Women and
    Child" (2 of 3 tokens) from "Andhra Pradesh" -> "Arunachal Pradesh" (1 of 2).
    """
    wanted = _tokens(value)
    if not wanted:
        return value
    for option in options:
        if option.strip().lower() == value.strip().lower():
            return option
    best, best_score = value, 0.0
    for option in options:
        score = len(wanted & _tokens(option)) / len(wanted)
        if score > best_score:
            best, best_score = option, score
    return best if best_score >= threshold else value


async def _vocabulary(res: Resources) -> dict[str, tuple[str, ...]]:
    """The corpus' own `state` and `category` labels, cached on `res`.

    Read from Postgres rather than hardcoded so that finishing the harvest — it
    is partway through — cannot leave a stale list behind. The lock matters
    because concurrent callers are now the normal case: every voice session
    shares one `Resources`, and the first query of each would otherwise run this
    scan again.
    """
    if res.vocabulary is not None:
        return res.vocabulary
    async with res.lock:
        if res.vocabulary is not None:
            return res.vocabulary
        async with res.session_factory() as session:
            states = [s for s, in (await session.execute(
                select(Scheme.state).where(Scheme.state.is_not(None))
                .group_by(Scheme.state))).all()]
            categories = [c for c, in (await session.execute(
                select(Scheme.category).where(Scheme.category.is_not(None))
                .group_by(Scheme.category))).all()]
        res.vocabulary = {"state": tuple(sorted(states)),
                          "category": tuple(sorted(categories))}
        return res.vocabulary


async def _resolve_scope(res: Resources, state: str | None,
                         category: str | None) -> tuple[str | None, str | None,
                                                        str | None]:
    """`(state, level, category)` as the corpus spells them."""
    level: str | None = None
    # Aliases first: "MP" and "kheti" have to become "Madhya Pradesh" and a
    # category before there is anything for the fuzzy match to work on.
    state = _dealias("state", state)
    category = _dealias("category", category)
    if state and state.strip().lower() in _CENTRAL_ALIASES:
        state, level = None, "Central"
    vocab = await _vocabulary(res)
    if state:
        state = _canonicalise(state, vocab["state"])
    if category:
        category = _canonicalise(category, vocab["category"])
    return state, level, category


# ── search ──────────────────────────────────────────────────────────────
def _implied_query(*, category: str | None, state: str | None,
                   occupation: str | None, gender: str | None,
                   caste: str | None, age: int | None) -> str:
    """Query text for a call that arrived with filters but no words.

    `search_schemes` has always been callable with structured filters alone, and
    that path still has to rank. Without something to embed the best available
    answer would be an arbitrary slice of whatever matched the filter, which is
    worse than a weak ranking. The filters are enforced by Qdrant either way —
    this string only decides the order within them.
    """
    bits = ["government welfare schemes"]
    if occupation:
        bits.append(f"for {occupation}")
    if gender and gender != "All":
        bits.append(f"for {gender}")
    if caste and caste != "All":
        bits.append(f"for {caste} category")
    if age is not None:
        bits.append(f"age {age}")
    return build_query_text(" ".join(bits), state=state, category=category)


async def _rank(res: Resources, *, text: str, query_filter,
                limit: int) -> list[str]:
    """Ordered scheme ids for a query. Falls back to filter-only on no ranker."""
    if text and res.embedder is not None:
        dense = await res.embedder.embed_query(text)
        indices, values = encode_query(text)
        points = await qi.hybrid_search(
            res.qdrant, res.collection, dense=dense,
            sparse_indices=indices, sparse_values=values,
            query_filter=query_filter, limit=limit)
    else:
        # No embedder (missing key) or nothing to embed. Structured lookups still
        # have to work, so scroll the filter unranked rather than return nothing.
        points, _ = await res.qdrant.scroll(
            collection_name=res.collection, scroll_filter=query_filter,
            limit=limit, with_payload=True, with_vectors=False)
    return [p.payload["scheme_id"] for p in points
            if p.payload and p.payload.get("scheme_id")]


async def _hydrate(res: Resources, scheme_ids: Sequence[str]) -> list[Scheme]:
    """Fetch the ranked schemes from Postgres, preserving Qdrant's order."""
    if not scheme_ids:
        return []
    async with res.session_factory() as session:
        rows = (await session.execute(
            select(Scheme).where(Scheme.scheme_id.in_(list(scheme_ids)))
        )).scalars().all()
    by_id = {s.scheme_id: s for s in rows}
    return [by_id[sid] for sid in scheme_ids if sid in by_id]


def _summary(scheme: Scheme) -> dict[str, Any]:
    """The compact per-result shape the LLM sees. Unchanged from v1."""
    return {
        "scheme_id": scheme.scheme_id,
        "scheme_name": scheme.scheme_name,
        "category": scheme.category,
        "state": _display_state(scheme.state),
        "description": _clip(scheme.description, _DESC_CHARS),
    }


async def search(
    res: Resources,
    *,
    query: str | None = None,
    state: str | None = None,
    category: str | None = None,
    age: int | None = None,
    gender: str | None = None,
    occupation: str | None = None,
    income: int | None = None,
    caste: str | None = None,
    disability: bool | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    state, level, category = await _resolve_scope(res, state, category)
    # These two go straight into the hard filter, so an un-aliased "kisan" would
    # match no scheme's occupation and quietly empty the result set.
    gender = _dealias("gender", gender)
    occupation = _dealias("occupation", occupation)
    caste = _dealias("caste", caste)
    query_filter = qi.eligibility_filter(
        state=state, category=category, level=level, age=age, income=income,
        gender=gender, caste=caste, occupation=occupation,
        disability=disability)

    text = (query or "").strip() or _implied_query(
        category=category, state=state, occupation=occupation,
        gender=gender, caste=caste, age=age)
    if query:
        # State and category go in as weak semantic hints only; the hard filter
        # above is what actually constrains them.
        text = build_query_text(text, state=state, category=category)

    ids = await _rank(res, text=text, query_filter=query_filter,
                      limit=limit + _OVERFETCH)
    schemes = await _hydrate(res, ids)
    return [_summary(s) for s in schemes[:limit]]


# ── details ─────────────────────────────────────────────────────────────
async def _by_id(res: Resources, scheme_id: str) -> Scheme | None:
    async with res.session_factory() as session:
        return (await session.execute(
            select(Scheme).where(Scheme.scheme_id == scheme_id)
        )).scalars().first()


async def details(res: Resources, scheme_id: str) -> dict[str, Any] | None:
    """A ~300-token summary for the LLM to speak from.

    Everything the frontend card needs lives in `card_payload` instead — putting
    documents, the full application process and translations in front of the LLM
    only spends context it will not read aloud.
    """
    scheme = await _by_id(res, scheme_id)
    if scheme is None:
        return None

    compact_benefits = []
    for b in (scheme.benefits or [])[:2]:
        entry: dict[str, Any] = {}
        if isinstance(b, dict):
            if b.get("amount"):
                entry["amount"] = b["amount"]
            entry["description"] = _clip(str(b.get("description") or ""),
                                         _BENEFIT_CHARS)
        else:
            entry["description"] = _clip(str(b), _BENEFIT_CHARS)
        if entry.get("description"):
            compact_benefits.append(entry)

    return {
        "scheme_id": scheme.scheme_id,
        "scheme_name": scheme.scheme_name,
        "state": _display_state(scheme.state),
        "category": scheme.category,
        "description": _clip(scheme.description, _DESC_CHARS),
        "top_benefits": compact_benefits,
        "eligibility_summary": _clip(scheme.eligibility_description, _DESC_CHARS),
        "has_application_url": bool(scheme.application_url),
    }


# ── card payload ────────────────────────────────────────────────────────
def _process_text(process: Any) -> str:
    """`[{mode, url, steps[]}]` -> the plain text the card renders.

    v1 stored one string here and `SchemeCard.tsx` renders it `whitespace-pre-
    wrap`; v2's API gives structured modes and steps. Flattened rather than
    reshaped, because the card contract is frozen until P4.
    """
    if isinstance(process, str):
        return process
    if not isinstance(process, list):
        return ""
    blocks = []
    for entry in process:
        if isinstance(entry, str):
            blocks.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        head = str(entry.get("mode") or "").strip()
        steps = [str(s).strip() for s in (entry.get("steps") or []) if s]
        body = "\n".join(steps)
        blocks.append(f"{head}:\n{body}" if head and body else (body or head))
    return "\n\n".join(b for b in blocks if b)


def _benefit_list(benefits: Any) -> list[dict[str, Any]]:
    """Normalise to the `{amount?, description}` objects the card expects."""
    out = []
    for b in benefits or []:
        if isinstance(b, dict):
            out.append({k: v for k, v in b.items()
                        if k in ("amount", "description") and v is not None})
        elif b:
            out.append({"description": str(b)})
    return [b for b in out if b.get("description")]


def _translation_block(row) -> dict[str, Any]:
    """Only the keys we actually have a translation for.

    `SchemeCard.tsx`'s `t()` falls back to English on a missing *or empty* key,
    so omitting is strictly better than emitting `""` — and myScheme does not
    translate `benefits` or the application process, so those stay absent.
    """
    block = {
        "scheme_name": row.scheme_name,
        "description": row.description,
        "eligibility_description": row.eligibility_description,
        "documents_required": row.documents_required,
    }
    return {k: v for k, v in block.items() if v}


async def card_payload(res: Resources, scheme_id: str) -> dict[str, Any] | None:
    """The full record, in the v1 knowledge-base shape the frontend still reads.

    Reshaping the card is a P4 item, so this adapts v2's columns to the v1 keys
    (`metadata.state`, `eligibility{}`, `translations{lang}`) rather than
    changing the wire format underneath a frontend nobody has touched yet.
    """
    scheme = await _by_id(res, scheme_id)
    if scheme is None:
        return None

    return {
        "scheme_id": scheme.scheme_id,
        "scheme_name": scheme.scheme_name,
        "metadata": {
            "state": _display_state(scheme.state),
            "category": scheme.category,
            "subcategory": (scheme.subcategories or [None])[0],
            "tags": scheme.tags or [],
            "level": scheme.level,
        },
        "ministry": scheme.nodal_ministry or scheme.nodal_department,
        "description": scheme.description or scheme.detailed_description,
        "objectives": scheme.objectives or [],
        "benefits": _benefit_list(scheme.benefits),
        "eligibility": {
            "age_min": scheme.age_min,
            "age_max": scheme.age_max,
            "income_max": _f(scheme.income_max),
            "gender": scheme.gender,
            "caste": scheme.caste,
            "occupation": scheme.occupation,
            "disability": scheme.disability,
            "bpl_card": scheme.bpl_card,
            "state_residence": scheme.state_residence or scheme.states,
        },
        "eligibility_description": scheme.eligibility_description,
        "application_process": _process_text(scheme.application_process),
        "documents_required": scheme.documents_required or [],
        "application_url": scheme.application_url,
        "helpline": scheme.helpline,
        "source_url": scheme.source_url,
        "translations": {t.lang: _translation_block(t)
                         for t in scheme.translations
                         if _translation_block(t)},
    }


# ── eligibility: deterministic rules, never similarity ──────────────────
def _matches_any(value: str, options: Iterable[str] | None) -> bool:
    """Case-insensitive membership, with "All" always matching."""
    if not options:
        return True
    lowered = [str(o).strip().lower() for o in options if o]
    return (not lowered
            or "all" in lowered
            or value.strip().lower() in lowered)


async def evaluate_eligibility(
    res: Resources,
    scheme_id: str,
    **facts: Any,
) -> dict[str, Any] | None:
    """`{eligible, scheme_id, scheme_name, reasons[]}` — the v1 contract.

    Rules evaluation against typed columns, never vector similarity: the plan is
    explicit that a verdict a citizen may act on cannot come from a ranking. Any
    check whose scheme-side value is NULL is skipped entirely, because telling
    someone they are ineligible on the strength of missing data is the one
    failure this system must not produce.

    One database read, then `evaluate_scheme`. The split exists so that
    `services/eligibility.py` can score the whole corpus from a single `SELECT`
    instead of 1,786 lookups — the rules themselves have no reason to touch the
    database, and keeping them in one function is what stops the bulk path and the
    single-scheme path drifting into two different verdicts for one caller.
    """
    scheme = await _by_id(res, scheme_id)
    if scheme is None:
        return None
    return evaluate_scheme(scheme, **facts)


def evaluate_scheme(
    scheme: Scheme,
    *,
    user_age: int | None = None,
    user_gender: str | None = None,
    user_state: str | None = None,
    user_income: int | None = None,
    user_occupation: str | None = None,
    user_caste: str | None = None,
    user_disability: bool | None = None,
    user_bpl_card: bool | None = None,
) -> dict[str, Any]:
    """The rules, against a scheme already loaded. Pure and synchronous."""
    # Same normalisation as `search`, for the same reason: a caller who says
    # "main kisan hoon" must not be told a farmer scheme targets someone else.
    user_gender = _dealias("gender", user_gender)
    user_occupation = _dealias("occupation", user_occupation)
    user_state = _dealias("state", user_state)
    user_caste = _dealias("caste", user_caste)

    reasons: list[str] = []
    eligible = True

    if user_age is not None:
        if scheme.age_min is not None and user_age < scheme.age_min:
            eligible = False
            reasons.append(f"Age {user_age} is below minimum {scheme.age_min}")
        elif scheme.age_max is not None and user_age > scheme.age_max:
            eligible = False
            reasons.append(f"Age {user_age} is above maximum {scheme.age_max}")
        elif scheme.age_min is None and scheme.age_max is None:
            reasons.append("Age: ✓ no age limit stated")
        else:
            reasons.append(f"Age {user_age}: ✓ eligible")

    if user_gender:
        if not scheme.gender:
            reasons.append("Gender: ✓ open to all")
        elif _matches_any(user_gender, scheme.gender):
            reasons.append(f"Gender '{user_gender}': ✓ eligible")
        else:
            eligible = False
            reasons.append(f"Gender '{user_gender}' not eligible "
                           f"(requires {list(scheme.gender)})")

    if user_state:
        # `states` is authoritative for coverage; `state_residence` is the
        # narrower residency requirement when one was stated. An empty list means
        # nationwide, not "no state qualifies".
        required = [str(r) for r in (scheme.state_residence or scheme.states or [])]
        # Canonicalised against the scheme's own list first: "Jammu & Kashmir"
        # against a stored "Jammu and Kashmir" would otherwise be reported as
        # *ineligible*, which is the one answer this system must not get wrong.
        resolved = _canonicalise(user_state, required) if required else user_state
        if not required:
            reasons.append(f"State '{user_state}': ✓ nationwide scheme")
        elif _matches_any(resolved, required):
            reasons.append(f"State '{user_state}': ✓ eligible")
        else:
            eligible = False
            reasons.append(f"State '{user_state}' not in eligible states: "
                           f"{list(required)}")

    if user_income is not None:
        cap = _f(scheme.income_max)
        if cap is None:
            reasons.append("Income: ✓ no income limit stated")
        elif user_income > cap:
            eligible = False
            reasons.append(f"Income ₹{user_income:,} exceeds maximum ₹{cap:,.0f}")
        else:
            reasons.append(f"Income ₹{user_income:,}: ✓ within limit ₹{cap:,.0f}")

    if user_occupation:
        # v1 accepted this argument and then ignored it. Reported rather than
        # enforced: `Scheme.occupation` is regex-derived from prose and only
        # ~20% populated, so a mismatch is far more likely to mean "our parse is
        # thin" than "this citizen does not qualify". Never flips `eligible`.
        stated = (scheme.occupation or "").strip()
        if not stated:
            reasons.append("Occupation: not specified by the scheme")
        elif user_occupation.strip().lower() in stated.lower():
            reasons.append(f"Occupation '{user_occupation}': ✓ eligible")
        else:
            reasons.append(f"Occupation: scheme targets {stated} — "
                           f"confirm whether '{user_occupation}' qualifies")

    if user_caste:
        # Enforced, unlike occupation, and that asymmetry is deliberate: `caste`
        # is a curated keyword array from myScheme's own eligibility facets, not a
        # regex guess at prose, so a mismatch really does mean the scheme is
        # reserved for another category. It became necessary the moment the
        # profile started carrying caste — a stored "General" silently ignored
        # here would have the agent telling a General-caste caller they qualify
        # for an SC-only scholarship, which is worse than not knowing their caste
        # at all. Empty array still means "open to all" (rule: NULL does not
        # exclude), and `_matches_any` honours a literal "All".
        if not scheme.caste:
            reasons.append("Caste category: ✓ open to all")
        elif _matches_any(user_caste, scheme.caste):
            reasons.append(f"Caste category '{user_caste}': ✓ eligible")
        else:
            eligible = False
            reasons.append(f"Caste category '{user_caste}' not eligible "
                           f"(reserved for {list(scheme.caste)})")

    if user_disability is not None:
        # `Scheme.disability` is tri-state and only `True` is a requirement:
        # `None` means the corpus says nothing and `False` means the scheme is not
        # disability-specific — neither is a reason to exclude anybody. So a
        # caller *with* a disability is never made ineligible by this check; only
        # a caller without one, against a scheme that requires one.
        if scheme.disability is not True:
            reasons.append("Disability: not a requirement of this scheme")
        elif user_disability:
            reasons.append("Disability: ✓ this scheme is for persons with "
                           "disabilities")
        else:
            eligible = False
            reasons.append("This scheme is only for persons with disabilities")

    if user_bpl_card is not None:
        # Same tri-state reading as `disability`.
        if scheme.bpl_card is not True:
            reasons.append("BPL card: not a requirement of this scheme")
        elif user_bpl_card:
            reasons.append("BPL card: ✓ required and held")
        else:
            eligible = False
            reasons.append("This scheme requires a BPL ration card")

    if scheme.delisted_at is not None:
        eligible = False
        reasons.append("This scheme is no longer listed on myScheme")

    return {
        "eligible": eligible,
        "scheme_id": scheme.scheme_id,
        "scheme_name": scheme.scheme_name,
        "reasons": reasons,
    }


# ── stats (the /health endpoint) ────────────────────────────────────────
async def stats(res: Resources) -> dict[str, Any]:
    async with res.session_factory() as session:
        total = (await session.execute(
            select(func.count()).select_from(Scheme))).scalar() or 0
        states = [s for s, in (await session.execute(
            select(Scheme.state).where(Scheme.state.is_not(None))
            .group_by(Scheme.state).order_by(Scheme.state))).all()]
        categories = [c for c, in (await session.execute(
            select(Scheme.category).where(Scheme.category.is_not(None))
            .group_by(Scheme.category).order_by(Scheme.category))).all()]

    indexed = None
    try:
        info = await res.qdrant.get_collection(res.collection)
        indexed = info.points_count
    except Exception:                                         # noqa: BLE001
        # A missing collection is a real state to report, not a 500 on /health.
        pass

    return {
        "total_schemes": total,
        "states": states,
        "categories": categories,
        "indexed_schemes": indexed,
        "semantic_search": res.embedder is not None,
    }
