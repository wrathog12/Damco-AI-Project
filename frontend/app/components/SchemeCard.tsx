"use client";

import { useMemo, useState } from "react";
import {
  BadgeIndianRupee,
  CalendarRange,
  ExternalLink,
  FileText,
  ListChecks,
  Phone,
  Target,
  Users,
  X,
} from "lucide-react";
import { Badge, Card } from "./ui/primitives";
import { IconButton } from "./ui/Button";
import { CARD_LANGUAGES } from "@/lib/types";
import type { Language, SchemeCardData, SchemeTranslation } from "@/lib/types";
import { cn } from "@/lib/cn";

interface SchemeCardProps {
  scheme: SchemeCardData;
  /** The language the tool was called with — the card opens in it. */
  language?: Language;
  onDismiss?: () => void;
  className?: string;
}

/** ₹1,20,000 — Indian grouping, because "120,000" is the wrong shape here. */
const rupees = (value: number) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(value);

/** Title-cases the corpus' occasional SHOUTED or lower-case labels without
 *  touching Devanagari or Bengali, which have no case. */
const label = (value: string) =>
  value.length > 3 && value === value.toUpperCase()
    ? value.charAt(0) + value.slice(1).toLowerCase()
    : value;

export default function SchemeCard({
  scheme,
  language = "en",
  onDismiss,
  className,
}: SchemeCardProps) {
  /**
   * All four card languages are offered, always.
   *
   * v1 built this list by state — English and Hindi for everyone, Bengali only
   * if `metadata.state === "West Bengal"`, Marathi only for Maharashtra. That
   * was wrong on the data: the corpus holds native hi/bn/mr for **all** 1,786
   * schemes. It also had the geography backwards, since the people most likely
   * to need a Bengali reading of a central scheme are exactly the ones looking
   * at a scheme that is not filed under West Bengal.
   */
  /*
   * A new card arrives in the language the conversation is in, which may differ
   * from whatever tab the caller had open on the previous one — so the choice has
   * to reset. That reset is *derived* from the card's identity rather than
   * written back by an effect: keying the override on `(scheme, language)` means
   * a new card simply stops matching and the language falls back on its own, with
   * no second render and no window in which the wrong script is on screen.
   */
  const key = `${scheme.scheme_id}:${language}`;
  const [picked, setPicked] = useState<{ key: string; language: Language } | null>(null);
  const shown = picked?.key === key ? picked.language : language;
  const setShown = (next: Language) => setPicked({ key, language: next });

  const translation: SchemeTranslation | undefined =
    shown === "en" ? undefined : scheme.translations[shown];

  /** Per-key fallback, not per-card: `_translation_block` omits keys myScheme
   *  left untranslated, so a card can have a translated name and an English
   *  description, and showing the English one is better than showing nothing. */
  const name = translation?.scheme_name || scheme.scheme_name;
  const description = translation?.description || scheme.description;
  const eligibilityText =
    translation?.eligibility_description || scheme.eligibility_description;
  const documents =
    translation?.documents_required?.length
      ? translation.documents_required
      : scheme.documents_required;

  const criteria = useMemo(() => {
    const e = scheme.eligibility;
    const out: { icon: React.ReactNode; text: string }[] = [];

    // NULL means "unspecified — do not exclude", so an absent bound produces no
    // line at all rather than a "0 and above" that reads as a real rule.
    if (e.age_min != null || e.age_max != null) {
      out.push({
        icon: <CalendarRange aria-hidden className="size-[1.15rem]" />,
        text:
          e.age_min != null && e.age_max != null
            ? `Age ${e.age_min}–${e.age_max} years`
            : e.age_min != null
              ? `Age ${e.age_min} and above`
              : `Age up to ${e.age_max} years`,
      });
    }
    if (e.income_max != null) {
      out.push({
        icon: <BadgeIndianRupee aria-hidden className="size-[1.15rem]" />,
        text: `Yearly income up to ${rupees(e.income_max)}`,
      });
    }
    if (e.gender?.length) {
      out.push({
        icon: <Users aria-hidden className="size-[1.15rem]" />,
        text: e.gender.map(label).join(", "),
      });
    }
    if (e.caste?.length) {
      out.push({
        icon: <Users aria-hidden className="size-[1.15rem]" />,
        text: e.caste.map(label).join(", "),
      });
    }
    if (e.occupation?.length) {
      out.push({
        icon: <ListChecks aria-hidden className="size-[1.15rem]" />,
        text: e.occupation.map(label).join(", "),
      });
    }
    if (e.disability) {
      out.push({
        icon: <Users aria-hidden className="size-[1.15rem]" />,
        text: "For persons with disability",
      });
    }
    if (e.bpl_card) {
      out.push({
        icon: <FileText aria-hidden className="size-[1.15rem]" />,
        text: "BPL ration card required",
      });
    }
    return out;
  }, [scheme.eligibility]);

  return (
    <Card
      as="article"
      aria-labelledby={`scheme-${scheme.scheme_id}-name`}
      className={cn("animate-rise overflow-hidden", className)}
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="relative border-b border-line bg-primary-soft/60 px-5 py-4 sm:px-6">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1 space-y-2.5">
            <div className="flex flex-wrap items-center gap-1.5">
              {scheme.metadata.state && (
                <Badge tone="primary">{scheme.metadata.state}</Badge>
              )}
              {scheme.metadata.category && (
                <Badge tone="neutral">{scheme.metadata.category}</Badge>
              )}
              {scheme.metadata.level && (
                <Badge tone="neutral">{label(scheme.metadata.level)}</Badge>
              )}
            </div>
            <h2
              id={`scheme-${scheme.scheme_id}-name`}
              className="text-[1.35rem] leading-snug font-extrabold tracking-[-0.02em] text-ink sm:text-2xl"
            >
              {name}
            </h2>
            {scheme.ministry && (
              <p className="text-[0.9375rem] text-ink-muted">{scheme.ministry}</p>
            )}
          </div>

          {onDismiss && (
            <IconButton
              label="Close this scheme"
              variant="ghost"
              size="sm"
              onClick={onDismiss}
              className="-mt-1 shrink-0"
            >
              <X aria-hidden className="size-5" />
            </IconButton>
          )}
        </div>

        {/* Language tabs. Each is written in its own script: someone who needs
            the Bengali reading may not be able to read the word "Bengali". */}
        <div role="tablist" aria-label="Card language" className="mt-4 flex flex-wrap gap-1.5">
          {CARD_LANGUAGES.map((option) => {
            const active = shown === option.code;
            return (
              <button
                key={option.code}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setShown(option.code)}
                className={cn(
                  "min-h-11 rounded-xl px-3.5 text-[0.9375rem] font-semibold transition-colors",
                  active
                    ? "bg-primary text-on-primary shadow-card"
                    : "bg-surface/70 text-ink-muted hover:bg-surface hover:text-ink",
                )}
              >
                {option.native}
              </button>
            );
          })}
        </div>
      </header>

      {/* ── Body ───────────────────────────────────────────────────────── */}
      <div className="scroll-slim max-h-[min(60vh,32rem)] space-y-6 overflow-y-auto px-5 py-5 sm:px-6">
        {description && (
          <p className="text-[1.0625rem] leading-relaxed text-ink">{description}</p>
        )}

        {scheme.benefits.length > 0 && (
          <Section
            icon={<BadgeIndianRupee aria-hidden className="size-[1.15rem]" />}
            title="What you get"
            tone="accent"
          >
            <ul className="space-y-2.5">
              {scheme.benefits.map((benefit, i) => (
                <li
                  key={i}
                  className="rounded-xl border border-accent/25 bg-accent-soft px-4 py-3"
                >
                  {benefit.amount != null && benefit.amount !== "" && (
                    <p className="text-lg font-extrabold text-on-accent">
                      {typeof benefit.amount === "number"
                        ? rupees(benefit.amount)
                        : benefit.amount}
                    </p>
                  )}
                  <p className="text-[0.9375rem] leading-relaxed text-ink">
                    {benefit.description}
                  </p>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {(criteria.length > 0 || eligibilityText) && (
          <Section
            icon={<ListChecks aria-hidden className="size-[1.15rem]" />}
            title="Who can apply"
          >
            {criteria.length > 0 && (
              <ul className="mb-3 grid gap-2 sm:grid-cols-2">
                {criteria.map((item, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2.5 rounded-xl bg-surface-2 px-3.5 py-2.5"
                  >
                    <span className="mt-0.5 shrink-0 text-primary">{item.icon}</span>
                    <span className="text-[0.9375rem] leading-snug text-ink">
                      {item.text}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {eligibilityText && (
              <p className="text-[0.9375rem] leading-relaxed whitespace-pre-wrap text-ink-muted">
                {eligibilityText}
              </p>
            )}
          </Section>
        )}

        {scheme.objectives.length > 0 && (
          <Section
            icon={<Target aria-hidden className="size-[1.15rem]" />}
            title="Purpose"
          >
            <ul className="space-y-1.5">
              {scheme.objectives.map((objective, i) => (
                <li
                  key={i}
                  className="flex gap-2.5 text-[0.9375rem] leading-relaxed text-ink-muted"
                >
                  <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-primary/50" />
                  {objective}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {documents.length > 0 && (
          <Section
            icon={<FileText aria-hidden className="size-[1.15rem]" />}
            title="Papers to carry"
          >
            <ul className="flex flex-wrap gap-1.5">
              {documents.map((doc, i) => (
                <li key={i}>
                  <Badge tone={doc.is_mandatory === false ? "neutral" : "primary"}>
                    {doc.name}
                    {doc.is_mandatory === false && (
                      <span className="font-normal text-ink-subtle">optional</span>
                    )}
                  </Badge>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {scheme.application_process && (
          <Section
            icon={<ListChecks aria-hidden className="size-[1.15rem]" />}
            title="How to apply"
          >
            {/* Pre-wrapped: `_process_text` flattens the structured
                `[{mode, url, steps[]}]` into text with its own line breaks. */}
            <p className="text-[0.9375rem] leading-relaxed whitespace-pre-wrap text-ink-muted">
              {scheme.application_process}
            </p>
          </Section>
        )}
      </div>

      {/* ── Actions ────────────────────────────────────────────────────── */}
      <footer className="flex flex-wrap items-center gap-2 border-t border-line bg-surface-2/60 px-5 py-4 sm:px-6">
        {scheme.application_url && (
          <a
            href={scheme.application_url}
            target="_blank"
            rel="noreferrer"
            className={cn(
              "inline-flex min-h-12 items-center gap-2 rounded-xl bg-primary px-5",
              "font-semibold text-on-primary shadow-card transition-colors hover:bg-primary-hover",
            )}
          >
            Apply on the official site
            <ExternalLink aria-hidden className="size-[1.05rem]" />
          </a>
        )}
        {scheme.source_url && (
          <a
            href={scheme.source_url}
            target="_blank"
            rel="noreferrer"
            className={cn(
              "inline-flex min-h-12 items-center gap-2 rounded-xl border border-line-strong",
              "bg-surface px-4 font-semibold text-ink transition-colors hover:bg-surface-2",
            )}
          >
            Full details
            <ExternalLink aria-hidden className="size-[1.05rem]" />
          </a>
        )}
        {scheme.helpline && (
          <a
            href={`tel:${scheme.helpline.replace(/[^\d+]/g, "")}`}
            className="inline-flex min-h-12 items-center gap-2 rounded-xl px-3 font-semibold text-primary hover:underline"
          >
            <Phone aria-hidden className="size-[1.05rem]" />
            {scheme.helpline}
          </a>
        )}
      </footer>
    </Card>
  );
}

function Section({
  icon,
  title,
  tone = "primary",
  children,
}: {
  icon: React.ReactNode;
  title: string;
  tone?: "primary" | "accent";
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-2.5 flex items-center gap-2 text-[0.8125rem] font-bold tracking-[0.06em] text-ink-subtle uppercase">
        <span className={tone === "accent" ? "text-accent-strong" : "text-primary"}>
          {icon}
        </span>
        {title}
      </h3>
      {children}
    </section>
  );
}
