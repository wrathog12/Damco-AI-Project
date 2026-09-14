/**
 * The one place that talks to the backend.
 *
 * Three things are centralised here because getting any of them wrong in one
 * caller is a bug you find months later:
 *
 * 1. **`credentials: "include"` on every request.** The anonymous identity is a
 *    signed `bh_anon` cookie, and the frontend is a different origin from the
 *    API (`:3000` vs `:8000`), so the browser drops it under the default
 *    `same-origin` policy. A dropped cookie is not an error — the server just
 *    mints a *new* anonymous identity, so the caller silently gets a fresh
 *    ten-turn allowance on every request and the quota looks broken rather than
 *    absent.
 * 2. **The access token lives in memory, never in `localStorage`.** That is the
 *    backend's stated reason for putting it in the response body instead of a
 *    cookie: it is short-lived precisely so that holding it in a JS variable is
 *    acceptable. Persisting it would hand an XSS a durable credential.
 *    Surviving a page reload is the refresh cookie's job — see `restore()`.
 * 3. **One error shape.** Every refusal on the account surface comes back as
 *    `{error, message}` through a single handler, so `ApiError` carries the code
 *    and a sentence that is already safe to show a user.
 */
import { apiUrl } from "@/lib/config";
import type {
  ApiErrorBody,
  ChatResponse,
  ChatTurn,
  EligibleResponse,
  Health,
  Language,
  Me,
  OtpSent,
  ProfileFacts,
  ProfileResponse,
  Quota,
  Session,
} from "@/lib/types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  /** True when signing in is what would fix this. */
  get needsLogin() {
    return this.status === 401;
  }
}

/* ── The access token ─────────────────────────────────────────────────── */

let accessToken: string | null = null;
const tokenListeners = new Set<(token: string | null) => void>();

export function getAccessToken() {
  return accessToken;
}

export function setAccessToken(token: string | null) {
  accessToken = token;
  for (const listener of tokenListeners) listener(token);
}

export function onAccessTokenChange(listener: (token: string | null) => void) {
  tokenListeners.add(listener);
  return () => tokenListeners.delete(listener);
}

/* ── The fetch wrapper ────────────────────────────────────────────────── */

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Set for the refresh call itself, which must not recurse. */
  noRetry?: boolean;
  signal?: AbortSignal;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, noRetry = false, signal } = opts;

  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const res = await fetch(apiUrl(path), {
    method,
    headers,
    credentials: "include",
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  // A 401 on an account-surface call after the 15-minute access token expired
  // is the common case, not an exception. Rotate once and replay; only a second
  // 401 means the session is really gone. The conversational surface never
  // 401s, so this only ever fires for `/api/me*`.
  if (res.status === 401 && !noRetry && accessToken) {
    const rotated = await restore();
    if (rotated) return request<T>(path, { ...opts, noRetry: true });
  }

  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = `The server could not complete that request (${res.status}).`;
    try {
      const parsed = (await res.json()) as Partial<ApiErrorBody> & {
        detail?: unknown;
      };
      if (typeof parsed.error === "string") code = parsed.error;
      if (typeof parsed.message === "string") message = parsed.message;
      else if (typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      // A non-JSON body (a proxy's HTML error page, a dropped connection) keeps
      // the generic message rather than throwing a parse error over the top of
      // the real failure.
    }
    throw new ApiError(res.status, code, message);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* ── Endpoints ────────────────────────────────────────────────────────── */

export const api = {
  health: () => request<Health>("/health"),

  /** Also what establishes an anonymous identity: calling it sets the cookie,
   *  so doing it once on load puts the id in place before anyone speaks. */
  quota: () => request<Quota>("/api/quota"),

  me: () => request<Me>("/api/me"),

  chat: (payload: {
    message: string;
    history: ChatTurn[];
    language: Language;
    conversation_id?: string;
  }) => request<ChatResponse>("/chat", { method: "POST", body: payload }),

  requestOtp: (phone: string) =>
    request<OtpSent>("/api/auth/request-otp", {
      method: "POST",
      body: { phone },
    }),

  verifyOtp: (phone: string, code: string) =>
    request<Session>("/api/auth/verify-otp", {
      method: "POST",
      body: { phone, code },
    }),

  logout: () => request<{ signed_out: boolean }>("/api/auth/logout", { method: "POST" }),

  acceptConsent: () =>
    request<{ consent: { version: string; at: string; current: boolean } }>(
      "/api/me/consent",
      { method: "POST" },
    ),

  profile: () => request<ProfileResponse>("/api/me/profile"),

  /** PUT, but a patch-merge — `null` clears one field, omitting it leaves it. */
  saveProfile: (patch: ProfileFacts) =>
    request<{ saved: boolean; facts: ProfileFacts; version: number; updated_at: string | null }>(
      "/api/me/profile",
      { method: "PUT", body: patch },
    ),

  deleteProfile: () => request<{ deleted: boolean }>("/api/me/profile", { method: "DELETE" }),

  deleteHistory: () =>
    request<{ deleted: { conversations: number; interactions: number } }>(
      "/api/me/history",
      { method: "DELETE" },
    ),

  deleteAccount: () => request<{ deleted: boolean }>("/api/me", { method: "DELETE" }),

  eligible: (limit = 30) =>
    request<EligibleResponse>(`/api/me/eligible?limit=${limit}`),
};

/**
 * Trade the refresh cookie for a fresh access token.
 *
 * Called once on load and again whenever a request 401s. Returns whether a
 * session was recovered; a failure is the *normal* outcome for someone who has
 * never signed in, so it resolves false instead of throwing — a rejected
 * promise here would put a "session expired" error in front of a first-time
 * visitor who has no session to expire.
 */
export async function restore(): Promise<boolean> {
  try {
    const session = await request<Session>("/api/auth/refresh", {
      method: "POST",
      noRetry: true,
    });
    setAccessToken(session.access_token);
    return true;
  } catch {
    setAccessToken(null);
    return false;
  }
}
