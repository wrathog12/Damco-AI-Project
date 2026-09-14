"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Lock, ShieldCheck, Timer } from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "../components/ui/primitives";
import type { Health } from "@/lib/types";

/**
 * The retention numbers are read from `/health`, not written into this page.
 *
 * A privacy notice that states a window the server does not actually enforce is
 * worse than no notice. The sweep interval and the three windows are configured
 * server-side and reported by `/health`'s `privacy` block, so this page cannot
 * drift away from what the code does.
 */
export default function PrivacyPage() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((next) => {
        if (!cancelled) setHealth(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const privacy = health?.privacy;
  const enabled = privacy?.profiles_enabled;
  const retention = privacy?.retention ?? {};

  return (
    <div className="mx-auto max-w-2xl px-4 pt-8 pb-20 sm:px-6">
      <h1 className="text-[2rem] leading-tight font-extrabold tracking-[-0.03em] text-ink">
        What we keep, and for how long
      </h1>
      <p className="mt-2 text-[1.0625rem] leading-relaxed text-ink-muted">
        Plainly, without the legal wrapper.
        {privacy?.consent_version ? ` Notice version ${privacy.consent_version}.` : ""}
      </p>

      {enabled === false && (
        <Card className="mt-6 border-success/30 bg-success-soft p-5">
          <p className="flex items-start gap-2 text-[1.0625rem] leading-relaxed text-ink">
            <ShieldCheck aria-hidden className="mt-0.5 size-5 shrink-0 text-success" />
            <span>
              <span className="font-bold">
                This server stores nothing personal at all.
              </span>{" "}
              No encryption key is configured, so no details, transcripts or
              eligibility results can be written. Everything below describes what
              would happen if one were.
            </span>
          </p>
        </Card>
      )}

      <section className="mt-8 space-y-4">
        <Block
          icon={<Lock aria-hidden className="size-5 text-primary" />}
          title="If you never sign in, nothing personal is stored"
        >
          You get an anonymous identity — a random id in a signed cookie — purely
          so a daily turn allowance can be counted. No IP address, no device
          fingerprint, no phone number. Delete the cookie and it is gone.
        </Block>

        <Block
          icon={<Lock aria-hidden className="size-5 text-primary" />}
          title="Everything sensitive is encrypted before it is stored"
        >
          Your details, your transcripts and the reasons behind an eligibility
          verdict are encrypted with AES-256-GCM in the application, before they
          reach the database. Each is tied to its own location, so a row copied
          somewhere else will not open. A database dump on its own reveals none of
          it.
        </Block>

        <Block
          icon={<Lock aria-hidden className="size-5 text-primary" />}
          title="We never store an Aadhaar number"
        >
          Not encrypted, not hashed, not at all. No part of this app asks for one,
          and if you say one aloud it is not written anywhere.
        </Block>

        <Block
          icon={<Timer aria-hidden className="size-5 text-primary" />}
          title="Old data is deleted automatically"
        >
          Not as a promise — as a scheduled sweep.
          <ul className="mt-2 space-y-1">
            {[
              ["Transcripts", retention.conversation_days, "the most revealing thing stored, so the shortest window"],
              ["Schemes you looked at", retention.interaction_days, "no sensitive category, and still useful context later"],
              ["Unused anonymous identities", retention.anonymous_identity_days, "an id that counted no turns is not worth keeping"],
            ].map(([label, days, why]) => (
              <li key={String(label)} className="text-[0.9375rem] text-ink-muted">
                <span className="font-semibold text-ink">{label}:</span>{" "}
                {typeof days === "number" ? `${days} days` : "as configured"} — {why}
              </li>
            ))}
          </ul>
        </Block>

        <Block
          icon={<ShieldCheck aria-hidden className="size-5 text-success" />}
          title="Three separate deletions, because they are three separate things"
        >
          You can forget your details, forget your conversations, or delete the
          account entirely — each on its own, all from{" "}
          <Link href="/profile" className="font-semibold text-primary hover:underline">
            My details
          </Link>
          . Deleting the account cascades: no row anywhere survives it.
        </Block>

        <Block
          icon={<ShieldCheck aria-hidden className="size-5 text-success" />}
          title="Your phone number is never shown back to you in full"
        >
          It is stored on its own, used only to send you a code, and masked
          (+*********3210) in every response. The one-time codes themselves are
          stored as keyed hashes and deleted the moment they are used or expire.
        </Block>
      </section>

      <p className="mt-8 text-[0.9375rem] leading-relaxed text-ink-subtle">
        Bhasha is not a government service. Scheme information comes from{" "}
        <a
          href="https://www.myscheme.gov.in"
          target="_blank"
          rel="noreferrer"
          className="font-semibold text-primary hover:underline"
        >
          myScheme
        </a>
        , a Government of India portal, and should always be confirmed there
        before you apply.
      </p>
    </div>
  );
}

function Block({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <Card className="p-5">
      <h2 className="flex items-start gap-2.5 text-[1.0625rem] font-bold text-ink">
        <span className="mt-0.5 shrink-0">{icon}</span>
        {title}
      </h2>
      {children && (
        <div className="mt-1.5 pl-[1.9rem] text-[0.9375rem] leading-relaxed text-ink-muted">
          {children}
        </div>
      )}
    </Card>
  );
}
