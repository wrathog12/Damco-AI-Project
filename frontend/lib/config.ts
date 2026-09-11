/**
 * Runtime endpoints for the backend.
 *
 * Two rules from Next.js's environment-variable handling drive the shape of
 * this file:
 *
 *  1. `process.env.NEXT_PUBLIC_*` must be referenced *statically* to be
 *     inlined into the client bundle. Dynamic lookups (`process.env[name]`,
 *     or destructuring `process.env` first) are NOT replaced at build time and
 *     silently evaluate to `undefined` in the browser. Hence the literal
 *     references below — do not "simplify" them into a loop or a map.
 *  2. Those values are frozen at `next build` time. One image cannot be
 *     promoted across environments with different backends; rebuild per
 *     environment, or expose the value from a server route instead.
 */

/** Base URL of the FastAPI backend, no trailing slash. */
export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");

/**
 * Base URL for WebSocket connections, no trailing slash.
 *
 * Defaults to `API_URL` with the scheme swapped, so a single
 * `NEXT_PUBLIC_API_URL=https://api.example.com` yields `wss://…` — the
 * previous hardcoded `ws://` broke as soon as the page was served over HTTPS,
 * because browsers refuse insecure WebSockets from a secure origin.
 */
export const WS_URL = (
  process.env.NEXT_PUBLIC_WS_URL ?? API_URL.replace(/^http/, "ws")
).replace(/\/+$/, "");

const withLeadingSlash = (path: string) =>
  path.startsWith("/") ? path : `/${path}`;

/** `apiUrl("/health")` → `http://localhost:8000/health` */
export const apiUrl = (path: string) => `${API_URL}${withLeadingSlash(path)}`;

/** `wsUrl("/ws/cards")` → `ws://localhost:8000/ws/cards` */
export const wsUrl = (path: string) => `${WS_URL}${withLeadingSlash(path)}`;
