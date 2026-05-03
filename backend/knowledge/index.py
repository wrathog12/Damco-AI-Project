"""
In-memory search / filter over the loaded knowledge base.
All filtering is optional and additive (AND logic).
"""
from knowledge import loader


def filter_schemes(
    state: str | None = None,
    category: str | None = None,
    age: int | None = None,
    gender: str | None = None,
    occupation: str | None = None,
    income: int | None = None,
    caste: str | None = None,
    disability: bool | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Filter schemes from the in-memory KB.
    Returns compact dicts: {scheme_id, scheme_name, category, state, description}.
    """
    results = []

    for s in loader.get_all():
        meta = s.get("metadata", {})
        elig = s.get("eligibility", {})

        # ── State filter ────────────────────────────────
        if state:
            s_state = (meta.get("state") or "").lower()
            if state.lower() not in s_state and s_state not in state.lower():
                continue

        # ── Category filter ─────────────────────────────
        if category:
            s_cat = (meta.get("category") or "").lower()
            if category.lower() not in s_cat:
                continue

        # ── Age filter ──────────────────────────────────
        if age is not None:
            age_min = elig.get("age_min")
            age_max = elig.get("age_max")
            if age_min is not None and age < age_min:
                continue
            if age_max is not None and age > age_max:
                continue

        # ── Gender filter ───────────────────────────────
        if gender:
            s_gender = elig.get("gender", ["All"])
            if isinstance(s_gender, list):
                if "All" not in s_gender and gender not in s_gender:
                    continue

        # ── Occupation filter (fuzzy) ───────────────────
        if occupation:
            s_occ = str(elig.get("occupation") or "").lower()
            if occupation.lower() not in s_occ and s_occ not in ("", "null"):
                # Also check tags
                tags = meta.get("tags", [])
                if not any(occupation.lower() in t.lower() for t in tags):
                    continue

        # ── Income filter ───────────────────────────────
        if income is not None:
            inc_max = elig.get("income_max")
            if inc_max is not None and income > inc_max:
                continue

        # ── Caste filter ────────────────────────────────
        if caste:
            s_caste = elig.get("caste")
            if s_caste is not None and isinstance(s_caste, list):
                if caste not in s_caste:
                    continue

        # ── Disability filter ───────────────────────────
        if disability is not None:
            s_dis = elig.get("disability")
            if s_dis is not None and s_dis != disability:
                continue

        # Passed all filters
        desc = s.get("description", "")
        results.append({
            "scheme_id": s.get("scheme_id"),
            "scheme_name": s.get("scheme_name"),
            "category": meta.get("category"),
            "state": meta.get("state"),
            "description": desc[:150] + "…" if len(desc) > 150 else desc,
        })

        if len(results) >= limit:
            break

    return results
