"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Globe } from "lucide-react";
import { useLanguage } from "../providers";
import { LANGUAGES } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * The language control. Deliberately the most prominent thing in the header
 * after the logo — the plan lists it as a product requirement, and for most of
 * this audience it is the difference between the app working and not.
 *
 * Each option is labelled in its own script, not in English. "हिन्दी" is legible
 * to someone who cannot read the word "Hindi", which is exactly the person the
 * control is for.
 *
 * A custom menu rather than a native `<select>`, for one reason: the six
 * languages with no card translation have to be visibly marked as
 * voice-only, and a `<select>` cannot carry that without lying about it in the
 * option text.
 */
export default function LanguageSwitcher({ compact = false }: { compact?: boolean }) {
  const { language, setLanguage } = useLanguage();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const current = LANGUAGES.find((l) => l.code === language) ?? LANGUAGES[0];

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={`Language: ${current.label}. Change language`}
        className={cn(
          "inline-flex min-h-11 items-center gap-2 rounded-xl border border-line-strong",
          "bg-surface px-3 font-semibold text-ink transition-colors hover:bg-surface-2",
        )}
      >
        <Globe aria-hidden className="size-[1.15rem] text-primary" />
        <span className={cn(compact && "sr-only sm:not-sr-only")}>{current.native}</span>
      </button>

      {open && (
        <div
          role="listbox"
          aria-label="Language"
          className={cn(
            "animate-rise absolute right-0 z-50 mt-2 max-h-[70vh] w-64 overflow-y-auto",
            "scroll-slim rounded-2xl border border-line bg-surface p-1.5 shadow-float",
          )}
        >
          {LANGUAGES.map((option) => {
            const selected = option.code === language;
            return (
              <button
                key={option.code}
                type="button"
                role="option"
                aria-selected={selected}
                onClick={() => {
                  setLanguage(option.code);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full min-h-12 items-center gap-3 rounded-xl px-3 text-left",
                  "transition-colors hover:bg-surface-2",
                  selected && "bg-primary-soft",
                )}
              >
                <span className="flex-1">
                  <span className="block text-base font-semibold text-ink">
                    {option.native}
                  </span>
                  <span className="block text-[0.8125rem] text-ink-subtle">
                    {option.label}
                    {!option.cards && " · voice only"}
                  </span>
                </span>
                {selected && <Check aria-hidden className="size-5 text-primary" />}
              </button>
            );
          })}
          <p className="px-3 py-2 text-[0.8125rem] leading-snug text-ink-subtle">
            Scheme details are written in English, Hindi, Bengali and Marathi. The
            assistant speaks all ten.
          </p>
        </div>
      )}
    </div>
  );
}
