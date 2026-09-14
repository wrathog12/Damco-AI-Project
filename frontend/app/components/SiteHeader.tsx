"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AudioLines, ListChecks, UserRound } from "lucide-react";
import { useSession } from "../providers";
import LanguageSwitcher from "./LanguageSwitcher";
import QuotaPill from "./QuotaPill";
import ThemeToggle from "./ThemeToggle";
import { cn } from "@/lib/cn";

const NAV = [
  { href: "/", label: "Assistant", icon: AudioLines },
  { href: "/schemes", label: "My schemes", icon: ListChecks },
  { href: "/profile", label: "My details", icon: UserRound },
] as const;

/** Exact match for the root, prefix match elsewhere — otherwise "/" is active
 *  on every page. */
function isActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export default function SiteHeader() {
  const pathname = usePathname();
  const { authenticated, ready } = useSession();

  return (
    <>
      <header className="sticky top-0 z-40 border-b border-line bg-canvas/85 backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-2.5 sm:px-6">
          <Link
            href="/"
            className="flex shrink-0 items-center gap-2.5 rounded-xl py-1 pr-2"
            aria-label="Bhasha, home"
          >
            <span
              aria-hidden
              className="grid size-9 place-items-center rounded-xl bg-primary text-on-primary shadow-card"
            >
              {/* A speech mark, not a microphone: the product is a conversation,
                  and half of it is typed. */}
              <svg viewBox="0 0 24 24" className="size-5" fill="none" aria-hidden>
                <path
                  d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H9l-5 4v-4Z"
                  stroke="currentColor"
                  strokeWidth="1.9"
                  strokeLinejoin="round"
                />
                <path
                  d="M8.5 10h.01M12 10h.01M15.5 10h.01"
                  stroke="currentColor"
                  strokeWidth="2.6"
                  strokeLinecap="round"
                />
              </svg>
            </span>
            <span className="text-[1.35rem] leading-none font-extrabold tracking-[-0.03em] text-ink">
              Bhasha
            </span>
          </Link>

          <nav aria-label="Main" className="hidden md:flex md:items-center md:gap-1">
            {NAV.map(({ href, label }) => (
              <Link
                key={href}
                href={href}
                aria-current={isActive(pathname, href) ? "page" : undefined}
                className={cn(
                  "inline-flex min-h-11 items-center rounded-xl px-3.5 font-semibold transition-colors",
                  isActive(pathname, href)
                    ? "bg-primary-soft text-primary"
                    : "text-ink-muted hover:bg-surface-2 hover:text-ink",
                )}
              >
                {label}
              </Link>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <QuotaPill />
            <LanguageSwitcher compact />
            <ThemeToggle />
            {/* A link, not a `<Button>` wrapping one: `button > a` is invalid
                markup and gives the row two focus stops for one action. */}
            {ready && !authenticated && (
              <Link
                href="/login"
                className={cn(
                  "hidden min-h-11 items-center rounded-xl bg-primary px-4 font-semibold",
                  "text-on-primary shadow-card transition-colors hover:bg-primary-hover sm:inline-flex",
                )}
              >
                Sign in
              </Link>
            )}
          </div>
        </div>
      </header>

      {/* Mobile navigation as a bottom bar. This audience is overwhelmingly on a
          phone held in one hand, where the top of the screen is the hardest place
          to reach — and a hamburger hides the two surfaces (eligible schemes, my
          details) that are the reason to sign in at all. */}
      <nav
        aria-label="Main"
        className={cn(
          "fixed inset-x-0 bottom-0 z-40 border-t border-line bg-canvas/95 backdrop-blur-xl md:hidden",
          "pb-[env(safe-area-inset-bottom)]",
        )}
      >
        <ul className="mx-auto flex max-w-md">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <li key={href} className="flex-1">
                <Link
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex min-h-14 flex-col items-center justify-center gap-0.5 px-2 py-1.5",
                    active ? "text-primary" : "text-ink-subtle",
                  )}
                >
                  <Icon aria-hidden className="size-[1.35rem]" />
                  <span className="text-[0.75rem] font-semibold">{label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </>
  );
}
