"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import type { Health } from "@/lib/types";

/**
 * The footer carries the corpus size and the privacy posture, both read live
 * from `/health` rather than written into the markup.
 *
 * That is not decoration. "1,786 schemes" is the single most reassuring fact
 * about this product, and a hardcoded number goes stale the first time the
 * corpus changes — at which point the page is confidently wrong about the one
 * thing a user might check. Likewise `profiles_enabled`: when the server has no
 * encryption key, nothing personal can be stored, and saying so is more honest
 * than a privacy promise the deployment is not actually keeping.
 */
export default function SiteFooter() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((next) => {
        if (!cancelled) setHealth(next);
      })
      .catch(() => {
        // A footer is not worth an error state. Absent numbers read as a quiet
        // page; an error banner down here reads as a broken app.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const encrypted = health?.privacy?.profiles_enabled;

  return (
    <footer className="mt-16 border-t border-line bg-surface-2/60 pb-24 md:pb-0">
      <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <div className="flex flex-col gap-8 md:flex-row md:justify-between">
          <div className="max-w-sm space-y-2">
            <p className="text-base font-bold text-ink">Bhasha</p>
            <p className="text-[0.9375rem] leading-relaxed text-ink-muted">
              A voice and text assistant for Indian government welfare schemes.
              {health?.schemes
                ? ` Searching ${health.schemes.toLocaleString("en-IN")} central and state schemes across ${health.states ?? 0} states.`
                : ""}
            </p>
          </div>

          <div className="space-y-3">
            <p className="text-sm font-bold tracking-wide text-ink-subtle uppercase">
              Your data
            </p>
            <ul className="space-y-2 text-[0.9375rem] text-ink-muted">
              <li className="flex items-start gap-2">
                <ShieldCheck aria-hidden className="mt-0.5 size-[1.15rem] shrink-0 text-success" />
                <span>
                  {encrypted === false
                    ? "This server stores nothing personal at all."
                    : "Your details are encrypted before they are stored."}
                </span>
              </li>
              <li>
                <Link href="/privacy" className="font-semibold text-primary hover:underline">
                  What we keep, and for how long
                </Link>
              </li>
              <li>
                <Link href="/profile" className="font-semibold text-primary hover:underline">
                  Delete my data
                </Link>
              </li>
            </ul>
          </div>

          <div className="space-y-3">
            <p className="text-sm font-bold tracking-wide text-ink-subtle uppercase">
              Source
            </p>
            <ul className="space-y-2 text-[0.9375rem] text-ink-muted">
              <li>
                Scheme information from{" "}
                <a
                  href="https://www.myscheme.gov.in"
                  target="_blank"
                  rel="noreferrer"
                  className="font-semibold text-primary hover:underline"
                >
                  myScheme
                </a>
                , a Government of India portal.
              </li>
              <li className="text-ink-subtle">
                Bhasha is not a government service. Always confirm details on the
                official portal before applying.
              </li>
            </ul>
          </div>
        </div>
      </div>
    </footer>
  );
}
