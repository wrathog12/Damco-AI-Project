"""
check_eligibility tool — compare user profile against scheme requirements.
"""
import json
from knowledge import loader


def check_eligibility(
    scheme_id: str,
    user_age: int | None = None,
    user_gender: str | None = None,
    user_state: str | None = None,
    user_income: int | None = None,
    user_occupation: str | None = None,
) -> str:
    """
    Check if a user is eligible for a specific scheme.
    Returns {eligible: bool, reasons: [str]}.
    """
    scheme = loader.get_by_id(scheme_id)

    if not scheme:
        return json.dumps({"error": f"Scheme '{scheme_id}' not found."})

    elig = scheme.get("eligibility", {})
    reasons: list[str] = []
    eligible = True

    # ── Age check ───────────────────────────────────────
    if user_age is not None:
        age_min = elig.get("age_min")
        age_max = elig.get("age_max")
        if age_min is not None and user_age < age_min:
            eligible = False
            reasons.append(f"Age {user_age} is below minimum {age_min}")
        elif age_max is not None and user_age > age_max:
            eligible = False
            reasons.append(f"Age {user_age} is above maximum {age_max}")
        else:
            reasons.append(f"Age {user_age}: ✓ eligible")

    # ── Gender check ────────────────────────────────────
    if user_gender:
        s_gender = elig.get("gender", ["All"])
        if isinstance(s_gender, list) and "All" not in s_gender:
            if user_gender not in s_gender:
                eligible = False
                reasons.append(f"Gender '{user_gender}' not eligible (requires {s_gender})")
            else:
                reasons.append(f"Gender '{user_gender}': ✓ eligible")
        else:
            reasons.append("Gender: ✓ open to all")

    # ── State check ─────────────────────────────────────
    if user_state:
        s_states = elig.get("state_residence", [])
        if s_states and isinstance(s_states, list):
            matches = any(
                user_state.lower() in st.lower() or st.lower() in ("all india", "all")
                for st in s_states
            )
            if not matches:
                eligible = False
                reasons.append(f"State '{user_state}' not in eligible states: {s_states}")
            else:
                reasons.append(f"State '{user_state}': ✓ eligible")

    # ── Income check ────────────────────────────────────
    if user_income is not None:
        inc_max = elig.get("income_max")
        if inc_max is not None and user_income > inc_max:
            eligible = False
            reasons.append(f"Income ₹{user_income:,} exceeds maximum ₹{inc_max:,}")
        elif inc_max is not None:
            reasons.append(f"Income ₹{user_income:,}: ✓ within limit ₹{inc_max:,}")

    return json.dumps({
        "eligible": eligible,
        "scheme_id": scheme_id,
        "scheme_name": scheme.get("scheme_name"),
        "reasons": reasons,
    }, ensure_ascii=False)
