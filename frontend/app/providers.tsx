"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { api, ApiError, restore, setAccessToken } from "@/lib/api";
import type { Language, Me, Quota } from "@/lib/types";

/* ─────────────────────────────────────────────────────────────────────────
   Preferences that live outside React

   Both the language and the theme are stored in `localStorage`, which makes them
   an *external store* rather than React state. Reading them in an effect and
   calling `setState` works but costs a second render on every mount — and the
   React Compiler rejects it, correctly. `useSyncExternalStore` is the primitive
   for exactly this: the value is read during the client render, and
   `getServerSnapshot` supplies what the server actually rendered so hydration
   still matches.
   ───────────────────────────────────────────────────────────────────────── */

/** Same-tab writes don't fire `storage` (that event is for *other* tabs), so a
 *  write announces itself and both paths converge on one subscription. */
const STORE_EVENT = "bhasha:stored";

function useStored<T extends string>(key: string, fallback: T) {
  const subscribe = useCallback((onChange: () => void) => {
    window.addEventListener("storage", onChange);
    window.addEventListener(STORE_EVENT, onChange);
    return () => {
      window.removeEventListener("storage", onChange);
      window.removeEventListener(STORE_EVENT, onChange);
    };
  }, []);

  const read = useCallback(
    () => (window.localStorage.getItem(key) as T | null) ?? fallback,
    [key, fallback],
  );
  const onServer = useCallback(() => fallback, [fallback]);

  const value = useSyncExternalStore(subscribe, read, onServer);

  const write = useCallback(
    (next: T) => {
      window.localStorage.setItem(key, next);
      window.dispatchEvent(new Event(STORE_EVENT));
    },
    [key],
  );

  return [value, write] as const;
}

/** `prefers-color-scheme`, likewise an external store. False on the server,
 *  which is harmless: `THEME_SCRIPT` has already put the right class on `<html>`
 *  before this component exists. */
function useSystemDark() {
  const subscribe = useCallback((onChange: () => void) => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia("(prefers-color-scheme: dark)").matches,
    () => false,
  );
}

/* ─────────────────────────────────────────────────────────────────────────
   Session

   One source of truth for "who is this and what are they allowed", because the
   header, the voice panel, the chat panel and the profile page all need it and
   four independent fetches would show four different numbers.
   ───────────────────────────────────────────────────────────────────────── */

interface SessionValue {
  /** Null while the first load is in flight. Distinguished from "loaded and
   *  anonymous" so the header can skeleton instead of flashing "Sign in". */
  me: Me | null;
  quota: Quota | null;
  ready: boolean;
  authenticated: boolean;
  /** Re-read the identity. Called after login, after a profile save, and after
   *  every conversation turn — a turn spends allowance, so a stale number here
   *  is a number that says a caller has turns they do not have. */
  refresh: () => Promise<void>;
  /** Apply a quota a mutating endpoint already returned, without a round trip.
   *  `/chat` carries the post-turn allowance in its own response. */
  applyQuota: (next: Quota) => void;
  signIn: (token: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <Providers>");
  return value;
}

function SessionProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.me();
      setMe(next);
      setQuota(next.quota);
      return;
    } catch (err) {
      // `/api/me` needs a token for the personal half but answers for anyone;
      // a 401 here means the token expired mid-flight and `restore` already
      // failed, so fall back to the allowance, which never refuses.
      if (!(err instanceof ApiError)) throw err;
    }
    try {
      setMe(null);
      setQuota(await api.quota());
    } catch {
      // The backend is down. Leaving both null is honest — the UI shows
      // "can't reach the assistant" rather than a fabricated allowance.
      setQuota(null);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Order matters. `restore()` trades the httpOnly refresh cookie for an
      // access token, and it has to finish before `/api/me` is asked anything —
      // otherwise the first render of a signed-in caller says "anonymous",
      // which is a login screen appearing in front of someone already logged in.
      await restore();
      if (cancelled) return;
      await load();
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  const signIn = useCallback(
    async (token: string) => {
      setAccessToken(token);
      await load();
    },
    [load],
  );

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // Asking to be signed out cannot fail from the caller's point of view.
      // The server answers 200 for any input; a network error here still has to
      // drop the local token, or the UI shows a session the server has revoked.
    }
    setAccessToken(null);
    await load();
  }, [load]);

  const value = useMemo<SessionValue>(
    () => ({
      me,
      quota,
      ready,
      authenticated: me?.authenticated ?? false,
      refresh: load,
      applyQuota: setQuota,
      signIn,
      signOut,
    }),
    [me, quota, ready, load, signIn, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/* ─────────────────────────────────────────────────────────────────────────
   Language

   This picks the language the *agent* answers in and the one a scheme card
   renders in. It is deliberately prominent in the header rather than buried in
   settings: for most of this audience it is the single most consequential
   control on the page.
   ───────────────────────────────────────────────────────────────────────── */

const LANGUAGE_KEY = "bhasha.language";

interface LanguageValue {
  language: Language;
  setLanguage: (next: Language) => void;
}

const LanguageContext = createContext<LanguageValue | null>(null);

export function useLanguage() {
  const value = useContext(LanguageContext);
  if (!value) throw new Error("useLanguage must be used inside <Providers>");
  return value;
}

function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguage] = useStored<Language>(LANGUAGE_KEY, "en");
  const value = useMemo(() => ({ language, setLanguage }), [language, setLanguage]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

/* ─────────────────────────────────────────────────────────────────────────
   Theme
   ───────────────────────────────────────────────────────────────────────── */

export type Theme = "light" | "dark" | "system";

const THEME_KEY = "bhasha.theme";

interface ThemeValue {
  theme: Theme;
  /** What is actually on screen once "system" is resolved. */
  resolved: "light" | "dark";
  setTheme: (next: Theme) => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside <Providers>");
  return value;
}

/** Inline in <head> so the class lands before first paint. Without it a caller
 *  on dark sees a white page flash on every navigation, which on a slow phone
 *  is a quarter of a second of glare. Kept as a string so it can be injected
 *  synchronously — a React effect is by definition too late. */
export const THEME_SCRIPT = `
(function(){try{
  var stored = localStorage.getItem(${JSON.stringify(THEME_KEY)});
  var dark = stored === 'dark' || ((!stored || stored === 'system') &&
    window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
}catch(e){}})();
`;

function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useStored<Theme>(THEME_KEY, "system");
  const systemDark = useSystemDark();

  // "system" follows the OS for as long as the caller has not chosen for
  // themselves, so this is derived every render rather than stored.
  const dark = theme === "dark" || (theme === "system" && systemDark);
  const resolved: "light" | "dark" = dark ? "dark" : "light";

  // The `<html>` class *is* an external system, which is what an effect is for.
  // `THEME_SCRIPT` already set it before first paint, so this only ever corrects
  // it after a change.
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  const value = useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/* ── Composed ─────────────────────────────────────────────────────────── */

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <LanguageProvider>
        <SessionProvider>{children}</SessionProvider>
      </LanguageProvider>
    </ThemeProvider>
  );
}
