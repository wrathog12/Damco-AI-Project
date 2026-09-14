/**
 * The wire contract, typed once.
 *
 * Every shape here mirrors something the backend actually emits, and the
 * backend source of each is named in a comment so the two can be checked
 * against each other. v1 typed all of this as `any` — which is why the client
 * went on posting to `/rtc/webrtc/offer` and listening on `/ws/cards` for a
 * whole phase after both were deleted, with nothing to notice it.
 *
 * These are hand-written rather than generated. The Pydantic models on the
 * other side are `dict[str, Any]` builders (`services/schemes.py::card_payload`
 * is a literal dict), so there is no schema to generate *from*; a generator
 * would only produce `Record<string, unknown>`. `lib/api.ts` therefore narrows
 * at the boundary and nothing downstream re-checks.
 */

/* ── Scheme card — services/schemes.py::card_payload ──────────────────── */

/** A translated block. Only the keys myScheme actually translated are present,
 *  which is why every field is optional — see `_translation_block`. */
export interface SchemeTranslation {
  scheme_name?: string;
  description?: string;
  eligibility_description?: string;
  documents_required?: SchemeDocument[];
}

export interface SchemeBenefit {
  /** Free-form on purpose: the corpus holds ints, strings and the occasional
   *  range ("₹500–₹2,000 per month"), so this is rendered, never arithmetic. */
  amount?: string | number;
  description: string;
}

export interface SchemeDocument {
  name: string;
  is_mandatory?: boolean;
}

export interface SchemeEligibility {
  age_min: number | null;
  age_max: number | null;
  income_max: number | null;
  gender: string[] | null;
  caste: string[] | null;
  occupation: string[] | null;
  disability: boolean | null;
  bpl_card: boolean | null;
  state_residence: string[] | null;
}

export interface SchemeMetadata {
  state: string | null;
  category: string | null;
  subcategory: string | null;
  tags: string[];
  level: string | null;
}

export interface SchemeCardData {
  scheme_id: string;
  scheme_name: string;
  metadata: SchemeMetadata;
  ministry: string | null;
  description: string | null;
  objectives: string[];
  benefits: SchemeBenefit[];
  eligibility: SchemeEligibility;
  eligibility_description: string | null;
  /** Already flattened to text by `_process_text`; render whitespace-pre-wrap. */
  application_process: string | null;
  documents_required: SchemeDocument[];
  application_url: string | null;
  helpline: string | null;
  source_url: string | null;
  /** Keyed by language code. `ta`/`te`/`gu`/`kn`/`ml`/`pa` are languages the
   *  agent will *speak* but has no card translation for, so a card may be
   *  English while the conversation is not. */
  translations: Partial<Record<Language, SchemeTranslation>>;
}

/* ── RTVI server messages — tools/card.py + voice/pipeline.py ─────────── */

export interface ShowSchemeCardEvent {
  type: "show_scheme_card";
  scheme: SchemeCardData;
  language?: Language;
}

export interface EndCallEvent {
  type: "end_call";
  reason?: string;
}

export type ServerEvent = ShowSchemeCardEvent | EndCallEvent;

/* ── Quota — main.py::_quota_payload ─────────────────────────────────── */

export interface Quota {
  /** False means the reply *is* the refusal. The backend answers 200 either
   *  way, deliberately, so there is no error path to render here. */
  allowed: boolean;
  used: number;
  limit: number;
  remaining: number;
  authenticated: boolean;
  resets_at: string | null;
}

/* ── Chat — main.py::ChatResponse ────────────────────────────────────── */

/** OpenAI-shaped, because that is the endpoint's public contract even though
 *  the server talks to Gemini. Echoed back verbatim on the next turn. */
export interface ChatTurn {
  role: string;
  content?: string | null;
  [key: string]: unknown;
}

export interface ChatResponse {
  response: string;
  history: ChatTurn[];
  timings: { llm_ms?: number };
  /** The **whole event**, not a bare scheme. `/chat` passes a collector as
   *  `show_scheme_card`'s `deliver`, so what lands here is byte-for-byte what the
   *  voice path sends over RTVI — including the `language` the tool was actually
   *  called with, which can differ from the one the UI is set to. */
  card: ShowSchemeCardEvent | null;
  quota: Quota;
  /** Present only when the thread is being persisted — which needs a phone
   *  number on the account *and* an encryption key on the server. Absent is
   *  "this conversation is not remembered", not an error. */
  conversation_id: string | null;
}

/* ── Identity — main.py::me ──────────────────────────────────────────── */

export interface Consent {
  version: string | null;
  at: string | null;
  /** Not `version !== null`: publishing a new notice makes an old agreement
   *  stale, and a client that only checked for presence would never re-ask. */
  current: boolean;
  required_version: string;
}

export interface ProfileSummary {
  /** False when the server has no `PROFILE_ENCRYPTION_KEY`, in which case
   *  nothing personal can be stored at all and the UI must say so rather than
   *  offer a form that silently discards. */
  supported: boolean;
  has_profile: boolean;
  fields: ProfileField[];
  updated_at: string | null;
  /** `null` means "not worked out yet", which is a different thing to show
   *  someone than zero. */
  eligible_count: number | null;
}

export interface Me {
  /** Whether this *request* proved it holds an access token — not whether the
   *  identity happens to carry a phone number. */
  authenticated: boolean;
  phone: string | null;
  consent: Consent;
  profile: ProfileSummary;
  quota: Quota;
}

/* ── Profile — services/profiles.py::_TYPES ──────────────────────────── */

export const PROFILE_FIELDS = [
  "age",
  "gender",
  "state",
  "income",
  "occupation",
  "caste",
  "disability",
  "bpl_card",
] as const;

export type ProfileField = (typeof PROFILE_FIELDS)[number];

export interface ProfileFacts {
  age?: number | null;
  gender?: string | null;
  state?: string | null;
  income?: number | null;
  occupation?: string | null;
  caste?: string | null;
  disability?: boolean | null;
  bpl_card?: boolean | null;
}

export interface ProfileResponse {
  supported: boolean;
  has_profile: boolean;
  facts: ProfileFacts;
  fields: ProfileField[];
  version: number;
  consent_version: string | null;
  updated_at: string | null;
  retention: string;
}

/* ── Eligible schemes — services/eligibility.py::evaluate_all ────────── */

export interface EligibleScheme {
  scheme_id: string;
  scheme_name: string;
  reasons: string[];
}

export interface EligibleResponse {
  total: number;
  eligible_count: number;
  /** Empty with `eligible_count: 0` means there is nothing stored to evaluate
   *  against — not that the caller qualifies for nothing. */
  profile_fields: ProfileField[];
  schemes: EligibleScheme[];
}

/* ── Auth — auth/routes.py ───────────────────────────────────────────── */

export interface OtpSent {
  sent: boolean;
  /** Masked (`+*********3210`) so a typo is catchable before the SMS. A full
   *  number never leaves the server. */
  phone: string;
  expires_in: number;
  retry_after: number;
  /** Only ever present when the server runs `OTP_PROVIDER=stub` *and*
   *  `OTP_DEV_ECHO=1`. Rendering it is a dev affordance; it cannot appear in a
   *  deployment because the flag is ignored for a real provider. */
  dev_code: string | null;
}

export interface Session {
  access_token: string;
  token_type: string;
  expires_in: number;
  phone: string | null;
  quota: Partial<Quota>;
}

/* ── Health — main.py::health ────────────────────────────────────────── */

export interface Health {
  status: "ok" | "degraded";
  schemes?: number;
  indexed?: number;
  semantic_search?: boolean;
  states?: number;
  categories?: number;
  privacy?: {
    profiles_enabled: boolean;
    consent_version: string;
    retention: Record<string, number | string>;
  };
  error?: string;
}

/* ── Errors — services/auth.py::AuthError, handled at main.py:160 ────── */

/** Every refusal on the account surface has this one shape, because they all
 *  raise the same exception through one handler. So the client has one error
 *  parser, not one per endpoint. */
export interface ApiErrorBody {
  error: string;
  message: string;
}

/* ── Language ────────────────────────────────────────────────────────── */

/** What `LanguageTagger` can switch the voice to. Only the first four have
 *  card translations in the corpus; the rest fall back to English text while
 *  the agent still speaks the chosen language. */
export const LANGUAGES = [
  { code: "en", label: "English", native: "English", cards: true },
  { code: "hi", label: "Hindi", native: "हिन्दी", cards: true },
  { code: "bn", label: "Bengali", native: "বাংলা", cards: true },
  { code: "mr", label: "Marathi", native: "मराठी", cards: true },
  { code: "ta", label: "Tamil", native: "தமிழ்", cards: false },
  { code: "te", label: "Telugu", native: "తెలుగు", cards: false },
  { code: "gu", label: "Gujarati", native: "ગુજરાતી", cards: false },
  { code: "kn", label: "Kannada", native: "ಕನ್ನಡ", cards: false },
  { code: "ml", label: "Malayalam", native: "മലയാളം", cards: false },
  { code: "pa", label: "Punjabi", native: "ਪੰਜਾਬੀ", cards: false },
] as const;

export type Language = (typeof LANGUAGES)[number]["code"];

export const CARD_LANGUAGES = LANGUAGES.filter((l) => l.cards);
