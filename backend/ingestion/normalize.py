"""
Raw API payload -> canonical scheme record.

Three things the API does not hand us cleanly:

1. **`{value, label}` envelopes.** `state`, `level`, `schemeCategory`,
   `schemeSubCategory`, `targetBeneficiaries` and friends all arrive wrapped.
   We keep the label, because that is the vocabulary the LLM's normalisation
   table and the facet filters both speak.

2. **Rich-text ASTs.** `benefits_md` / `eligibilityDescription_md` /
   `detailedDescription_md` are convenient Markdown mirrors, but
   `documents_required`, `applicationProcess[].process` and
   `schemeDefinitions[].definition` exist *only* as Slate-style trees, so they
   need flattening.

3. **Eligibility as prose.** Only `eligibilityDescription_md` is provided; there
   are no numeric age/income fields. `parse_eligibility()` recovers what it can.

On (3), the guiding rule is that **a wrong constraint is far worse than a missing
one**: telling a citizen they are ineligible when they qualify is the failure
mode that matters. Since NULL means "unspecified, do not exclude", every parser
here declines to guess — it sets a value only on explicitly restrictive phrasing.
The authoritative source for the categorical fields is the facet crawl
(`harvest_facets`), which reads the government's own per-scheme tagging; this
module's regexes exist for the numeric ranges facets only bucket.
"""
import html
import re
from datetime import date, datetime
from typing import Any

# ── text hygiene ─────────────────────────────────────────────────────────
# The CMS behind this API stores HTML fragments inside "markdown" fields, and
# entities arrive multiply-escaped ("&amp;amp;#39;" for a single apostrophe), so
# one unescape pass is not enough.
_BR_TAG = re.compile(r"<br\s*/?>", re.I)
_HTML_TAG = re.compile(r"</?(?:p|div|span|strong|b|i|em|u|ul|ol|li|table|tbody|tr|"
                       r"td|th|h[1-6]|a|font)\b[^>]*>", re.I)
_NBSP = " "


def clean_text(text: Any) -> str:
    """Unescape entities, drop stray HTML, normalise whitespace."""
    if not text or not isinstance(text, str):
        return ""
    out = text
    for _ in range(4):                    # entities can be nested 3 deep
        unescaped = html.unescape(out)
        if unescaped == out:
            break
        out = unescaped
    out = _BR_TAG.sub("\n", out)
    out = _HTML_TAG.sub("", out)
    out = out.replace(_NBSP, " ").replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean_or_none(text: Any) -> str | None:
    cleaned = clean_text(text)
    return cleaned or None

# ── rich text ────────────────────────────────────────────────────────────
_LIST_TYPES = {"ol_list", "ul_list", "numbered_list", "bulleted_list", "list"}
_ORDERED_TYPES = {"ol_list", "numbered_list"}
_INLINE_TYPES = {"link", "inline", None}


def _leaf_text(node: dict) -> str:
    text = node.get("text") or ""
    if not text:
        return ""
    if node.get("bold"):
        text = f"**{text}**"
    if node.get("italic"):
        text = f"*{text}*"
    return text


def _is_inline(node: Any) -> bool:
    if not isinstance(node, dict):
        return True
    return "text" in node and "children" not in node or node.get("type") == "link"


def _render_children(children: list, depth: int) -> str:
    """Inline children concatenate; block children go on their own lines."""
    parts: list[str] = []
    for child in children or []:
        rendered = _render_node(child, depth)
        if not rendered:
            continue
        if _is_inline(child) and parts and _is_inline_part(parts[-1]):
            parts[-1] = parts[-1] + rendered
        else:
            parts.append(rendered)
    return "\n".join(p for p in parts if p.strip())


def _is_inline_part(_: str) -> bool:
    # Inline runs are merged greedily; block nodes always start a new part.
    return True


def _render_node(node: Any, depth: int = 0) -> str:
    if node is None:
        return ""
    if not isinstance(node, dict):
        return str(node)

    node_type = node.get("type")
    children = node.get("children") or []

    if node_type is None and "text" in node:
        return _leaf_text(node)

    if node_type == "link":
        label = _render_children(children, depth) or node.get("link") or ""
        url = node.get("link") or node.get("url") or ""
        return f"[{label}]({url})" if url else label

    if node_type in _LIST_TYPES:
        ordered = node_type in _ORDERED_TYPES
        lines: list[str] = []
        for i, item in enumerate(children, start=1):
            body = _render_node(item, depth + 1).strip()
            if not body:
                continue
            marker = f"{i}. " if ordered else "- "
            indent = "  " * depth
            # Keep multi-line list items visually inside their bullet.
            body = body.replace("\n", "\n" + indent + "  ")
            lines.append(f"{indent}{marker}{body}")
        return "\n".join(lines)

    if node_type == "list_item":
        return _render_children(children, depth)

    if node_type and re.fullmatch(r"h[1-6]", node_type):
        return "#" * int(node_type[1]) + " " + _render_children(children, depth)

    if node_type in ("table", "tbody", "tr"):
        return _render_children(children, depth)
    if node_type in ("td", "th"):
        return _render_children(children, depth)

    # paragraph, div, blockquote, anything unrecognised
    return _render_children(children, depth)


def richtext_to_markdown(nodes: Any) -> str:
    """Flatten a Slate-style AST into Markdown-ish plain text."""
    if nodes is None:
        return ""
    if isinstance(nodes, str):
        return clean_text(nodes)
    if isinstance(nodes, dict):
        nodes = [nodes]
    return clean_text("\n".join(
        filter(None, (_render_node(n).strip() for n in nodes))
    ))


# A bullet marker is `1.` / `-` / `•` / a *single* asterisk followed by space.
# `**bold**` must survive: matching a lone `*` here is what previously turned
# "**Step 1:**" into "*Step 1:**" across 2,166 application steps.
_BULLET_PREFIX = re.compile(r"^\s*(?:\d+[.)]\s+|[-•]\s+|\*(?!\*)\s+)")


def strip_bullet(line: str) -> str:
    return _BULLET_PREFIX.sub("", line).strip()


def richtext_to_items(nodes: Any) -> list[str]:
    """Flatten an AST that is really a list, into one string per item."""
    md = richtext_to_markdown(nodes)
    items = [strip_bullet(line) for line in md.splitlines()]
    return [i for i in items if i]


# ── {value, label} envelopes ─────────────────────────────────────────────
def label_of(value: Any) -> Any:
    """Unwrap one `{value,label}` object (or a list of them) down to labels."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        label = value.get("label")
        if isinstance(label, str):
            return label.strip() or None
        return label
    if isinstance(value, list):
        out = [label_of(v) for v in value]
        return [v for v in out if v]
    return value


def labels_of(value: Any) -> list[str]:
    """Always a list of label strings, possibly empty."""
    result = label_of(value)
    if result is None:
        return []
    if isinstance(result, list):
        return [str(r) for r in result]
    return [str(result)]


def first_label(value: Any) -> str | None:
    labels = labels_of(value)
    return labels[0] if labels else None


def normalize_level(value: Any) -> str | None:
    """Collapse the API's three labels to two.

    Observed: 'State/ UT' (the majority), 'State', 'Central'. Union Territories
    are state-level for every filter we run, so folding them in keeps `level` a
    clean binary rather than a vocabulary that has to be memorised downstream.
    """
    label = first_label(value)
    if not label:
        return None
    low = label.strip().lower()
    if "central" in low:
        return "Central"
    if low.startswith("state") or low in ("ut", "union territory"):
        return "State"
    return label.strip()


# ── numeric parsing ──────────────────────────────────────────────────────
_MULTIPLIERS = {
    "thousand": 1_000, "k": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
}

_AGE_RANGE = re.compile(
    r"(?:between\s+)?(\d{1,2})\s*(?:-|–|—|to|and)\s*(\d{1,2})\s*(?:years|yrs|year)",
    re.I)
_AGE_MIN = re.compile(
    r"(?:minimum\s+age|age\s+of|aged|not\s+less\s+than|at\s+least)\s*"
    r"(?:of\s+)?(\d{1,2})\s*(?:years|yrs)?\s*"
    r"(?:or\s+(?:above|more|older)|and\s+above|\+)?", re.I)
_AGE_MIN_ABOVE = re.compile(
    r"(?:above|over|more\s+than)\s*(\d{1,2})\s*(?:years|yrs)", re.I)
_AGE_MAX = re.compile(
    r"(?:maximum\s+age|not\s+exceed(?:ing)?|below|under|less\s+than|"
    r"up\s*to|upto|maximum\s+of)\s*(?:the\s+age\s+of\s*)?(\d{1,2})\s*(?:years|yrs)",
    re.I)

_AMOUNT = r"(?:rs\.?|inr|₹)?\s*([\d][\d,]*(?:\.\d+)?)\s*(thousand|lakhs?|lacs?|crores?|cr|k)?"
_INCOME_MAX = re.compile(
    r"(?:annual\s+)?(?:family\s+)?income[^.\n]{0,90}?"
    r"(?:below|less\s+than|not\s+exceed(?:ing)?|does\s+not\s+exceed|up\s*to|upto|"
    r"under|maximum\s+of|not\s+more\s+than|within)\s*" + _AMOUNT,
    re.I)


def _to_rupees(number: str, unit: str | None) -> float | None:
    try:
        # Indian grouping: "2,00,000" -> 200000
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if unit:
        value *= _MULTIPLIERS.get(unit.lower().rstrip("s"), 1) \
            if unit.lower().rstrip("s") in _MULTIPLIERS \
            else _MULTIPLIERS.get(unit.lower(), 1)
    return value if 0 < value < 1e11 else None


def parse_age_range(text: str) -> tuple[int | None, int | None]:
    """Recover (age_min, age_max). Either may stay None."""
    if not text:
        return None, None
    age_min = age_max = None

    match = _AGE_RANGE.search(text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if 0 <= low < high <= 100:
            return low, high

    for pattern in (_AGE_MIN, _AGE_MIN_ABOVE):
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if 0 < value <= 100:
                age_min = value
                break

    match = _AGE_MAX.search(text)
    if match:
        value = int(match.group(1))
        if 0 < value <= 100:
            age_max = value

    # An inverted range means we matched two unrelated sentences; trust neither.
    if age_min is not None and age_max is not None and age_min >= age_max:
        return None, None
    return age_min, age_max


def parse_income_max(text: str) -> float | None:
    if not text:
        return None
    match = _INCOME_MAX.search(text)
    if not match:
        return None
    value = _to_rupees(match.group(1), match.group(2))
    # A bare "income below 2" is a parse artefact, not a rupee figure.
    if value is None or value < 1000:
        return None
    return value


# ── categorical flags: only set on explicitly restrictive phrasing ───────
_FEMALE_ONLY = re.compile(
    r"(?:only|exclusively|solely)\s+(?:available\s+)?(?:for\s+|to\s+)?"
    r"(?:women|woman|girls?|female|widows?|mothers?)"
    r"|(?:women|girls?|female)\s+(?:candidates?|applicants?|beneficiaries)\s+only"
    r"|applicant\s+(?:must|should)\s+be\s+a?\s*(?:woman|female|girl)", re.I)
_MALE_ONLY = re.compile(
    r"(?:only|exclusively)\s+(?:for\s+|to\s+)?(?:men|male|boys?)"
    r"|applicant\s+(?:must|should)\s+be\s+a?\s*(?:man|male|boy)", re.I)
_BPL = re.compile(r"\bbpl\b|below\s+poverty\s+line", re.I)
_DISABILITY = re.compile(
    r"\bdisabilit(?:y|ies)\b|\bdivyang\b|differently[- ]abled|"
    r"\bpwd\b|\bhandicapped\b|specially\s+abled", re.I)
_MINORITY = re.compile(r"\bminority\b|\bminorities\b", re.I)
_STUDENT = re.compile(r"\bstudent\b|\bstudying\b|\benrolled\b|\bscholarship\b", re.I)
_CASTES = (
    ("SC", re.compile(r"\bsc\b|scheduled\s+caste", re.I)),
    ("ST", re.compile(r"\bst\b|scheduled\s+tribe", re.I)),
    ("OBC", re.compile(r"\bobc\b|other\s+backward", re.I)),
)


def parse_eligibility(text: str, *, category: str | None = None,
                      tags: list[str] | None = None) -> dict:
    """Best-effort typed eligibility. Absent evidence -> None (never exclude)."""
    haystack = " ".join(filter(None, [text or "", category or "",
                                      " ".join(tags or [])]))
    age_min, age_max = parse_age_range(text or "")

    gender: list[str] | None = None
    if _FEMALE_ONLY.search(haystack):
        gender = ["Female"]
    elif _MALE_ONLY.search(haystack):
        gender = ["Male"]

    castes = [name for name, pattern in _CASTES if pattern.search(text or "")]

    return {
        "age_min": age_min,
        "age_max": age_max,
        "income_max": parse_income_max(text or ""),
        "gender": gender,
        "caste": castes or None,
        "occupation": None,          # supplied by the facet crawl
        "disability": True if _DISABILITY.search(haystack) else None,
        "bpl_card": True if _BPL.search(text or "") else None,
        "minority": True if _MINORITY.search(haystack) else None,
        "student": True if _STUDENT.search(haystack) else None,
        "marital_status": None,
    }


# ── dates ────────────────────────────────────────────────────────────────
def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (lambda t: datetime.fromisoformat(t).date(),
                  lambda t: datetime.strptime(t, "%d-%m-%Y").date(),
                  lambda t: datetime.strptime(t, "%d/%m/%Y").date()):
        try:
            return parse(text)
        except (ValueError, TypeError):
            continue
    return None


# ── the canonical record ─────────────────────────────────────────────────
SOURCE_URL_TEMPLATE = "https://www.myscheme.gov.in/schemes/{slug}"


def build_scheme(raw: dict) -> dict:
    """Map one harvested file into Scheme column values.

    `raw` is a record written by `ingestion.harvest` — the whole file, so index
    metadata and every language block are available.
    """
    slug = raw["slug"]
    en = (raw.get("langs") or {}).get("en") or {}
    index_fields = raw.get("index_fields") or {}

    basic = en.get("basicDetails") or {}
    content = en.get("schemeContent") or {}
    eligibility_block = en.get("eligibilityCriteria") or {}
    application = en.get("applicationProcess") or []

    scheme_name = clean_text(basic.get("schemeName")
                            or index_fields.get("schemeName")) or slug

    # beneficiaryState is the authoritative multi-state list; "All" means
    # nationwide, which is the *absence* of a residence constraint, not a state.
    raw_states = labels_of(index_fields.get("beneficiaryState")) \
        or labels_of(basic.get("state"))
    states = [s for s in raw_states if s.lower() != "all"]
    nationwide = bool(raw_states) and not states

    categories = labels_of(basic.get("schemeCategory")) \
        or labels_of(index_fields.get("schemeCategory"))
    subcategories = labels_of(basic.get("schemeSubCategory"))

    # `_md` mirrors still carry CMS HTML and multiply-escaped entities, so they
    # go through clean_text too — and the eligibility parser must see the cleaned
    # text, or "&amp;#39;" noise defeats the regexes.
    eligibility_md = clean_text(eligibility_block.get("eligibilityDescription_md")) or \
        richtext_to_markdown(eligibility_block.get("eligibilityDescription"))
    benefits_md = clean_text(content.get("benefits_md")) or \
        richtext_to_markdown(content.get("benefits"))
    detailed_md = clean_text(content.get("detailedDescription_md")) or \
        richtext_to_markdown(content.get("detailedDescription"))
    exclusions_md = clean_text(content.get("exclusions_md")) or \
        richtext_to_markdown(content.get("exclusions"))

    tags = labels_of(basic.get("tags")) or labels_of(index_fields.get("tags"))

    parsed = parse_eligibility(
        eligibility_md,
        category=categories[0] if categories else None,
        tags=tags,
    )

    # Application: prefer an online URL, but record every mode offered.
    modes, application_url = [], None
    for entry in application if isinstance(application, list) else []:
        mode = label_of(entry.get("mode")) if isinstance(entry, dict) else None
        if mode:
            modes.append(str(mode))
        url = entry.get("url") if isinstance(entry, dict) else None
        if url and (application_url is None or str(mode).lower() == "online"):
            application_url = url

    documents = raw.get("documents") or {}
    document_items = richtext_to_items(documents.get("en"))

    return {
        "scheme_id": slug,
        "myscheme_id": raw.get("scheme_id"),
        "scheme_name": scheme_name,
        "scheme_short_title": basic.get("schemeShortTitle")
        or index_fields.get("schemeShortTitle"),

        "state": states[0] if states else ("All" if nationwide else None),
        "states": states or None,
        "level": normalize_level(basic.get("level")
                                 or index_fields.get("level")),
        "category": categories[0] if categories else None,
        "categories": categories or None,
        "subcategories": subcategories or None,
        "tags": tags or None,

        "nodal_ministry": first_label(basic.get("nodalMinistryName"))
        or index_fields.get("nodalMinistryName"),
        "nodal_department": first_label(basic.get("nodalDepartmentName")),
        "implementing_agency": first_label(basic.get("implementingAgency")),
        "scheme_for": basic.get("schemeFor") or index_fields.get("schemeFor"),
        "target_beneficiaries": labels_of(basic.get("targetBeneficiaries")) or None,
        "dbt_scheme": basic.get("dbtScheme") if isinstance(basic.get("dbtScheme"), bool)
        else None,

        "description": clean_or_none(content.get("briefDescription")
                                     or index_fields.get("briefDescription")),
        "detailed_description": detailed_md or None,
        "benefits_md": benefits_md or None,
        "exclusions_md": exclusions_md or None,
        "eligibility_description": eligibility_md or None,

        # The API has no equivalent of v1's LLM-written 3-bullet objectives, and
        # inventing them would be fabrication. Left empty on purpose.
        "objectives": None,
        "benefits": [{"description": b} for b in _md_bullets(benefits_md)] or None,
        "benefit_types": labels_of(content.get("benefitTypes")) or None,
        # `is_mandatory` is genuinely unknown here — v1 had the LLM guess it.
        "documents_required": [{"name": d, "is_mandatory": None}
                               for d in document_items] or None,
        "application_process": _application_process(application),
        "scheme_definitions": _definitions(en.get("schemeDefinitions")),
        "references": content.get("references") or None,

        "application_url": application_url,
        "application_modes": sorted(set(modes)) or None,
        "helpline": None,          # not exposed by this API

        **parsed,
        # A nationwide scheme must not be filtered to any state list.
        "state_residence": states or None,

        "scheme_open_date": parse_date(basic.get("schemeOpenDate")),
        "scheme_close_date": parse_date(basic.get("schemeCloseDate")
                                       or index_fields.get("schemeCloseDate")),
        "source_url": SOURCE_URL_TEMPLATE.format(slug=slug),
        "content_hash": raw["content_hash"],
    }


def _md_bullets(markdown: str | None) -> list[str]:
    """Split a Markdown body into its top-level bullets/numbered items."""
    if not markdown:
        return []
    items = [strip_bullet(line) for line in markdown.splitlines()]
    return [i for i in items if i]


def _application_process(application: Any) -> list | None:
    if not isinstance(application, list) or not application:
        return None
    out = []
    for entry in application:
        if not isinstance(entry, dict):
            continue
        out.append({
            "mode": label_of(entry.get("mode")),
            "url": entry.get("url"),
            "steps": richtext_to_items(entry.get("process")),
        })
    return out or None


def _definitions(definitions: Any) -> list | None:
    if not isinstance(definitions, list) or not definitions:
        return None
    out = []
    for entry in definitions:
        if not isinstance(entry, dict):
            continue
        out.append({
            "name": entry.get("name"),
            "definition": richtext_to_markdown(entry.get("definition")),
        })
    return out or None


def build_translation(raw: dict, lang: str) -> dict | None:
    """Translation row values, or None if that language wasn't really served."""
    status = (raw.get("translation_status") or {}).get(lang)
    block = (raw.get("langs") or {}).get(lang)
    if not block:
        return None

    basic = block.get("basicDetails") or {}
    content = block.get("schemeContent") or {}
    eligibility = block.get("eligibilityCriteria") or {}
    documents = (raw.get("documents") or {}).get(lang)

    return {
        "lang": lang,
        "status": status or "TRANSLATED",
        "scheme_name": clean_or_none(basic.get("schemeName")),
        "description": clean_or_none(content.get("briefDescription")),
        "detailed_description": clean_or_none(content.get("detailedDescription_md"))
        or richtext_to_markdown(content.get("detailedDescription")) or None,
        "benefits_md": clean_or_none(content.get("benefits_md"))
        or richtext_to_markdown(content.get("benefits")) or None,
        "eligibility_description": clean_or_none(eligibility.get("eligibilityDescription_md"))
        or richtext_to_markdown(eligibility.get("eligibilityDescription")) or None,
        "documents_required": [{"name": d, "is_mandatory": None}
                               for d in richtext_to_items(documents)] or None,
        "payload": block,
        "source_content_hash": raw.get("content_hash"),
    }


# ── validation: nothing reaches Postgres unvalidated ─────────────────────
REQUIRED_FIELDS = ("scheme_id", "scheme_name", "content_hash")


class ValidationError(ValueError):
    """A normalised record is not fit to store."""


def validate_scheme(record: dict) -> list[str]:
    """Return a list of problems; empty means the record is storable.

    v1 wrote whatever the LLM produced straight to disk with no checks at all,
    which is how a file literally named `.json` ended up in the corpus.
    """
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if not record.get(field):
            problems.append(f"missing required field: {field}")

    if record.get("age_min") is not None and record.get("age_max") is not None:
        if record["age_min"] > record["age_max"]:
            problems.append(f"age_min {record['age_min']} > age_max {record['age_max']}")
    for field in ("age_min", "age_max"):
        value = record.get(field)
        if value is not None and not (0 <= value <= 120):
            problems.append(f"{field} out of range: {value}")
    if record.get("income_max") is not None and record["income_max"] <= 0:
        problems.append(f"income_max not positive: {record['income_max']}")
    # normalize_level() folds 'State/ UT' into 'State', so only two are valid.
    if record.get("level") and record["level"] not in ("Central", "State"):
        problems.append(f"unexpected level: {record['level']!r}")
    return problems
