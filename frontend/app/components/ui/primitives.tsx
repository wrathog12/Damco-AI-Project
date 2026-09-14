"use client";

import { forwardRef, useId } from "react";
import { cn } from "@/lib/cn";

/* ── Card ─────────────────────────────────────────────────────────────── */

export function Card({
  className,
  as: Tag = "div",
  ...rest
}: React.HTMLAttributes<HTMLElement> & { as?: React.ElementType }) {
  return (
    <Tag
      className={cn(
        "rounded-card border border-line bg-surface shadow-card",
        className,
      )}
      {...rest}
    />
  );
}

/* ── Badge ────────────────────────────────────────────────────────────── */

type Tone = "neutral" | "primary" | "accent" | "success" | "warning" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-muted border-line",
  primary: "bg-primary-soft text-primary border-primary/25",
  accent: "bg-accent-soft text-on-accent border-accent/35",
  success: "bg-success-soft text-success border-success/30",
  warning: "bg-warning-soft text-warning border-warning/35",
  danger: "bg-danger-soft text-danger border-danger/30",
};

export function Badge({
  tone = "neutral",
  className,
  ...rest
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1",
        "text-[0.8125rem] font-semibold leading-tight",
        TONES[tone],
        className,
      )}
      {...rest}
    />
  );
}

/* ── Fields ───────────────────────────────────────────────────────────── */

const CONTROL = cn(
  "w-full min-h-12 rounded-xl border border-line-strong bg-surface px-4",
  "text-base text-ink placeholder:text-ink-subtle",
  "transition-colors hover:border-primary/45",
  "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-ink-subtle",
);

interface FieldShellProps {
  label: string;
  hint?: string;
  error?: string | null;
  children: (id: string, describedBy: string | undefined) => React.ReactNode;
}

/**
 * Label, hint and error wired to the control by id.
 *
 * The render-prop shape exists so the `aria-describedby` link cannot be
 * forgotten: the control receives the ids rather than being asked to declare
 * them, so a hint or a validation message is always announced and not merely
 * displayed.
 */
export function Field({ label, hint, error, children }: FieldShellProps) {
  const base = useId();
  const id = `${base}-control`;
  const hintId = hint ? `${base}-hint` : undefined;
  const errorId = error ? `${base}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-semibold text-ink">
        {label}
      </label>
      {children(id, describedBy)}
      {hint && !error && (
        <p id={hintId} className="text-sm text-ink-subtle">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm font-medium text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(CONTROL, className)} {...rest} />;
  },
);

export const Select = forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, ...rest }, ref) {
  return (
    <select
      ref={ref}
      className={cn(CONTROL, "cursor-pointer appearance-none pr-10", className)}
      style={{
        // A native arrow is the one place a system control beats a styled one:
        // it is the affordance every Android user already recognises. Drawn as a
        // background image so it survives `appearance-none`, which is needed to
        // stop the OS overriding the border radius.
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2.2' stroke-linecap='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
        backgroundRepeat: "no-repeat",
        backgroundPosition: "right 0.85rem center",
      }}
      {...rest}
    />
  );
});

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(CONTROL, "resize-none py-3 leading-relaxed", className)}
      {...rest}
    />
  );
});

/* ── Toggle ───────────────────────────────────────────────────────────── */

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "inline-flex min-h-11 items-center gap-3 rounded-xl px-1 text-left",
        "disabled:opacity-50",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "relative h-7 w-12 shrink-0 rounded-full transition-colors",
          checked ? "bg-primary" : "bg-surface-3",
        )}
      >
        <span
          className={cn(
            "absolute top-1 size-5 rounded-full bg-white shadow-sm transition-[left]",
            checked ? "left-6" : "left-1",
          )}
        />
      </span>
      <span className="text-base font-medium text-ink">{label}</span>
    </button>
  );
}

/* ── Empty state ──────────────────────────────────────────────────────── */

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: React.ReactNode;
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      {icon && (
        <div className="grid size-14 place-items-center rounded-2xl bg-surface-2 text-ink-subtle">
          {icon}
        </div>
      )}
      <h3 className="text-xl font-bold text-ink">{title}</h3>
      {children && (
        <p className="max-w-sm text-base leading-relaxed text-ink-muted">{children}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/* ── Skeleton ─────────────────────────────────────────────────────────── */

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("skeleton rounded-lg", className)} />;
}
