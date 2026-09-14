"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Keyboard, Mic, ShieldCheck, Sparkles } from "lucide-react";
import SchemeCard from "./components/SchemeCard";
import TextChat from "./components/TextChat";
import VoiceSession from "./components/VoiceSession";
import LanguageSwitcher from "./components/LanguageSwitcher";
import { Card } from "./components/ui/primitives";
import { useLanguage, useSession } from "./providers";
import { api } from "@/lib/api";
import type { Health, ShowSchemeCardEvent } from "@/lib/types";
import { cn } from "@/lib/cn";

type Mode = "talk" | "type";

export default function Home() {
  const { language } = useLanguage();
  const { quota, ready } = useSession();
  const [mode, setMode] = useState<Mode>("talk");
  const [card, setCard] = useState<ShowSchemeCardEvent | null>(null);
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

  // Both panels stay mounted is *not* what happens here, deliberately: an
  // unmounted VoiceSession hangs up, and leaving a live microphone open behind a
  // hidden tab is the kind of thing that gets an app uninstalled.
  const exhausted = ready && quota && !quota.allowed;

  return (
    <div className="mx-auto max-w-6xl px-4 pt-6 pb-16 sm:px-6 sm:pt-10">
      {/* ── Hero ─────────────────────────────────────────────────────────── */}
      <section className="mb-8 text-center sm:mb-10">
        <p className="mb-3 inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-1.5 text-[0.875rem] font-semibold text-ink-muted shadow-card">
          <Sparkles aria-hidden className="size-4 text-accent-strong" />
          {health?.schemes
            ? `${health.schemes.toLocaleString("en-IN")} government schemes, searchable by voice`
            : "Government schemes, searchable by voice"}
        </p>
        <h1 className="mx-auto max-w-2xl text-[2.1rem] leading-[1.1] font-extrabold tracking-[-0.035em] text-ink sm:text-5xl">
          Find the schemes you are{" "}
          <span className="text-primary">actually entitled to</span>.
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-[1.0625rem] leading-relaxed text-ink-muted sm:text-lg">
          Just say what you need — a scholarship, a pension, a loan for a shop.
          Speak Hindi, English, Bengali, Marathi, or a mix. No form to fill in and
          no sign-up to get started.
        </p>

        <div className="mt-5 flex flex-wrap items-center justify-center gap-3">
          <LanguageSwitcher />
          <span className="inline-flex items-center gap-1.5 text-[0.9375rem] text-ink-subtle">
            <ShieldCheck aria-hidden className="size-[1.15rem] text-success" />
            Nothing personal is stored unless you ask us to
          </span>
        </div>
      </section>

      <div
        className={cn(
          "grid gap-6",
          card && "lg:grid-cols-[minmax(0,1fr)_minmax(0,30rem)] lg:items-start",
        )}
      >
        {/* ── The assistant ──────────────────────────────────────────────── */}
        <div className="space-y-4">
          {/* Talk or type, as a real choice rather than a fallback. Voice-only
              excludes anyone in a noisy room, anyone sharing a room, and anyone
              who would rather not be overheard asking about a poverty scheme. */}
          <div
            role="tablist"
            aria-label="How would you like to ask?"
            className="flex gap-1 rounded-2xl border border-line bg-surface-2 p-1"
          >
            {(
              [
                { id: "talk", label: "Talk", icon: Mic },
                { id: "type", label: "Type", icon: Keyboard },
              ] as const
            ).map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={mode === id}
                onClick={() => setMode(id)}
                className={cn(
                  "flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl font-semibold transition-colors",
                  mode === id
                    ? "bg-surface text-ink shadow-card"
                    : "text-ink-muted hover:text-ink",
                )}
              >
                <Icon aria-hidden className="size-[1.15rem]" />
                {label}
              </button>
            ))}
          </div>

          {exhausted && (
            <Card className="border-accent/35 bg-accent-soft p-4 sm:p-5">
              <p className="text-[1.0625rem] font-bold text-on-accent">
                You have used today&apos;s free turns.
              </p>
              <p className="mt-1 text-[0.9375rem] leading-relaxed text-on-accent/85">
                Sign in with your phone number for a much larger daily allowance.
                It takes one text message, and you keep everything you have found
                so far.
              </p>
              <Link
                href="/login"
                className="mt-3 inline-flex min-h-12 items-center rounded-xl bg-primary px-5 font-semibold text-on-primary shadow-card transition-colors hover:bg-primary-hover"
              >
                Sign in with a phone number
              </Link>
            </Card>
          )}

          {mode === "talk" ? (
            <VoiceSession language={language} onCard={setCard} />
          ) : (
            <TextChat language={language} onCard={setCard} />
          )}
        </div>

        {/* ── The card ───────────────────────────────────────────────────── */}
        {card && (
          <div className="lg:sticky lg:top-20">
            <SchemeCard
              scheme={card.scheme}
              language={card.language ?? language}
              onDismiss={() => setCard(null)}
            />
          </div>
        )}
      </div>

      {/* ── What this is ─────────────────────────────────────────────────── */}
      {!card && (
        <section className="mt-12 grid gap-4 sm:grid-cols-3">
          {[
            {
              title: "Ask in your own words",
              body: "No scheme names or category codes. “My daughter is in class 11 in Bihar” is a complete question.",
            },
            {
              title: "Answers you can act on",
              body: "Eligibility is worked out from the published rules, not guessed — and every card links to the official page.",
            },
            {
              title: "Sign in only if you want to",
              body: "A phone number raises your daily limit and lets the assistant remember your details. It is never required.",
            },
          ].map((item) => (
            <Card key={item.title} className="p-5">
              <h2 className="text-[1.0625rem] font-bold text-ink">{item.title}</h2>
              <p className="mt-1.5 text-[0.9375rem] leading-relaxed text-ink-muted">
                {item.body}
              </p>
            </Card>
          ))}
        </section>
      )}
    </div>
  );
}
