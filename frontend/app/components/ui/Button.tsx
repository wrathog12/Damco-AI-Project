"use client";

import { forwardRef } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "accent";
type Size = "sm" | "md" | "lg";

/**
 * Every clickable thing in the app, so that the accessibility floor is set in
 * one file rather than remembered in forty.
 *
 * The floor: **44px minimum height at every size**, which is the smallest touch
 * target that survives a thumb on a 5" phone. `size="sm"` shrinks the padding
 * and the type, never the hit area — that is the whole reason the sizes are
 * declared here instead of being passed as utilities per call site, where "make
 * this one a bit smaller" quietly becomes a 28px target.
 */
const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-primary text-on-primary shadow-card hover:bg-primary-hover active:scale-[0.985]",
  secondary:
    "bg-surface text-ink border border-line-strong hover:bg-surface-2 active:scale-[0.985]",
  ghost: "text-ink-muted hover:bg-surface-2 hover:text-ink",
  danger:
    "bg-danger-soft text-danger border border-danger/30 hover:bg-danger hover:text-white",
  accent:
    "bg-accent text-on-accent shadow-card hover:bg-accent-strong active:scale-[0.985]",
};

const SIZES: Record<Size, string> = {
  sm: "min-h-11 px-3.5 text-[0.9375rem] gap-1.5 rounded-xl",
  md: "min-h-12 px-5 text-base gap-2 rounded-xl",
  lg: "min-h-14 px-7 text-lg gap-2.5 rounded-2xl",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  /** Stretch to the container. Common enough on mobile to deserve a prop. */
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", loading = false, block = false, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      // A loading button is disabled, but it keeps announcing itself as busy so
      // a screen reader says "submitting" rather than going silent.
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex items-center justify-center font-semibold tracking-[-0.005em]",
        "transition-[background-color,color,transform,box-shadow] duration-150",
        "disabled:pointer-events-none disabled:opacity-50",
        SIZES[size],
        VARIANTS[variant],
        block && "w-full",
        className,
      )}
      {...rest}
    >
      {loading && <Loader2 aria-hidden className="size-[1.15em] animate-spin" />}
      {children}
    </button>
  );
});

/** A square icon-only button. Separate component so `aria-label` is required by
 *  the type system and cannot be forgotten. */
export interface IconButtonProps extends Omit<ButtonProps, "block" | "children"> {
  label: string;
  children: React.ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  function IconButton({ label, size = "md", className, children, ...rest }, ref) {
    return (
      <Button
        ref={ref}
        aria-label={label}
        title={label}
        size={size}
        className={cn(
          "aspect-square px-0",
          size === "sm" ? "w-11" : size === "lg" ? "w-14" : "w-12",
          className,
        )}
        {...rest}
      >
        {children}
      </Button>
    );
  },
);
